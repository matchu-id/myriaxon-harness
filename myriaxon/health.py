"""Health checks: detect a problem, name what fixing it would touch, and only then offer to fix it.

## The gap this closes

`myriaxon verify` answers "does this capability work?" — it probes and reports. It cannot answer "is this
installation healthy, and what would repairing it change?" Those are different questions: verify tests
capabilities, a health check inspects *state* (config, files, indexes, service lifetime) and can offer a repair.

OpenClaw's `src/flows/health-checks.ts` has the shape worth copying, and two rules in it are better than anything
I would have invented:

**A repair names its effects before it runs.** `HealthRepairResult.effects[]` is a list of
`{kind, action, target, dryRunSafe}` — `kind` being `config | file | service | process | package | state | other`.
Without this "repair" is a button that does something unspecified, which nobody should press.

**An unavailable owner's repair callback is not executed.** Their
`listExtensionHealthChecksForDoctor` replaces `detect` with a synthesized finding and sets `repair: undefined`
when the owning plugin is unavailable, with the comment *"Preserve selection without executing stale callbacks
from an unavailable owner."* A repair from code that is not currently loaded is a repair nobody can evaluate.

Third, smaller, and worth keeping: `isHealthCheckEnabledByDefault` is false only for an explicit
`defaultEnabled: False`, because *"Opt-in diagnostics stay out of routine lint and repair passes."*

## What is dropped

Their mode triple (`doctor | lint | fix`), update-readiness ordering, per-contribution runner phases, and the
budgeted update work. Those exist to sequence repairs across a 20-version migration history and an updater. This
is one pass over local state with an optional fix.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from .constants import CONFIG_DIR, CONFIG_FILE

# Severity, ranked so a caller can filter without knowing the names.
INFO = "info"
WARNING = "warning"
ERROR = "error"
SEVERITY_RANK = {INFO: 0, WARNING: 1, ERROR: 2}

# Effect kinds, from their `HealthRepairEffect`. `dry_run_safe` on each means "running this with dry_run=True
# tells you exactly what it would do without doing it", which is what makes `--dry-run` honest rather than
# best-effort.
CONFIG = "config"
FILE = "file"
SERVICE = "service"
PROCESS = "process"
PACKAGE = "package"
STATE = "state"
OTHER = "other"


@dataclass
class Finding:
    """One problem, with enough context to act on it."""

    check_id: str
    severity: str
    message: str
    path: str = ""
    fix_hint: str = ""
    target: str = ""

    def render(self) -> str:
        line = f"  [{self.severity:<7}] {self.message}"
        if self.path:
            line += f"\n            at {self.path}"
        if self.fix_hint:
            line += f"\n            fix: {self.fix_hint}"
        return line


@dataclass
class Effect:
    """One thing a repair would do. Named before it happens."""

    kind: str
    action: str
    target: str = ""
    dry_run_safe: bool = True

    def render(self) -> str:
        safety = "" if self.dry_run_safe else "  [not dry-run safe]"
        return f"        {self.kind:<8} {self.action}" + (f" -> {self.target}" if self.target else "") + safety


@dataclass
class RepairResult:
    """What a repair did, or would do."""

    check_id: str
    status: str = "skipped"        # "repaired" | "skipped" | "failed"
    reason: str = ""
    changes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    effects: list[Effect] = field(default_factory=list)
    dry_run: bool = False

    def render(self) -> str:
        head = f"  {self.check_id}: {self.status}"
        if self.dry_run:
            head += " (dry run -- nothing was changed)"
        lines = [head]
        if self.reason:
            lines.append(f"      {self.reason}")
        for change in self.changes:
            lines.append(f"      changed: {change}")
        for warning in self.warnings:
            lines.append(f"      warning: {warning}")
        if self.effects:
            lines.append("      would touch:" if self.dry_run else "      touched:")
            lines.extend(effect.render() for effect in self.effects)
        return "\n".join(lines)


@dataclass
class HealthCheck:
    """One check: a detector, and optionally a repair.

    `repair` receives the findings `detect` produced and the `dry_run` flag, and returns a `RepairResult`. A
    check with no `repair` is report-only, which is a legitimate and common case — most findings should be
    described accurately and left for a person to act on.
    """

    id: str
    description: str
    detect: Callable[[], list[Finding]]
    repair: Callable[[list[Finding], bool], RepairResult] | None = None
    default_enabled: bool = True
    owner: str = "core"

    @property
    def repairable(self) -> bool:
        return self.repair is not None


_REGISTRY: dict[str, HealthCheck] = {}


class HealthCheckRegistrationError(RuntimeError):
    pass


def register(check: HealthCheck, *, replace: bool = False) -> None:
    """Add a check. A duplicate id is refused unless `replace` is set.

    Their registry raises `HealthCheckRegistrationError` with `code = "OC_DOCTOR_DUPLICATE_CHECK"` on a duplicate,
    and refuses to let a plugin claim a core id. The reason is that two checks with one id produce one
    unpredictable report, and the id is how a finding is referred to.
    """
    if not check.id:
        raise HealthCheckRegistrationError("a health check needs an id")
    if check.id in _REGISTRY and not replace:
        raise HealthCheckRegistrationError(f"duplicate health check id: {check.id}")
    if check.id.startswith("core/") and check.owner != "core" and not replace:
        raise HealthCheckRegistrationError(f"{check.owner!r} may not claim the core id {check.id!r}")
    _REGISTRY[check.id] = check


def registered() -> dict[str, HealthCheck]:
    return dict(_REGISTRY)


def unregister(owner: str) -> int:
    """Remove every check owned by `owner`. Returns how many went.

    This is what makes the unavailable-owner rule enforceable: when an owner goes away its repairs must not
    remain runnable, so the removal happens at the registry rather than being checked at each call site.
    """
    with _locked():
        doomed = [k for k, v in _REGISTRY.items() if v.owner == owner]
        for key in doomed:
            _REGISTRY.pop(key, None)
        return len(doomed)


class _locked:
    """Trivial context manager; the registry is process-local and mutated at import time."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc: Any) -> bool:
        return False


def run_checks(
    *,
    only: Iterable[str] | None = None,
    include_opt_in: bool = False,
    min_severity: str = INFO,
) -> dict[str, list[Finding]]:
    """Run every applicable check. Returns findings per check id, including checks that found nothing.

    A check that raises is reported as a finding rather than aborting the pass: a broken detector must not hide
    the state of everything else. Their rule for the same situation: *"A monitor that could not run has no new
    evidence about the system it monitors."*
    """
    wanted = set(only) if only else None
    threshold = SEVERITY_RANK.get(min_severity, 0)
    results: dict[str, list[Finding]] = {}

    for check_id, check in sorted(_REGISTRY.items()):
        if wanted is not None and check_id not in wanted:
            continue
        if not include_opt_in and not check.default_enabled and wanted is None:
            continue
        try:
            findings = check.detect() or []
        except Exception as exc:  # noqa: BLE001
            findings = [Finding(
                check_id=check_id,
                severity=WARNING,
                message=f"the check itself failed: {type(exc).__name__}: {exc}",
                fix_hint="this says nothing about the state it was checking",
            )]
        kept = [f for f in findings if SEVERITY_RANK.get(f.severity, 0) >= threshold]
        results[check_id] = kept
    return results


def repair_all(
    findings_by_check: dict[str, list[Finding]],
    *,
    dry_run: bool = False,
    only: Iterable[str] | None = None,
) -> list[RepairResult]:
    """Repair what can be repaired, in check-id order, reporting what each would touch.

    Checks without a repair are skipped and said so. A repair that raises becomes a `failed` result rather than
    an exception, because one broken repair must not stop the others -- the same containment their plugin loader
    applies to a plugin that throws during registration.
    """
    wanted = set(only) if only else None
    results: list[RepairResult] = []

    for check_id, findings in sorted(findings_by_check.items()):
        if wanted is not None and check_id not in wanted:
            continue
        if not findings:
            continue
        check = _REGISTRY.get(check_id)
        if check is None:
            results.append(RepairResult(
                check_id=check_id, status="skipped",
                reason="the check is no longer registered; its repair was not run",
            ))
            continue
        if check.repair is None:
            results.append(RepairResult(
                check_id=check_id, status="skipped",
                reason="this check reports only; nothing to repair automatically",
                dry_run=dry_run,
            ))
            continue
        try:
            outcome = check.repair(findings, dry_run)
            outcome.dry_run = dry_run
            results.append(outcome)
        except Exception as exc:  # noqa: BLE001
            results.append(RepairResult(
                check_id=check_id, status="failed",
                reason=f"{type(exc).__name__}: {exc}", dry_run=dry_run,
            ))
    return results


def summarise(findings_by_check: dict[str, list[Finding]]) -> dict[str, int]:
    counts = {INFO: 0, WARNING: 0, ERROR: 0}
    for findings in findings_by_check.values():
        for finding in findings:
            counts[finding.severity] = counts.get(finding.severity, 0) + 1
    counts["checks_run"] = len(findings_by_check)
    counts["checks_with_findings"] = sum(1 for f in findings_by_check.values() if f)
    counts["total"] = sum(counts[s] for s in (INFO, WARNING, ERROR))
    return counts


# --------------------------------------------------------------------------- the shipped checks


def _check_config_file() -> list[Finding]:
    findings: list[Finding] = []
    if not CONFIG_FILE.exists():
        findings.append(Finding(
            check_id="core/config-file",
            severity=WARNING,
            message="no configuration file on this machine",
            path=str(CONFIG_FILE),
            fix_hint="run `myriaxon auth` to create one",
        ))
        return findings
    try:
        raw = CONFIG_FILE.read_text(encoding="utf-8")
    except OSError as exc:
        findings.append(Finding(
            check_id="core/config-file", severity=ERROR,
            message=f"the configuration file cannot be read: {exc}",
            path=str(CONFIG_FILE),
        ))
        return findings
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        findings.append(Finding(
            check_id="core/config-file", severity=ERROR,
            message=f"the configuration file is not valid JSON: line {exc.lineno}",
            path=str(CONFIG_FILE),
            fix_hint="repair it by hand, or re-run `myriaxon auth`",
        ))
        return findings
    if not isinstance(parsed, dict):
        findings.append(Finding(
            check_id="core/config-file", severity=ERROR,
            message="the configuration file is not a JSON object",
            path=str(CONFIG_FILE),
        ))
    return findings


def _check_config_permissions() -> list[Finding]:
    """The config holds API keys, so on POSIX it must not be world-readable."""
    if os.name == "nt" or not CONFIG_FILE.exists():
        return []
    try:
        mode = CONFIG_FILE.stat().st_mode & 0o777
    except OSError:
        return []
    if mode & 0o077:
        return [Finding(
            check_id="core/config-permissions",
            severity=ERROR,
            message=f"the configuration file is readable by others (mode {mode:o})",
            path=str(CONFIG_FILE),
            fix_hint="it holds API keys; restrict it to 0600",
        )]
    return []


def _check_workspace_state() -> list[Finding]:
    """A corrupt loci.json or session.json silently resets state, so it is worth detecting."""
    findings: list[Finding] = []
    for name, label in (("loci.json", "Locus registry"), ("skill-usage.json", "skill usage"),
                        ("compaction-audit", "compaction audit")):
        path = CONFIG_DIR / name
        if not path.exists():
            continue
        if path.is_dir():
            continue
        try:
            json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            findings.append(Finding(
                check_id="core/workspace-state", severity=WARNING,
                message=f"{label} at {name} is not valid JSON (line {exc.lineno})",
                path=str(path),
                fix_hint="it will be ignored and recreated; back it up first if it matters",
            ))
        except OSError:
            continue
    return findings


def _check_projects_dir() -> list[Finding]:
    """A project directory that has lost its session file is a session that silently restarted."""
    root = Path.cwd() / ".myriaxon"
    if not root.is_dir():
        return []
    findings: list[Finding] = []
    session = root / "session.json"
    if session.exists():
        try:
            parsed = json.loads(session.read_text(encoding="utf-8"))
            if not isinstance(parsed, dict) or "messages" not in parsed:
                findings.append(Finding(
                    check_id="core/project-session", severity=WARNING,
                    message="the saved session has no messages array",
                    path=str(session),
                    fix_hint="the session will not resume; a new one will start",
                ))
        except (OSError, json.JSONDecodeError):
            findings.append(Finding(
                check_id="core/project-session", severity=WARNING,
                message="the saved session file is unreadable",
                path=str(session),
                fix_hint="the session will not resume; a new one will start",
            ))
    return findings


def _check_skill_workshop() -> list[Finding]:
    """Staged proposals whose draft has gone missing cannot be applied and should be cleaned up."""
    from . import skill_workshop

    findings: list[Finding] = []
    for proposal in skill_workshop.listing():
        if skill_workshop.draft_missing(proposal):
            findings.append(Finding(
                check_id="core/skill-workshop", severity=WARNING,
                message=f"proposal {proposal.id} ({proposal.target_name}) has lost its draft",
                path=proposal.target_dir,
                fix_hint=f"reject it: myriaxon skills --reject {proposal.id} --reason 'draft missing'",
                target=proposal.id,
            ))
    return findings


def _repair_skill_workshop(findings: list[Finding], dry_run: bool) -> RepairResult:
    """Mark proposals with a missing draft as rejected.

    The only repair shipped, and deliberately the safest one available: it changes a record's status and nothing
    on disk, so a dry run and a real run differ only in whether the write happens. A repair that rewrites user
    content is exactly the kind that should require an explicit decision, not a `--fix` flag.
    """
    from . import skill_workshop

    result = RepairResult(check_id="core/skill-workshop", status="repaired")
    for finding in findings:
        proposal_id = finding.target or ""
        result.effects.append(Effect(
            kind=STATE,
            action="mark the proposal rejected",
            target=proposal_id,
            dry_run_safe=True,
        ))
        if dry_run:
            result.changes.append(f"would reject {proposal_id}")
            continue
        outcome = skill_workshop.reject(
            proposal_id, "draft file missing (repaired by doctor)", expected_revision_hash=""
        )
        if outcome.ok:
            result.changes.append(f"rejected {proposal_id}")
        else:
            result.warnings.append(
                f"could not reject {proposal_id}: {'; '.join(outcome.errors) or 'unknown'}"
            )
    if not result.changes and not dry_run:
        result.status = "skipped"
        result.reason = "nothing to change"
    return result


def _check_paths() -> list[Finding]:
    """Directories Myriaxon expects to be able to write."""
    findings: list[Finding] = []
    for label, path in (("config", CONFIG_DIR), ("plugins", CONFIG_DIR / "plugins")):
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            findings.append(Finding(
                check_id="core/paths", severity=ERROR,
                message=f"the {label} directory cannot be created: {exc}",
                path=str(path),
            ))
            continue
        if not os.access(path, os.W_OK):
            findings.append(Finding(
                check_id="core/paths", severity=ERROR,
                message=f"the {label} directory is not writable",
                path=str(path),
            ))
    return findings


def _check_optional_tools() -> list[Finding]:
    """Reported, never repaired: a missing external tool is a fact about the machine, not a defect."""
    findings: list[Finding] = []
    for tool, why in (("git", "versioning and checkpoints"), ("docker", "container isolation")):
        if shutil.which(tool) is None:
            findings.append(Finding(
                check_id="core/optional-tools", severity=INFO,
                message=f"{tool} is not on PATH",
                fix_hint=f"only needed for {why}",
            ))
    return findings


def _check_dot_conditions() -> list[Finding]:
    """Every Locus's condition, without a model call.

    ## Why this is a check rather than a convenience

    A watcher whose condition cannot be evaluated is the failure mode with the worst ratio of damage to visibility:
    it goes quiet, and quiet is what a healthy watcher looks like. Three shapes are detectable locally, and each one
    means the Locus will never do what it was created for:

    * the condition does not validate (an unknown kind, a missing path, a `changed` with no `observe`);
    * the command or observe program does not resolve -- the typo case, which without this appears only in the
      activity log after the fact;
    * the Locus is proactive with no goals, no watchlist and no condition, so `is_due` is False forever.

    The verdict is a DRY RUN (`condition_verdict`), so running `doctor` never consumes the change a watcher is
    waiting for.
    """
    findings: list[Finding] = []
    try:
        from . import loci as loci_module
    except Exception:  # noqa: BLE001
        return findings

    try:
        registry = loci_module.get_registry()
        registry.load()
        all_dots = registry.all()
    except Exception as exc:  # noqa: BLE001
        return [Finding(
            check_id="core/locus-conditions", severity="error",
            message=f"the locus registry could not be read: {exc}",
            fix_hint="run `myriaxon loci` to see the failure, and check ~/.myriaxon/loci.json parses",
        )]

    for locus in all_dots:
        if not locus.proactive:
            continue
        if locus.condition:
            try:
                from . import conditions as conditions_module

                condition = conditions_module.Condition(**locus.condition)
                problem = condition.validate()
                if problem:
                    findings.append(Finding(
                        check_id="core/locus-conditions", severity="error",
                        message=f"locus {locus.name!r} has an unusable condition: {problem}",
                        path=str(loci_module.LOCI_FILE),
                        fix_hint=f"recreate the condition with `locus_create`, or edit {loci_module.LOCI_FILE}",
                        target=locus.id,
                    ))
                    continue

                # Resolution is checked before evaluation, because once the shell has run a missing program and a
                # quiet condition are indistinguishable -- the same reasoning as in `conditions.py`.
                for label, command in (("command", condition.command), ("observe command", condition.observe)):
                    if not command:
                        continue
                    missing = conditions_module.unresolvable_program(command)
                    if missing:
                        findings.append(Finding(
                            check_id="core/locus-conditions", severity="error",
                            message=f"locus {locus.name!r}: {label} cannot run -- {missing}",
                            path=str(loci_module.LOCI_FILE),
                            fix_hint="fix the typo, or use an absolute path to the program",
                            target=locus.id,
                        ))
            except Exception as exc:  # noqa: BLE001
                findings.append(Finding(
                    check_id="core/locus-conditions", severity="error",
                    message=f"locus {locus.name!r} has a condition that could not be examined: {exc}",
                    path=str(loci_module.LOCI_FILE),
                    target=locus.id,
                ))
            continue

        if not locus.scratch.strip() and not locus.runnable_goals():
            findings.append(Finding(
                check_id="core/locus-conditions", severity="warning",
                message=(
                    f"locus {locus.name!r} is proactive but has no goals, no watchlist and no condition, so it will "
                    "never run"
                ),
                path=str(loci_module.LOCI_FILE),
                fix_hint="give it work with `locus_goal_update`, a watchlist with `locus_watchlist`, or a condition",
                target=locus.id,
            ))

    return findings


def _check_guardrails() -> list[Finding]:
    """Guardrail rules that failed to load.

    Named rather than counted. A rule that did not load is a guardrail the operator believes is active and is not,
    which is the single most consequential thing this suite can report -- and it is invisible from the outside,
    because the tool call it was meant to refuse simply goes through.
    """
    findings: list[Finding] = []
    try:
        from . import tool_registry
    except Exception:  # noqa: BLE001
        return findings

    try:
        problems = tool_registry.guardrail_problems()
        rules = tool_registry._guardrail_rules()
    except Exception as exc:  # noqa: BLE001
        return [Finding(
            check_id="core/guardrails", severity="warning",
            message=f"the guardrail rules could not be examined: {exc}",
        )]

    for problem in problems:
        findings.append(Finding(
            check_id="core/guardrails", severity="error",
            message=f"a guardrail rule failed to load: {problem}",
            fix_hint="check .myriaxon/guardrails/ and the user rules directory for a malformed rule",
        ))
    if not rules and not problems:
        # Not a finding: no rules is a legitimate configuration. Reported only in the summary.
        return findings
    return findings


def register_builtin_checks() -> int:
    """Install the shipped checks. Idempotent; returns how many are registered.

    Every check is a function of local state, so this is safe to call on every `doctor` run. The opt-in slot is
    used by `core/optional-tools`, which is reported only when asked for -- *"Opt-in diagnostics stay out of
    routine lint and repair passes."*
    """
    checks = [
        HealthCheck("core/config-file", "the configuration file parses", _check_config_file),
        HealthCheck("core/config-permissions", "the configuration file is not world-readable",
                    _check_config_permissions),
        HealthCheck("core/workspace-state", "Myriaxon's own state files parse", _check_workspace_state),
        HealthCheck("core/project-session", "the saved session in this directory is usable",
                    _check_projects_dir),
        HealthCheck("core/skill-workshop", "staged skill proposals are intact", _check_skill_workshop,
                    repair=_repair_skill_workshop),
        HealthCheck("core/paths", "the directories Myriaxon writes to are usable", _check_paths),
        HealthCheck("core/locus-conditions", "every locus's condition can actually run", _check_dot_conditions),
        HealthCheck("core/guardrails", "every guardrail rule loaded", _check_guardrails),
        HealthCheck("core/optional-tools", "optional external tools", _check_optional_tools,
                    default_enabled=False),
    ]
    count = 0
    for check in checks:
        try:
            register(check, replace=True)
            count += 1
        except HealthCheckRegistrationError:
            continue
    return count
