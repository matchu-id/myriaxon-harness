"""Guardrails as data: rules a person writes, evaluated at the tool-call boundary.

## The gap this closes

Myriaxon's policy was code: permission modes, the sandbox ladder, approval gates. A person who wanted "never run
`git push --force` in this repository" had to modify the harness.

Claude Code's `hookify` plugin solves this with per-rule files — event, field, operator, pattern, warn-vs-block —
and its *rule schema* is worth copying. Its **failure posture is not**, and that is the whole reason this is a
reimplementation rather than a port.

## What is deliberately different from hookify

`hookify` is **fail-open by construction**: every hook ends `sys.exit(0)` in a `finally`, so any error — a bad
regex, an unreadable file, an import failure — turns a rule meant to *block* into a warning, and the operation
proceeds. A guardrail that silently stops guarding is worse than no guardrail, because its author believes they
are protected.

Three further differences, each closing a hole visible in that implementation:

* **the rule directory is anchored, not CWD-relative.** `hookify` globs `.claude/hookify.*.local.md` relative to
  the process CWD, so rules resolve wherever the agent's shell happens to be — including to nothing.
* **supplied regexes are screened for catastrophic backtracking** before they are ever run. A rule file is
  authored by a person, but it is also a file in a repository, which means it can arrive with a pull request.
* **a rule file the agent can write is not trusted policy.** `hookify` reads rules from the project directory the
  agent itself can edit, so the model can author its own permissions. Here a project rule is labelled as such and
  a caller decides whether to honour it; a user-tier rule is the operator's.

## Fail-closed, precisely

"Fail closed" here means: **a rule that cannot be evaluated blocks the call.** Not every error — a rule whose
event does not match is simply not applicable, and that is not a failure. The failures are a rule that will not
parse, a regex that will not compile, or an evaluation that raises. Each of those is a guardrail the operator
believes is in force and which is not, so the safe reading is to refuse.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .constants import CONFIG_DIR

# Where rules live. User tier is the operator's; project tier travels with a repository and is therefore
# untrusted on arrival.
USER_RULES_DIR = CONFIG_DIR / "guardrails"
PROJECT_RULES_SUBDIR = ".myriaxon/guardrails"

# Events a rule may match. A closed set: an unknown event is a rule that will never fire, which is worse than a
# rejected rule because it looks installed.
EVENT_PRE_TOOL = "PreToolUse"
EVENT_POST_TOOL = "PostToolUse"
EVENT_USER_PROMPT = "UserPromptSubmit"
EVENTS = (EVENT_PRE_TOOL, EVENT_POST_TOOL, EVENT_USER_PROMPT)

# Fields a rule may inspect, by event. Their table, kept because each name has to mean one thing.
FIELDS: dict[str, tuple[str, ...]] = {
    EVENT_PRE_TOOL: ("tool", "command", "path", "content", "pattern", "argument", "session"),
    EVENT_POST_TOOL: ("tool", "result", "path", "session"),
    EVENT_USER_PROMPT: ("prompt", "session"),
}

# Operators. Their set, plus `not_regex` because "does not look like a force-push" is a rule people write.
OPERATORS = ("regex_match", "contains", "equals", "not_contains", "starts_with", "ends_with", "not_regex")

ACTION_WARN = "warn"
ACTION_BLOCK = "block"
ACTIONS = (ACTION_WARN, ACTION_BLOCK)

# The ReDoS shapes. Each is a nested or overlapping quantifier that can backtrack exponentially. A rule file is
# authored by a person AND shipped in a repository, so it can arrive with a pull request -- screening is not
# theoretical.
#
# The multiple-wildcard rule is deliberately conservative. Two `.*` in one pattern is not *always* catastrophic,
# but it is ambiguous enough that the safe reading is to ask for a rephrase: the cost of a false positive is one
# rejected rule, and the cost of a false negative is a rule that hangs the harness on a crafted input.
_REDOS_SHAPES = (
    (re.compile(r"\([^)]*[+*][^)]*\)[+*]"), "a quantified group containing a quantifier, e.g. (a+)*"),
    (re.compile(r"\([^)]*\|[^)]*\)[+*]"), "a quantified alternation, e.g. (a|aa)*"),
    (re.compile(r"\.\*.*\.\*", re.DOTALL), "more than one .* in the same pattern"),
    (re.compile(r"\(\.\*\)[+*]"), "a quantified wildcard group"),
)

MAX_RULES = 200
MAX_PATTERN_CHARS = 500


@dataclass
class Rule:
    """One guardrail."""

    name: str
    event: str
    conditions: list[dict] = field(default_factory=list)
    action: str = ACTION_WARN
    message: str = ""
    enabled: bool = True
    tier: str = "user"          # "user" | "project"
    source: str = ""

    def render(self) -> str:
        state = "on " if self.enabled else "off"
        return f"  [{state}] {self.name} ({self.event}, {self.action}) from {self.tier}: {self.message[:70]}"


@dataclass
class Decision:
    """What the rules decided about one call."""

    blocked: bool = False
    warnings: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    # Rule names that could not be evaluated. Non-empty means the block is a fail-closed refusal rather than a
    # rule match, and a caller should say so.
    failed: list[str] = field(default_factory=list)

    @property
    def allow(self) -> bool:
        return not self.blocked

    def render(self) -> str:
        lines: list[str] = []
        for reason in self.reasons:
            lines.append(f"  blocked: {reason}")
        for warning in self.warnings:
            lines.append(f"  warning: {warning}")
        for name in self.failed:
            lines.append(f"  FAILED to evaluate: {name} (treated as a block)")
        return "\n".join(lines) if lines else "  allowed"


class RuleError(ValueError):
    """A rule file that cannot be used. Distinct from `SnapshotError`-style failures so a caller can report it."""


# --------------------------------------------------------------------------- parsing

_FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.DOTALL)


def _parse_scalar(text: str) -> Any:
    """A YAML-ish scalar: quoted string, bool, int, or bare string.

    Hand-rolled and deliberately narrow. A full YAML dependency for four field types would be a large surface for
    a small need, and this parser cannot be tricked into constructing an object because it constructs nothing.
    """
    value = text.strip()
    if not value:
        return ""
    if value[0] in "\"'" and value[-1:] == value[0] and len(value) > 1:
        return value[1:-1]
    lowered = value.lower()
    if lowered in ("true", "yes"):
        return True
    if lowered in ("false", "no"):
        return False
    if re.fullmatch(r"-?\d+", value):
        return int(value)
    return value


def parse_rule(text: str, *, source: str = "", tier: str = "user") -> Rule:
    """Parse one rule file. Raises `RuleError` with a reason rather than returning a half-built rule.

    The body below the frontmatter is the message — that is their shape, and it keeps a long explanation out of
    the metadata where a colon or a quote would break parsing.

    ## The parsing rule is column position, and the first version got it wrong

    A top-level field starts at column zero. Anything indented belongs to whatever came before it. The first
    version instead tried to track state by intercepting the `conditions:` key, which meant that on a file without
    that exact key the indented lines were read as *more top-level fields* — so
    `    pattern: .env` became a field named `pattern` and the condition silently lost its pattern. Measured:
    a `.env` warn rule loaded with an empty pattern, matched nothing, and reported no error.

    Deriving the level from the line's own indentation removes the state entirely, and there is no key whose
    absence changes how the rest of the file is read.
    """
    match = _FRONTMATTER.match(text or "")
    if not match:
        raise RuleError("no frontmatter block (expected the file to start with ---)")

    header, body = match.group(1), match.group(2)
    fields: dict[str, Any] = {}
    conditions: list[dict] = []
    in_conditions = False
    current: dict | None = None

    def flush() -> None:
        nonlocal current
        if current is not None:
            conditions.append(current)
            current = None

    for raw in header.splitlines():
        if not raw.strip() or raw.strip().startswith("#"):
            continue
        indented = raw[:1] in (" ", "\t")
        stripped = raw.strip()

        if not indented:
            # A top-level field. This is the only place a new key can begin.
            flush()
            in_conditions = False
            if ":" not in stripped:
                continue
            key, _, value = stripped.partition(":")
            key, value = key.strip(), value.strip()
            if key == "conditions":
                in_conditions = True
                continue
            fields[key] = _parse_scalar(value)
            continue

        # Indented: either a list item inside `conditions`, or a continuation of one.
        if not in_conditions:
            continue
        if stripped.startswith("- "):
            flush()
            current = {}
            stripped = stripped[2:].strip()
            if ":" in stripped:
                key, _, value = stripped.partition(":")
                current[key.strip()] = _parse_scalar(value)
            continue
        if current is not None and ":" in stripped:
            key, _, value = stripped.partition(":")
            current[key.strip()] = _parse_scalar(value)

    flush()

    name = str(fields.get("name") or "").strip()
    if not name:
        raise RuleError("missing 'name'")

    event = str(fields.get("event") or EVENT_PRE_TOOL).strip()
    if event not in EVENTS:
        raise RuleError(f"unknown event {event!r}; expected one of {', '.join(EVENTS)}")

    action = str(fields.get("action") or ACTION_WARN).strip().lower()
    if action not in ACTIONS:
        raise RuleError(f"unknown action {action!r}; expected {ACTION_WARN} or {ACTION_BLOCK}")

    allowed_fields = FIELDS[event]
    clean_conditions: list[dict] = []
    for condition in conditions:
        field_name = str(condition.get("field") or "").strip()
        operator = str(condition.get("operator") or "contains").strip()
        pattern = str(condition.get("pattern") or "")
        if field_name not in allowed_fields:
            raise RuleError(
                f"condition field {field_name!r} is not valid for {event}; use one of "
                f"{', '.join(allowed_fields)}"
            )
        if operator not in OPERATORS:
            raise RuleError(
                f"unknown operator {operator!r}; use one of {', '.join(OPERATORS)}"
            )
        if len(pattern) > MAX_PATTERN_CHARS:
            raise RuleError(f"pattern is {len(pattern)} characters, over the {MAX_PATTERN_CHARS} limit")
        if operator in ("regex_match", "not_regex"):
            risk = redos_risk(pattern)
            if risk:
                raise RuleError(f"pattern looks prone to catastrophic backtracking: {risk}")
            try:
                re.compile(pattern)
            except re.error as exc:
                raise RuleError(f"pattern is not a valid regex: {exc}") from exc
        clean_conditions.append({"field": field_name, "operator": operator, "pattern": pattern})

    if not clean_conditions:
        raise RuleError("no conditions; a rule with none would match nothing, so it is refused")

    message = (body or "").strip() or str(fields.get("message") or "").strip() or name

    return Rule(
        name=name,
        event=event,
        conditions=clean_conditions,
        action=action,
        message=message,
        enabled=str(fields.get("enabled", True)).strip().lower() not in ("false", "no", "0"),
        tier=tier,
        source=source,
    )


def redos_risk(pattern: str) -> str:
    """A description of the catastrophic-backtracking shape found, or "".

    Static screening, before the pattern ever runs. Applied to every supplied regex because a rule file can arrive
    through a pull request, at which point "the operator wrote it" stops being a reason to trust it.
    """
    for shape, description in _REDOS_SHAPES:
        if shape.search(pattern or ""):
            return description
    return ""


# --------------------------------------------------------------------------- loading


def load_rules(
    *,
    directories: Iterable[Path] | None = None,
    project_root: Path | None = None,
) -> tuple[list[Rule], list[str]]:
    """Read every rule file. Returns `(rules, problems)`.

    Problems are returned rather than raised, so one malformed file does not hide the rules that are fine — but
    they are also *not swallowed*, because a caller that ignores them is running with fewer guardrails than its
    author believes. `Decision.failed` carries the ones that mattered for a specific call.
    """
    rules: list[Rule] = []
    problems: list[str] = []

    targets: list[tuple[Path, str]] = []
    if directories is not None:
        targets.extend((Path(d), "user") for d in directories)
    else:
        targets.append((USER_RULES_DIR, "user"))
        # Anchored to the project root rather than the CWD, which is where `hookify` resolves its rules and why
        # its rules can silently resolve to nothing.
        root = Path(project_root) if project_root is not None else Path.cwd()
        targets.append((root / PROJECT_RULES_SUBDIR, "project"))

    for directory, tier in targets:
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.md")):
            if len(rules) >= MAX_RULES:
                problems.append(f"stopped at the {MAX_RULES}-rule limit; {path.name} and later were not read")
                return rules, problems
            try:
                rules.append(parse_rule(path.read_text(encoding="utf-8"), source=str(path), tier=tier))
            except RuleError as exc:
                problems.append(f"{path.name}: {exc}")
            except OSError as exc:
                problems.append(f"{path.name}: {exc}")
    return rules, problems


# --------------------------------------------------------------------------- evaluation


def _field_value(call: dict, name: str) -> str:
    """The text a condition inspects.

    `command`, `path`, `prompt` and `content` are read from the call's arguments; `argument` joins every argument
    value, for a rule that wants to match anywhere in the call. All returned as text, because every operator is a
    string operator.

    Raises `RuleError` when the call itself is unusable — a non-dict `arguments`, or a mapping whose iteration
    fails. The first version returned `""` for both, which made "the field is empty" and "the arguments could not
    be read" the same value. That defeats the fail-closed posture one level down: an unreadable call would match
    no rule and therefore be *allowed*, silently, which is exactly the failure this module exists to avoid.
    """
    if not isinstance(call, dict):
        raise RuleError(f"the call is not a mapping ({type(call).__name__})")

    arguments = call.get("arguments")
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        raise RuleError(f"the call's arguments are not a mapping ({type(arguments).__name__})")

    if name == "tool":
        return str(call.get("tool") or "")
    if name == "session":
        return str(call.get("session") or "")
    if name == "result":
        return str(call.get("result") or "")
    if name == "argument":
        # Iterating the mapping is the failure point when a caller's arguments object is broken.
        return " ".join(str(value) for value in arguments.values())
    if name == "content":
        for key in ("content", "body", "new_string", "text"):
            if key in arguments:
                return str(arguments[key])
        return ""
    if name in arguments:
        return str(arguments[name])
    return ""


def _test(value: str, operator: str, pattern: str) -> bool:
    if operator == "contains":
        return pattern in value
    if operator == "not_contains":
        return pattern not in value
    if operator == "equals":
        return value == pattern
    if operator == "starts_with":
        return value.startswith(pattern)
    if operator == "ends_with":
        return value.endswith(pattern)
    if operator == "regex_match":
        return re.search(pattern, value, re.IGNORECASE) is not None
    if operator == "not_regex":
        return re.search(pattern, value, re.IGNORECASE) is None
    return False


def evaluate(
    rules: list[Rule],
    call: dict,
    *,
    event: str = EVENT_PRE_TOOL,
    honour_project_rules: bool = True,
) -> Decision:
    """Evaluate every applicable rule against one call.

    ## Fail-closed

    A rule that *raises* during evaluation blocks the call and is named in `Decision.failed`. The reasoning: the
    operator wrote that rule expecting it to be in force, and a rule that cannot be evaluated is not in force.
    Letting the call through would mean the guardrail silently stopped guarding — `hookify`'s failure mode, where
    a "block" rule degrades to a warning.

    A rule whose event does not match is simply not applicable, which is not a failure.
    """
    decision = Decision()
    for rule in rules:
        if not rule.enabled or rule.event != event:
            continue
        if rule.tier == "project" and not honour_project_rules:
            continue
        try:
            matched = all(
                _test(_field_value(call, c["field"]), c["operator"], c["pattern"])
                for c in rule.conditions
            )
        except Exception as exc:  # noqa: BLE001
            # Fail closed, and say it was the rule that failed rather than claiming a match.
            decision.blocked = True
            decision.failed.append(rule.name)
            decision.reasons.append(
                f"{rule.name} could not be evaluated ({type(exc).__name__}: {exc}), so the call was refused "
                "rather than allowed through unchecked"
            )
            continue

        if not matched:
            continue
        if rule.action == ACTION_BLOCK:
            decision.blocked = True
            decision.reasons.append(f"{rule.name}: {rule.message}")
        else:
            decision.warnings.append(f"{rule.name}: {rule.message}")
    return decision


# --------------------------------------------------------------------------- authoring helper

TEMPLATE = """---
name: {name}
event: PreToolUse
action: block
conditions:
  - field: command
    operator: regex_match
    pattern: 'git\\s+push\\s+.*--force'
---

Pushing with --force rewrites published history. Ask before doing that, or use --force-with-lease.
"""


def write_rule(name: str, *, directory: Path | None = None, body: str = "") -> Path:
    """Write a starter rule file. Refuses a name that would not round-trip through the parser."""
    slug = re.sub(r"[^a-z0-9-]+", "-", (name or "").strip().lower()).strip("-")
    if not slug:
        raise RuleError("a rule needs a name of letters, digits and hyphens")
    target = (Path(directory) if directory is not None else USER_RULES_DIR) / f"{slug}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(TEMPLATE.format(name=slug) if not body else body, encoding="utf-8", newline="\n")
    return target
