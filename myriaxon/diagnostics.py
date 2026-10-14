"""Post-edit diagnostics: tell the agent what its own change broke.

## The gap, and why it is the largest one left

Opencode queries a language server **after every `edit`, `write` and `apply_patch`** and appends the resulting error
block to the tool result (`packages/opencode/src/tool/edit.ts`, with the client in `lsp/client.ts`). Myriaxon had
nothing equivalent: after a write, the only signal that a change was wrong was whatever test the agent chose to run,
so **a change that compiles nowhere looks exactly like a change that works**. For a harness whose whole claim is
"prove it by execution", that was the one place execution was absent at the moment of the edit.

## The design decision that matters: tiers, and honesty about which tier ran

A full LSP client is ~3,000 lines in Opencode. This is not that. It is a **tiered** check that reports which tier
actually ran, because the failure mode to avoid is claiming "no problems found" when nothing was checked:

  * **tier 1 -- always available, zero dependencies.** Python: `ast.parse` + `compile()` catches syntax errors.
    JSON/TOML/YAML-lite: parse it. This runs even on a bare host.
  * **tier 2 -- an external checker if one is present**, found via `shutil.which` or the workspace `.tooling/`
    directory: `pyright` for types, `ruff` for lint. Absence is **reported as absence**, never as a clean result.

`DiagnosticReport.render()` says which tier ran. A finding set of zero from tier 2 and a finding set of zero from
tier 1 are different claims, and the agent needs to be able to tell them apart.

## What it deliberately does not do

It does not block a write. A diagnostic is information, and a harness that vetoes an edit because a type checker
disagrees would be wrong about half of legitimate work-in-progress. Opencode appends the block and continues; so does
this. The value is that the model finds out immediately rather than three turns later.
"""

from __future__ import annotations

import ast
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

# A checker must not become the thing that hangs a turn. Opencode's LSP calls are bounded too.
CHECK_TIMEOUT_SECONDS = 60.0

# Severities, ordered. A caller filters on them; `error` is what a person means by "broken".
ERROR = "error"
WARNING = "warning"
INFO = "info"
SEVERITIES = (ERROR, WARNING, INFO)

# Tiers, named so a report can say what it actually did.
TIER_SYNTAX = "syntax (stdlib, always available)"
TIER_JSON = "json/toml (stdlib, always available)"
TIER_PYRIGHT = "pyright (types)"
TIER_RUFF = "ruff (lint)"
TIER_NONE = "nothing available"


@dataclass
class Diagnostic:
    """One problem, located."""

    path: str
    line: int
    column: int
    severity: str
    message: str
    source: str = ""

    def render(self) -> str:
        where = f"{self.path}:{self.line}:{self.column}" if self.line else self.path
        return f"{where} [{self.severity}] {self.message}" + (f" ({self.source})" if self.source else "")


@dataclass
class DiagnosticReport:
    """What was checked, and what was found."""

    checked: list[str] = field(default_factory=list)
    tiers: list[str] = field(default_factory=list)
    diagnostics: list[Diagnostic] = field(default_factory=list)
    unavailable: list[str] = field(default_factory=list)

    @property
    def errors(self) -> list[Diagnostic]:
        return [d for d in self.diagnostics if d.severity == ERROR]

    @property
    def clean(self) -> bool:
        return not self.diagnostics

    def render(self, limit: int = 12) -> str:
        """A block for the model. Says which tier ran, always.

        The tier line is not decoration: "no problems found" from a syntax check is a much weaker statement than the
        same words from a type checker, and a model that cannot tell them apart will treat the first as proof.
        """
        if not self.checked:
            return ""
        head = f"Diagnostics ({', '.join(self.tiers) or TIER_NONE}):"
        if not self.diagnostics:
            lines = [head, f"  no problems in {len(self.checked)} file(s)"]
            for note in self.unavailable:
                lines.append(f"  note: {note}")
            return "\n".join(lines)
        lines = [head, f"  {len(self.diagnostics)} problem(s) in {len(self.checked)} file(s):"]
        for item in self.diagnostics[:limit]:
            lines.append(f"  {item.render()}")
        if len(self.diagnostics) > limit:
            lines.append(f"  ... and {len(self.diagnostics) - limit} more")
        for note in self.unavailable:
            lines.append(f"  note: {note}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------------------
# Tier 1: standard library only
# ---------------------------------------------------------------------------------------


def check_python_syntax(path: Path) -> list[Diagnostic]:
    """Parse and compile a Python file. Catches what `python file.py` would catch, without running it.

    `compile()` as well as `ast.parse` because they disagree: `ast.parse` accepts a file with a bad `return` outside
    a function, and `compile` does not. Running the file would catch more still, but it would also have side effects,
    which is not something a post-edit check may do.
    """
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return [Diagnostic(str(path), 0, 0, ERROR, f"could not be read: {exc}", "io")]
    try:
        ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        return [Diagnostic(
            str(path), int(exc.lineno or 0), int(exc.offset or 0), ERROR,
            (exc.msg or "syntax error"), "syntax",
        )]
    try:
        compile(source, str(path), "exec", dont_inherit=True)
    except SyntaxError as exc:
        return [Diagnostic(
            str(path), int(exc.lineno or 0), int(exc.offset or 0), ERROR,
            (exc.msg or "compile error"), "compile",
        )]
    except ValueError as exc:
        # A null byte, or a source encoding problem. Still a real defect in the file.
        return [Diagnostic(str(path), 0, 0, ERROR, str(exc)[:160], "compile")]
    return []


def check_json(path: Path) -> list[Diagnostic]:
    """Parse a JSON file. A trailing comma or an unquoted key is a real defect that breaks a consumer later."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return [Diagnostic(str(path), 0, 0, ERROR, f"could not be read: {exc}", "io")]
    try:
        json.loads(text)
    except json.JSONDecodeError as exc:
        return [Diagnostic(str(path), int(exc.lineno or 0), int(exc.colno or 0), ERROR,
                           exc.msg or "invalid JSON", "json")]
    return []


def check_toml(path: Path) -> list[Diagnostic]:
    """Parse a TOML file with the standard library parser where one exists."""
    try:
        import tomllib  # Python 3.11+
    except ImportError:
        return []
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return [Diagnostic(str(path), 0, 0, ERROR, f"could not be read: {exc}", "io")]
    try:
        tomllib.loads(raw.decode("utf-8", errors="replace"))
    except Exception as exc:  # noqa: BLE001 -- tomllib raises several types
        text = str(exc)
        line = 0
        if "at line" in text:
            try:
                line = int(text.split("at line")[1].split()[0])
            except (IndexError, ValueError):
                line = 0
        return [Diagnostic(str(path), line, 0, ERROR, text[:160], "toml")]
    return []


# Files tier 1 can check, by suffix.
STDLIB_CHECKERS = {
    ".py": check_python_syntax,
    ".json": check_json,
    ".toml": check_toml,
}


# ---------------------------------------------------------------------------------------
# Tier 2: an external checker, if one is genuinely present
# ---------------------------------------------------------------------------------------


def _workspace_tooling() -> Path:
    """Where fetched tools live. Gitignored, so a checker here is not part of the repository."""
    return Path(__file__).resolve().parent.parent / ".tooling"


def _checker_candidates(name: str) -> list[str]:
    """Every plausible entry point for a checker, shallow first.

    An npm package unpacks as `<pkg>/package/<files>` with several entry points, and they are **not** equivalent.
    Measured on pyright 1.1.414, same arguments, correct code:

      * `package/index.js`        -- errorCount **0**
      * `package/dist/pyright.js` -- errorCount **3**, all `"int" is not defined`

    The second is the bundled CLI and it does not resolve builtins on this host. Two attempts to pick the right one
    by filename failed, because the nesting depth differs per package and the names overlap. So this returns a
    CANDIDATE LIST and `_probe_checker` decides by measurement.
    """
    on_path = shutil.which(name)
    if on_path:
        return [on_path]
    tooling = _workspace_tooling()
    roots = [d for d in (tooling / name, tooling) if d.is_dir()]
    found: list[str] = []
    for root in roots:
        for pattern in (f"**/{name}/package/index.js", f"**/{name}/index.js",
                        f"**/{name}/package/{name}.js", f"**/{name}/package/dist/{name}.js",
                        f"**/{name}/package/cli.js", f"**/{name}/package/{name}.cjs"):
            for path in sorted(root.glob(pattern)):
                if path.is_file() and str(path) not in found:
                    found.append(str(path))
    return found


_probe_cache: dict[str, tuple[bool, str]] = {}


def _probe_checker(name: str) -> tuple[bool, str, str]:
    """Pick an entry point that actually WORKS, by running it on correct code.

    Returns `(usable, entry, note)`. The probe is the only reliable test: a checker that reports errors on a file
    containing nothing but `def add(a: int, b: int) -> int: return a + b` is **worse than no checker**, because the
    model learns to ignore the diagnostic block. That is exactly what the bundled pyright CLI did.
    """
    if name in _probe_cache:
        cached = _probe_cache[name]
        return cached[0], cached[1], cached[2]

    node = _node()
    if name == "pyright" and not node:
        result = (False, "", "node is not on PATH, so pyright cannot run")
        _probe_cache[name] = result
        return result

    try:
        from . import interpreter

        best = interpreter.find()
    except Exception:  # noqa: BLE001
        best = None
    if name == "pyright" and best is None:
        result = (False, "", "no working Python interpreter, so pyright cannot resolve builtins")
        _probe_cache[name] = result
        return result

    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        probe = Path(tmp) / "probe.py"
        probe.write_text(
            "def add(a: int, b: int) -> int:\n    return a + b\n\n\n"
            "def total(xs: list[int]) -> int:\n    return sum(xs)\n",
            encoding="utf-8",
        )
        for entry in _checker_candidates(name):
            try:
                if name == "pyright":
                    command = [node, entry, "--outputjson", "--level", "error",
                               "--pythonpath", best.path, str(probe)]
                else:
                    command = [entry, "check", "--output-format", "json", "--quiet", str(probe)]
                done = subprocess.run(command, cwd=tmp, capture_output=True, text=True,
                                      errors="replace", timeout=CHECK_TIMEOUT_SECONDS)
            except (OSError, subprocess.TimeoutExpired):
                continue
            payload = (done.stdout or "").strip()
            try:
                data = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if name == "pyright":
                problems = data.get("summary", {}).get("errorCount", 1)
            else:
                problems = len(data) if isinstance(data, list) else 1
            if problems == 0:
                result = (True, entry, "")
                _probe_cache[name] = result
                return result

    result = (False, "", (
        f"{name} is present but every entry point reported errors on a file with no errors, so it was NOT used "
        "-- a checker that is wrong about correct code is worse than none"
    ))
    _probe_cache[name] = result
    return result


def find_checker(name: str) -> str:
    """A path to a checker that has been PROVEN to work on correct code, or "".

    Kept as the simple name callers already use; the work is in `_probe_checker`.
    """
    usable, entry, _note = _probe_checker(name)
    return entry if usable else ""


def _node() -> str:
    return shutil.which("node") or ""


def check_with_pyright(paths: list[Path], root: Path) -> tuple[list[Diagnostic], str]:
    """Run pyright over the given files. Returns `(diagnostics, note)`.

    ## `--pythonpath` is not optional here, and getting it wrong is worse than not running

    Measured: without it, pyright reported **`"int" is not defined`** four times against a file containing
    `def add(a: int, b: int) -> int`. It could not locate a Python environment, so it had no builtins -- and the
    cause is the same Windows Store alias this repository already has a module for: pyright looks for `python` on
    PATH, finds an App Execution Alias, and concludes there is no interpreter.

    A type checker producing four false errors on correct code would be worse than having no type checker at all,
    because the model would learn to ignore the block. So the interpreter is resolved through
    `myriaxon.interpreter` -- which *executes* candidates rather than trusting PATH -- and passed explicitly.

    `--outputjson` rather than parsing human output, because the text format is explicitly not a stable interface.
    """
    entry = find_checker("pyright")
    if not entry:
        return [], "pyright is not installed, so types were NOT checked (only syntax)"
    node = _node()
    if not node:
        return [], "node is not on PATH, so pyright could not run and types were NOT checked"

    py_files = [str(p) for p in paths if p.suffix == ".py"]
    if not py_files:
        return [], ""

    command = [node, entry, "--outputjson", "--level", "error"]
    try:
        from . import interpreter

        best = interpreter.find()
        if best is not None:
            command += ["--pythonpath", best.path]
        else:
            # Say so rather than producing builtin errors the model would have to disbelieve.
            return [], (
                "no working Python interpreter was found, so pyright cannot resolve builtins and was NOT run "
                "(checked syntax only)"
            )
    except Exception:  # noqa: BLE001
        return [], "the Python interpreter could not be resolved, so types were NOT checked"

    try:
        done = subprocess.run(
            [*command, *py_files],
            cwd=str(root), capture_output=True, text=True, errors="replace",
            timeout=CHECK_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return [], f"pyright did not finish within {CHECK_TIMEOUT_SECONDS:.0f}s, so types were NOT checked"
    except OSError as exc:
        return [], f"pyright could not be started ({exc}), so types were NOT checked"

    payload = (done.stdout or "").strip()
    if not payload:
        return [], "pyright produced no JSON output, so its result is unknown"
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        return [], "pyright's output was not JSON, so its result is unknown"

    out: list[Diagnostic] = []
    for item in data.get("generalDiagnostics") or []:
        severity = str(item.get("severity") or "error").lower()
        if severity not in SEVERITIES:
            severity = ERROR
        start = ((item.get("range") or {}).get("start") or {})
        out.append(Diagnostic(
            path=_relative(item.get("file", ""), root),
            line=int(start.get("line", 0)) + 1,
            column=int(start.get("character", 0)) + 1,
            severity=severity,
            message=str(item.get("message") or "").splitlines()[0][:200],
            source="pyright",
        ))
    note = ""
    if data.get("summary", {}).get("errorCount", 0) == 0 and not out:
        note = "pyright found no type errors"
    return out, note


def check_with_ruff(paths: list[Path], root: Path) -> tuple[list[Diagnostic], str]:
    """Run ruff if it is present. Lint, not types, so it is a separate tier and separately reported."""
    entry = find_checker("ruff")
    if not entry:
        return [], ""
    py_files = [str(p) for p in paths if p.suffix == ".py"]
    if not py_files:
        return [], ""
    try:
        done = subprocess.run(
            [entry, "check", "--output-format", "json", "--quiet", *py_files],
            cwd=str(root), capture_output=True, text=True, errors="replace",
            timeout=CHECK_TIMEOUT_SECONDS,
        )
    except (subprocess.TimeoutExpired, OSError):
        return [], "ruff did not produce a usable result"
    try:
        items = json.loads((done.stdout or "").strip() or "[]")
    except json.JSONDecodeError:
        return [], "ruff's output was not JSON"
    out = []
    for item in items if isinstance(items, list) else []:
        location = item.get("location") or {}
        out.append(Diagnostic(
            path=_relative(item.get("filename", ""), root),
            line=int(location.get("row", 0)),
            column=int(location.get("column", 0)),
            severity=WARNING,
            message=str(item.get("message") or "")[:200],
            source=f"ruff {item.get('code', '')}".strip(),
        ))
    return out, ""


def _relative(path: str, root: Path) -> str:
    """A path shown relative to the project, so a message is readable and does not leak a home directory."""
    try:
        return str(Path(path).resolve().relative_to(Path(root).resolve()))
    except (ValueError, OSError):
        return path


# ---------------------------------------------------------------------------------------
# The entry point
# ---------------------------------------------------------------------------------------


def check(paths: list[str | Path], root: Path | None = None, *, deep: bool = True) -> DiagnosticReport:
    """Check the given paths and report what was actually verified.

    `deep=False` runs tier 1 only, which is what a caller wants when it must not pay a subprocess per edit.
    """
    base = Path(root).resolve() if root else Path.cwd().resolve()
    report = DiagnosticReport()
    resolved: list[Path] = []
    for item in paths:
        path = Path(item)
        if not path.is_absolute():
            path = base / path
        if path.is_file():
            resolved.append(path)
    if not resolved:
        return report

    report.checked = [str(p) for p in resolved]

    # Tier 1, always.
    #
    # Paths are made relative to the project before being reported: a diagnostic is read by a model that is working
    # in the project, and an absolute temp path is longer, noisier, and leaks the user's directory layout. pyright's
    # findings already arrive relative, so this also makes the two tiers render consistently.
    used_tiers = set()
    for path in resolved:
        checker = STDLIB_CHECKERS.get(path.suffix.lower())
        if checker is None:
            continue
        used_tiers.add(TIER_SYNTAX if path.suffix == ".py" else TIER_JSON)
        for finding in checker(path):
            finding.path = _relative(finding.path, base)
            report.diagnostics.append(finding)
    report.tiers = sorted(used_tiers)

    if not deep:
        # Say so rather than letting a syntax-only pass read as a full check.
        unsupported = [p for p in resolved if p.suffix.lower() not in STDLIB_CHECKERS]
        if unsupported:
            report.unavailable.append(
                f"no checker for {', '.join(sorted({p.suffix for p in unsupported}))} -- not checked"
            )
        return report

    # Tier 2, only when it is genuinely there.
    pyright_findings, pyright_note = check_with_pyright(resolved, base)
    if pyright_findings:
        report.diagnostics.extend(pyright_findings)
        report.tiers.append(TIER_PYRIGHT)
    elif pyright_note and any(p.suffix == ".py" for p in resolved):
        report.unavailable.append(pyright_note)
        if "not installed" not in pyright_note:
            report.tiers.append(TIER_PYRIGHT)

    ruff_findings, ruff_note = check_with_ruff(resolved, base)
    if ruff_findings:
        report.diagnostics.extend(ruff_findings)
        if TIER_RUFF not in report.tiers:
            report.tiers.append(TIER_RUFF)
    elif ruff_note:
        report.unavailable.append(ruff_note)

    if not report.tiers:
        report.tiers = [TIER_NONE]
    unsupported = [p for p in resolved if p.suffix.lower() not in STDLIB_CHECKERS
                   and p.suffix.lower() not in (".ts", ".tsx")]
    if unsupported:
        report.unavailable.append(
            f"no checker for {', '.join(sorted({p.suffix for p in unsupported}))} -- not checked"
        )
    return report


def available_tiers() -> list[str]:
    """Which tiers this host can actually run. Reported by `doctor` and by `session_status`."""
    tiers = [TIER_SYNTAX, TIER_JSON]
    if find_checker("pyright") and _node():
        tiers.append(TIER_PYRIGHT)
    if find_checker("ruff"):
        tiers.append(TIER_RUFF)
    return tiers
