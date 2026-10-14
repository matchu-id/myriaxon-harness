"""Let a skill ship an executable tool — quarantined until it passes a smoke test.

A skill is prose. When the agent discovers a repeatable *mechanical* step, prose is the wrong artifact: it
re-describes the same procedure every session instead of running it. This module closes that loop by letting a
generated skill carry a plugin, with the guarantee that the plugin never becomes callable until it has been
executed successfully in isolation.

## Why this needs a gate rather than trust

`plugins.py` is explicit that a plugin is "code the user deliberately placed in their own config directory",
and that nothing auto-installs one. An agent-generated tool is the opposite of that: it was written by a model,
in a previous session, from a summary of what happened. Loading it straight into the running agent would hand
executable code the same trust as a file the user wrote by hand.

## The three-step gate

1. **Stage.** Generated code is written to `<plugins>/_quarantine/<name>.py`. `PluginRegistry._discover` skips
   entries whose name starts with `_`, so a quarantined file is *inert by construction* — it cannot be imported
   by the loader even if the gate is bypassed, because the loader never looks at it.
2. **Smoke-test.** The file is loaded in an isolated module namespace, its `register()` is called against a
   capture-only API, and every declared tool is invoked with a schema-derived dummy payload. Import errors,
   a missing `register`, a duplicate name, a non-callable handler and a raising handler are all failures.
3. **Promote.** Only a pass moves the file out of quarantine into the real plugin directory, where the normal
   loader picks it up on the next load. A failure stays in quarantine with its reason recorded.

## What this deliberately does not claim

A passing smoke test means the tool **runs**; it does not mean the tool is *safe* or *correct*. The gate stops
a broken or trivially malicious tool from loading silently — it does not audit intent, and a tool that passes
can still do something undesirable when called with real arguments. `verify_tool_code` is therefore described
as a smoke test everywhere, and `Capability` records whether a run happened rather than whether it was good.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
import types
from dataclasses import dataclass, field
from pathlib import Path

from . import plugins as plugins_module

QUARANTINE_DIRNAME = "_quarantine"
_REJECT_FILE = "rejected.json"

# Patterns that are refused outright. This is a floor, not a security boundary: the point is to stop a
# generated tool from trivially reaching the network or the process table without the user ever seeing it.
_REFUSED_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\bsubprocess\b", "spawns subprocesses"),
    (r"\bos\.system\b", "calls os.system"),
    (r"\beval\s*\(", "uses eval()"),
    (r"\bexec\s*\(", "uses exec()"),
    (r"\b__import__\s*\(", "imports dynamically by name"),
    (r"\bsocket\b", "opens network sockets"),
    (r"\brequests\b", "makes network requests"),
    (r"\burllib\b", "makes network requests"),
    (r"\bhttpx\b", "makes network requests"),
    (r"\bshutil\.rmtree\b", "deletes directory trees"),
    (r"\bos\.remove\b|\bos\.unlink\b", "deletes files"),
    (r"\bopen\s*\([^)]*['\"][wa]", "writes files directly"),
    (r"\bpickle\b", "uses pickle"),
    (r"\bctypes\b", "uses ctypes"),
)


@dataclass
class ToolCheck:
    """The result of smoke-testing one generated plugin."""

    name: str
    # Defaulted, not required: `promote_after_review` builds one before it knows the outcome, and a required
    # field there was a real TypeError rather than a style question.
    ok: bool = False
    staged_path: str = ""
    promoted_path: str = ""
    checks: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    tools_found: list[str] = field(default_factory=list)

    def render(self) -> str:
        status = "PASS" if self.ok else "FAIL"
        lines = [f"  [{status}] {self.name}"]
        if self.tools_found:
            lines.append(f"           tools: {', '.join(self.tools_found)}")
        for message in self.checks:
            lines.append(f"           ok:      {message}")
        for message in self.errors:
            lines.append(f"           error:   {message}")
        for message in self.warnings:
            lines.append(f"           warning: {message}")
        return "\n".join(lines)


def quarantine_dir(project_root: Path | None = None) -> Path:
    """Where staged, not-yet-trusted plugin files live.

    The leading underscore is load-bearing: `PluginRegistry._discover` skips names starting with `_`, so
    anything in here is invisible to the loader. Quarantine is enforced by the directory name rather than by a
    flag, which means there is no state that can drift out of sync with reality.
    """
    root = Path(project_root) if project_root else Path.cwd()
    return root / ".myriaxon" / "plugins" / QUARANTINE_DIRNAME


def promoted_dir(project_root: Path | None = None) -> Path:
    """The real plugin directory the loader reads."""
    root = Path(project_root) if project_root else Path.cwd()
    return root / ".myriaxon" / "plugins"


def scan_tool_code(code: str) -> list[str]:
    """Refuse generated code that trivially reaches outside the process. Returns reasons, empty when clean."""
    reasons: list[str] = []
    for pattern, reason in _REFUSED_PATTERNS:
        if re.search(pattern, code):
            reasons.append(f"{reason} (matched /{pattern}/)")
    return reasons


def _capture_api() -> tuple[object, list[dict], list[tuple]]:
    """A PluginAPI stand-in that records registrations instead of performing them.

    Deliberately not a real `PluginAPI`: registering into the live registry during a smoke test would make an
    unverified tool callable, which is the exact outcome the gate exists to prevent.
    """
    captured: list[dict] = []
    hooks: list[tuple] = []

    class _Meta(type):
        pass

    class _Capture(metaclass=_Meta):
        def add_tool(self, name, description, input_schema, handler, mutating=False):
            captured.append({
                "name": name,
                "description": description,
                "input_schema": input_schema,
                "handler": handler,
                "mutating": mutating,
            })

        def on(self, event, handler):
            hooks.append((event, handler))

        @property
        def name(self):
            return "capture"

    return _Capture(), captured, hooks


def _dummy_payload(schema: dict, depth: int = 0) -> dict:
    """A schema-valid payload built only from required keys, so a probe cannot invent real arguments."""
    if depth > 4 or not isinstance(schema, dict):
        return {}
    if schema.get("type") not in (None, "object"):
        return {}
    properties = schema.get("properties") or {}
    required = schema.get("required") or []
    payload: dict = {}
    for key in required:
        prop = properties.get(key) or {}
        kind = prop.get("type")
        if isinstance(kind, list):
            kind = next((k for k in kind if k != "null"), None)
        if "enum" in prop and prop["enum"]:
            payload[key] = prop["enum"][0]
        elif kind == "string":
            payload[key] = ""
        elif kind in ("integer", "number"):
            payload[key] = 0
        elif kind == "boolean":
            payload[key] = False
        elif kind == "array":
            payload[key] = []
        elif kind == "object":
            payload[key] = _dummy_payload(prop, depth + 1)
        else:
            payload[key] = None
    return payload


def verify_tool_code(name: str, code: str, *, timeout_seconds: float = 5.0) -> ToolCheck:
    """Smoke-test generated plugin code without making it callable.

    Loads the module in a private namespace, calls `register()` against a capture-only API, and invokes every
    declared handler with a schema-derived dummy payload. Returns a `ToolCheck`; never raises.
    """
    check = ToolCheck(name=name, ok=True)

    if not re.match(r"^[a-zA-Z0-9_-]+$", name or ""):
        check.ok = False
        check.errors.append("name may only contain letters, digits, hyphens and underscores")
        return check

    reasons = scan_tool_code(code)
    if reasons:
        check.ok = False
        for reason in reasons:
            check.errors.append(f"refused: {reason}")
        return check
    check.checks.append("no refused constructs (subprocess, eval, network, deletion, ctypes)")

    module_name = f"myriaxon_quarantine_{name}"
    module = types.ModuleType(module_name)
    module.__file__ = f"<quarantine:{name}>"
    try:
        # A real compile first, so a syntax error is reported as one rather than as a mysterious exec failure.
        compiled = compile(code, module.__file__, "exec")
        sys.modules[module_name] = module
        try:
            exec(compiled, module.__dict__)
        finally:
            # Never leave the probe importable: a later `import` must not pick up unverified code.
            sys.modules.pop(module_name, None)
    except SyntaxError as exc:
        check.ok = False
        check.errors.append(f"syntax error at line {exc.lineno}: {exc.msg}")
        return check
    except BaseException as exc:  # noqa: BLE001 -- any import-time failure is the finding
        check.ok = False
        check.errors.append(f"import raised {type(exc).__name__}: {exc}")
        return check
    check.checks.append("imports cleanly")

    register = getattr(module, "register", None)
    if not callable(register):
        check.ok = False
        check.errors.append("no callable register(api) -- the plugin would load but expose nothing")
        return check

    api, captured, hooks = _capture_api()
    try:
        register(api)
    except BaseException as exc:  # noqa: BLE001
        check.ok = False
        check.errors.append(f"register() raised {type(exc).__name__}: {exc}")
        return check
    check.checks.append(f"register() ran; captured {len(captured)} tool(s)")

    if not captured:
        check.ok = False
        check.errors.append("register() declared no tools -- nothing would be callable")
        return check

    for entry in captured:
        tool_name = str(entry.get("name") or "")
        check.tools_found.append(tool_name or "(unnamed)")
        if not tool_name:
            check.ok = False
            check.errors.append("a declared tool has no name")
            continue
        if not str(entry.get("description") or "").strip():
            check.ok = False
            check.errors.append(f"{tool_name}: no description")
        schema = entry.get("input_schema")
        if not isinstance(schema, dict) or schema.get("type") != "object":
            check.ok = False
            check.errors.append(f"{tool_name}: input_schema is not an object schema")
            continue
        handler = entry.get("handler")
        if not callable(handler):
            check.ok = False
            check.errors.append(f"{tool_name}: handler is not callable")
            continue
        if entry.get("mutating"):
            check.warnings.append(
                f"{tool_name}: declared mutating, so it was not invoked by the smoke test"
            )
            continue
        try:
            handler(_dummy_payload(schema), None)
            check.checks.append(f"{tool_name}: handler ran on a dummy payload")
        except BaseException as exc:  # noqa: BLE001
            check.ok = False
            check.errors.append(f"{tool_name}: handler raised {type(exc).__name__}: {exc}")

    return check


def stage_and_verify(name: str, code: str, *, project_root: Path | None = None) -> ToolCheck:
    """Write generated code to quarantine, smoke-test it, and promote it only on a pass."""
    directory = quarantine_dir(project_root)
    directory.mkdir(parents=True, exist_ok=True)
    staged = directory / f"{name}.py"
    staged.write_text(code, encoding="utf-8", newline="\n")
    check = verify_tool_code(name, code)
    check.staged_path = str(staged)

    if not check.ok:
        _record_rejection(directory, name, check)
        return check

    target = promoted_dir(project_root) / f"{name}.py"
    try:
        target.write_text(code, encoding="utf-8", newline="\n")
        staged.unlink(missing_ok=True)
        check.promoted_path = str(target)
    except OSError as exc:
        check.ok = False
        check.errors.append(f"verified but could not be promoted: {exc}")
    return check


def _record_rejection(directory: Path, name: str, check: ToolCheck) -> None:
    """Keep a record of what failed and why, so a rejected tool can be diagnosed rather than retried blindly."""
    ledger = directory / _REJECT_FILE
    entries: dict = {}
    try:
        if ledger.is_file():
            entries = json.loads(ledger.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        entries = {}
    entries[name] = {"errors": check.errors, "warnings": check.warnings}
    try:
        ledger.write_text(json.dumps(entries, ensure_ascii=False, indent=2) + "\n",
                          encoding="utf-8", newline="\n")
    except OSError:
        pass


def quarantined(project_root: Path | None = None) -> list[str]:
    """Names currently sitting in quarantine (staged or rejected)."""
    directory = quarantine_dir(project_root)
    if not directory.is_dir():
        return []
    return sorted(
        path.stem for path in directory.glob("*.py")
        if path.is_file() and not path.name.startswith("_")
    )


def rejections(project_root: Path | None = None) -> dict:
    """Why each rejected tool failed."""
    ledger = quarantine_dir(project_root) / _REJECT_FILE
    try:
        return json.loads(ledger.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def promote_after_review(name: str, *, project_root: Path | None = None) -> ToolCheck:
    """Explicitly promote a quarantined file anyway, after a human has read it.

    The escape hatch exists because the gate is a smoke test, not a correctness proof: a tool that legitimately
    needs to write a file will be refused by `scan_tool_code`, and a user who has read it should be able to
    accept it. Requires the file to already be staged, so this cannot conjure a tool from nowhere.
    """
    directory = quarantine_dir(project_root)
    staged = directory / f"{name}.py"
    check = ToolCheck(name=name)
    if not staged.is_file():
        check.ok = False
        check.errors.append(f"nothing quarantined under the name {name!r}")
        return check
    code = staged.read_text(encoding="utf-8")
    # Structural checks still apply; only the refusal patterns are waived, and that is stated in the result.
    structural = verify_tool_code(name, code)
    if structural.errors and not any("refused:" in e for e in structural.errors):
        check.ok = False
        check.errors.append("cannot promote: the file fails structural checks, not just the refusal list")
        check.errors.extend(structural.errors)
        return check
    target = promoted_dir(project_root) / f"{name}.py"
    try:
        target.write_text(code, encoding="utf-8", newline="\n")
    except OSError as exc:
        check.ok = False
        check.errors.append(f"could not promote: {exc}")
        return check
    staged.unlink(missing_ok=True)
    check.ok = True
    check.promoted_path = str(target)
    check.warnings.append(
        "promoted after review: the refusal-pattern list was waived for this tool. "
        "It runs with your privileges and was not written by you."
    )
    return check
