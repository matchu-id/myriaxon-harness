"""Capability verification: prove what actually works instead of assuming it.

Two things in this harness are load-bearing and were previously taken on faith:

* **Skills** are instructions a future session reads and follows. `skill_trust` validates what is *stored*;
  this module checks what is *usable* — it loads each one through the same path the agent uses.
* **Plugin tools** are third-party code the agent can call. A plugin that imports but whose handler raises on
  its first call is worse than a missing plugin, because the agent will try to use it.

`verify_capabilities()` probes both and returns a report. Nothing here trusts a declaration: a tool is verified
by *calling* it, and a skill by *loading* it. Where a claim genuinely cannot be checked mechanically — whether
a skill's advice is any good — the report says so rather than implying a pass means more than it does.

## Why call the handler rather than inspect it

A `PluginTool` carries a callable and an input schema. Inspecting them proves only that a function object
exists. Calling the handler with a schema-valid dummy payload proves the wiring is live: imports resolve, the
handler accepts the documented shape, and it returns something rather than raising. The dummy payload is
constructed from the schema so it is valid for the documented contract, and any tool that cannot be probed
safely is reported as unprobed rather than silently counted as passing.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import skill_trust
from .skills import Skill

# Tools whose handler must not be called with a synthetic payload: they are documented as mutating, so probing
# them would change the user's state as a side effect of a health check. They are reported as unprobed.
_NEVER_PROBE_MUTATING = True


@dataclass
class Finding:
    """One capability and what was established about it."""

    kind: str          # "skill" | "tool" | "plugin"
    name: str
    ok: bool
    probed: bool
    detail: str = ""
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    checks: int = 0

    def render(self, verbose: bool = False) -> str:
        status = "PASS" if self.ok else "FAIL"
        if not self.probed:
            status = "SKIP"
        marker = "" if self.probed else " (not probed)"
        lines = [f"  [{status}] {self.kind:<6} {self.name}{marker}"]
        if self.detail:
            lines.append(f"           {self.detail}")
        for message in self.errors:
            lines.append(f"           error:   {message}")
        if verbose:
            for message in self.warnings:
                lines.append(f"           warning: {message}")
        return "\n".join(lines)


@dataclass
class Report:
    findings: list[Finding] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def failures(self) -> list[Finding]:
        return [f for f in self.findings if not f.ok and f.probed]

    @property
    def passes(self) -> list[Finding]:
        return [f for f in self.findings if f.ok and f.probed]

    @property
    def skipped(self) -> list[Finding]:
        return [f for f in self.findings if not f.probed]

    @property
    def ok(self) -> bool:
        return not self.failures

    def summary(self) -> str:
        return (
            f"{len(self.passes)} passed, {len(self.failures)} failed, "
            f"{len(self.skipped)} not probed"
        )

    def render(self, verbose: bool = False) -> str:
        lines = [finding.render(verbose) for finding in self.findings]
        if self.notes:
            lines.append("")
            for note in self.notes:
                lines.append(f"  note: {note}")
        return "\n".join(lines)


def _dummy_value(schema: dict, depth: int = 0) -> Any:
    """A schema-valid placeholder value, so a tool can be called without inventing real arguments.

    Kept deliberately boring. The purpose is to exercise the handler's entry, not to make it do work: a probe
    that passes a plausible-looking real path could touch the filesystem, which a health check must not do.
    """
    if depth > 4 or not isinstance(schema, dict):
        return None
    if "enum" in schema and schema["enum"]:
        return schema["enum"][0]
    if "const" in schema:
        return schema["const"]
    kind = schema.get("type")
    if isinstance(kind, list):
        kind = next((k for k in kind if k != "null"), kind[0] if kind else None)
    if kind == "string":
        return ""
    if kind == "integer":
        return 0
    if kind == "number":
        return 0
    if kind == "boolean":
        return False
    if kind == "array":
        return []
    if kind == "object":
        properties = schema.get("properties") or {}
        required = schema.get("required") or []
        return {
            key: _dummy_value(properties.get(key) or {}, depth + 1)
            for key in required
            if key in properties
        }
    return None


def _probe_tool(tool, ctx: Any) -> Finding:
    """Call a plugin tool's handler with a schema-valid payload and see what happens."""
    finding = Finding(kind="tool", name=tool.name, ok=True, probed=True)
    schema = tool.input_schema if isinstance(tool.input_schema, dict) else {}

    # A schema that is not an object or declares no properties cannot be exercised meaningfully.
    if schema.get("type") not in (None, "object") or not isinstance(schema.get("properties"), dict):
        finding.probed = False
        finding.detail = "input_schema is not an object schema; cannot construct a probe"
        return finding

    if getattr(tool, "mutating", False):
        finding.probed = False
        finding.detail = "declared mutating; not probed (a health check must not change your state)"
        return finding

    payload = _dummy_value(schema)
    try:
        result = tool.handler(payload, ctx)
        finding.checks += 1
    except Exception as exc:  # noqa: BLE001 -- any raise is the finding
        finding.ok = False
        finding.errors.append(f"handler raised {type(exc).__name__}: {exc}")
        return finding

    # Handlers may return a string or a (text, is_error) pair, per the PluginAPI contract.
    if isinstance(result, tuple):
        if len(result) != 2:
            finding.ok = False
            finding.errors.append(f"handler returned a {len(result)}-tuple; expected (text, is_error)")
            return finding
        text, is_error = result
        finding.checks += 1
        if is_error:
            # An error return on a synthetic payload is expected for many tools -- they are being asked to do
            # something meaningless. Reported as a detail, not a failure, because the handler *ran*.
            finding.detail = f"handled the probe and returned an error as expected on empty input"
        else:
            finding.detail = f"returned {len(str(text))} chars"
    elif isinstance(result, str):
        finding.detail = f"returned {len(result)} chars"
    else:
        finding.detail = f"returned {type(result).__name__}"

    if not finding.detail:
        finding.detail = "handler callable"
    return finding


def verify_skills(skills: dict[str, Skill]) -> list[Finding]:
    """Check every discovered skill loads, validates, and is not stale."""
    findings: list[Finding] = []
    for name, skill in sorted(skills.items()):
        report = skill_trust.check_skill(name, skill.description, skill.body, skill.source)
        finding = Finding(
            kind="skill",
            name=name,
            ok=report.ok,
            probed=True,
            errors=list(report.errors),
            warnings=list(report.warnings),
            checks=len(report.checked),
        )
        findings.append(finding)
    return findings


def verify_capabilities(
    skills: dict[str, Skill] | None = None,
    registry=None,
    ctx: Any = None,
) -> Report:
    """Probe every skill and plugin tool. Returns a report; never raises.

    `registry` is a `PluginRegistry`. When omitted, the module-level registry is used, which is what the CLI
    already loads — verifying a different one would prove nothing about what the agent can actually call.
    """
    report = Report()

    if skills is None:
        from .skills import discover_skills

        skills = discover_skills()
    report.findings.extend(verify_skills(skills))

    if registry is None:
        try:
            from .plugins import get_registry

            registry = get_registry()
        except Exception as exc:  # noqa: BLE001
            report.notes.append(f"Plugin registry unavailable: {type(exc).__name__}: {exc}")
            registry = None

    if registry is not None:
        # A plugin that failed to import is a finding in its own right: it is installed but inert.
        try:
            described = registry.describe()
        except Exception as exc:  # noqa: BLE001
            described = []
            report.notes.append(f"Could not describe plugins: {type(exc).__name__}: {exc}")

        for plugin in described:
            error = getattr(plugin, "error", "")
            report.findings.append(
                Finding(
                    kind="plugin",
                    name=getattr(plugin, "name", "?"),
                    ok=not error,
                    probed=True,
                    detail=(
                        f"{len(getattr(plugin, 'tools', []))} tool(s), "
                        f"{len(getattr(plugin, 'hooks', []))} hook(s)"
                    ),
                    errors=[error] if error else [],
                )
            )

        try:
            tool_names = registry.tool_names()
        except Exception as exc:  # noqa: BLE001
            tool_names = []
            report.notes.append(f"Could not list plugin tools: {type(exc).__name__}: {exc}")

        for name in sorted(tool_names):
            tool = registry.get_tool(name)
            if tool is None:
                report.findings.append(
                    Finding(kind="tool", name=name, ok=False, probed=True,
                            errors=["listed by the registry but not resolvable"])
                )
                continue
            if ctx is None:
                report.findings.append(
                    Finding(kind="tool", name=name, ok=True, probed=False,
                            detail="no ToolContext available; handler not called")
                )
                continue
            report.findings.append(_probe_tool(tool, ctx))

    if not report.findings:
        report.notes.append("Nothing to verify: no skills discovered and no plugin tools registered.")

    # Stated in the report rather than left implied, because a PASS column invites over-reading.
    report.notes.append(
        "A passing skill means it is well-formed, safe to store and still resolvable -- not that its advice "
        "is correct, which cannot be checked by inspection."
    )
    return report


def build_manifest(findings: list[Finding], notes: list[str] | None = None) -> dict:
    """The capability manifest: what this harness has, and what was actually established about each item.

    Deliberately records `probed` alongside `ok`. A manifest that lists only pass/fail invites the reading that
    everything green was exercised, when some entries were skipped because probing them would have changed
    state. `probed: false` is the honest third state.
    """
    return {
        "generator": "myriaxon verify",
        "counts": {
            "total": len(findings),
            "passed": len([f for f in findings if f.probed and f.ok]),
            "failed": len([f for f in findings if f.probed and not f.ok]),
            "not_probed": len([f for f in findings if not f.probed]),
        },
        "capabilities": [
            {
                "kind": f.kind,
                "name": f.name,
                "probed": f.probed,
                "ok": f.ok,
                "detail": f.detail,
                "errors": f.errors,
                "warnings": f.warnings,
                "checks": f.checks,
            }
            for f in sorted(findings, key=lambda item: (item.kind, item.name))
        ],
        "notes": notes or [],
    }


def write_manifest(path: str | Path, findings: list[Finding], notes: list[str] | None = None) -> Path:
    """Write the manifest as JSON. Returns the resolved path so a caller can report it."""
    import json

    target = Path(path)
    if target.is_dir():
        target = target / "capabilities.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(build_manifest(findings, notes), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return target


def verify_tool_defs(tool_defs: list[dict]) -> list[Finding]:
    """Structural check of every built-in tool definition the model is offered.

    Cheap and mechanical, and it catches a whole class of defect that is otherwise invisible until the model
    calls the tool: a duplicate name, a missing description, or an `input_schema` that is not an object schema.
    A malformed schema is rejected by providers at request time, which surfaces as a failed turn rather than as
    a bad tool.

    **Server-side tool declarations are excluded from the schema and description checks**, and this is not a
    convenience. Anthropic defines `web_search` and `web_fetch` itself; the harness declares only

        {"type": "web_search_20260209", "name": "web_search"}

    because the schema and description live on the provider's side. The first version of this function checked
    them anyway and reported both as failures ("no description -- the model cannot know when to call it"),
    which is exactly the kind of confidently wrong finding that teaches a reader to ignore the checker. They
    are still checked for the things that *are* local: a name, and a type, and no duplicate.
    """
    findings: list[Finding] = []
    seen: dict[str, int] = {}
    for definition in tool_defs:
        name = str(definition.get("name") or "")
        # A definition carrying a vendor `type` and no `input_schema` is a server-side declaration.
        server_side = bool(definition.get("type")) and "input_schema" not in definition
        finding = Finding(kind="tool", name=name or "(unnamed)", ok=True, probed=True)
        if not name:
            finding.ok = False
            finding.errors.append("tool definition has no name")
        else:
            seen[name] = seen.get(name, 0) + 1
            if seen[name] > 1:
                finding.ok = False
                finding.errors.append(f"duplicate tool name (seen {seen[name]} times)")

        if server_side:
            finding.detail = f"server-side declaration ({definition.get('type')}); schema and description are the provider's"
            finding.checks = 1
            finding.warnings.append(
                "cannot be checked locally beyond its name and type"
            )
            findings.append(finding)
            continue

        if not str(definition.get("description") or "").strip():
            finding.ok = False
            finding.errors.append("no description -- the model cannot know when to call it")
        schema = definition.get("input_schema")
        if not isinstance(schema, dict):
            finding.ok = False
            finding.errors.append("input_schema is missing or not an object")
        elif schema.get("type") != "object":
            finding.ok = False
            finding.errors.append(f"input_schema type is {schema.get('type')!r}, expected 'object'")
        elif not isinstance(schema.get("properties"), dict):
            finding.ok = False
            finding.errors.append("input_schema has no properties object")
        else:
            required = schema.get("required") or []
            missing = [key for key in required if key not in schema["properties"]]
            if missing:
                finding.ok = False
                finding.errors.append(f"required keys absent from properties: {missing}")
            for key, prop in schema["properties"].items():
                if not isinstance(prop, dict) or not prop:
                    finding.warnings.append(f"property {key!r} has an empty schema")
        finding.detail = "structural check only"
        findings.append(finding)
    return findings
