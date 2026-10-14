"""Roles: declarative, portable definitions that a Locus can be created from.

A Locus is a *live agent* — it has goals, a history, a run ledger, and it is persisted in `loci.json` on one
machine. A role is a *template*: what kind of agent this is, what it owns, how it works, and what it hands back.
Keeping those separate is the whole point of this module. Before it, every Locus was created imperatively and
existed only in one machine's JSON, so the answer to "what does a reviewer do here?" was whatever happened to be
in that file — not shareable, not reviewable, and not something a new install had any of.

## Why this shape, and where it came from

This follows OpenClaw's role templates (`docs/reference/templates/roles/*/CLAW.md`), whose most valuable property
is not the file format but the **body structure** they require. Every shipped role answers four questions in the
same order:

1. **Scope and trigger** — what this role owns, and explicitly what it does *not*.
2. **On a task** — a numbered procedure that ends in a verification step.
3. **Handoff contract** — the exact payload returned when the work is done.
4. **Escalation** — when to stop and ask, and what counts as out of scope.

That structure is what makes a role's output checkable rather than aspirational. Their own words for the failure
it prevents: *"Return the exact artifact path or complete brief, source links, conclusion, checks performed, and
meaningful uncertainty."* A role that does not say what it hands back produces work nobody can accept or reject.

`validate_role` enforces the structure, because a role template that skips the handoff contract is the specific
defect this format exists to prevent — the same reasoning as `skill_trust`, applied to agents instead of skills.

## What a role deliberately does not carry

No goals. A role says what kind of work this is; the *goals* are what this particular install wants done, and
shipping goals in a template would mean every user of a "reviewer" role inherits someone else's backlog. A role
may suggest a goal, but installing one leaves the Locus's goal list empty unless the user asks for it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .constants import CONFIG_DIR, PROJECT_SKILLS_SUBDIR
from .frontmatter import parse_frontmatter_doc

# Roles shipped with Myriaxon itself, so a fresh install has a starting set rather than an empty registry.
BUILTIN_ROLES_DIR = Path(__file__).parent / "builtin_roles"
# User-level roles: available in every project on this machine.
USER_ROLES_DIR = CONFIG_DIR / "roles"
# Project-level roles: ship with a repository, like project skills.
PROJECT_ROLES_SUBDIR = ".myriaxon/roles"

_VALID_ROLE_NAME = re.compile(r"^[a-z0-9][a-z0-9-]*$")
MAX_DESCRIPTION_CHARS = 400
MAX_OUTPUT_CHARS = 4_000

# The four sections every role body must contain, as they must appear in the text. Matched case-insensitively on
# the heading text rather than on a strict markdown level, so `## Handoff contract` and `### Handoff` both pass —
# the requirement is that the question is answered, not that the author used a particular heading depth.
_REQUIRED_SECTIONS: tuple[tuple[str, str], ...] = (
    ("scope and trigger", r"scope\s*(?:and|&)\s*trigger"),
    ("on a task", r"on\s+a\s+task"),
    ("handoff contract", r"handoff\s*(?:contract)?"),
    ("escalation", r"escalat"),
)


@dataclass
class RoleReport:
    """What validation could establish about one role."""

    name: str
    ok: bool = False
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    checks: list[str] = field(default_factory=list)

    def render(self) -> str:
        lines = [f"  {'PASS' if self.ok else 'FAIL'}  {self.name}"]
        for message in self.checks:
            lines.append(f"        ok:      {message}")
        for message in self.errors:
            lines.append(f"        error:   {message}")
        for message in self.warnings:
            lines.append(f"        warning: {message}")
        return "\n".join(lines)


@dataclass
class Role:
    """A portable definition of what kind of agent this is."""

    name: str
    description: str
    body: str
    source: Path
    theme: str = ""
    emoji: str = ""
    suggested_goal: str = ""
    proactive: bool = False
    interval_seconds: int = 0  # 0 means "use the Locus default"

    @property
    def instructions(self) -> str:
        return self.body.strip()

    def render(self) -> str:
        marks = f" {self.emoji}" if self.emoji else ""
        lines = [f"  {self.name}{marks}"]
        if self.description:
            lines.append(f"      {self.description}")
        if self.theme:
            lines.append(f"      theme: {self.theme}")
        lines.append(f"      source: {self.source}")
        if self.proactive:
            lines.append(
                f"      proactive: yes"
                + (f", every {self.interval_seconds}s" if self.interval_seconds else "")
            )
        return "\n".join(lines)


def _parse_role_md(text: str, source: Path) -> Role | None:
    parsed = parse_frontmatter_doc(text, source)
    if parsed is None:
        return None
    frontmatter, body = parsed
    if "name" not in frontmatter:
        return None
    try:
        interval = int(frontmatter.get("interval_seconds") or 0)
    except ValueError:
        interval = 0
    return Role(
        name=frontmatter["name"].strip(),
        description=frontmatter.get("description", "").strip(),
        body=body,
        source=source,
        theme=frontmatter.get("theme", "").strip(),
        emoji=frontmatter.get("emoji", "").strip(),
        suggested_goal=frontmatter.get("suggested_goal", "").strip(),
        proactive=frontmatter.get("proactive", "").strip().lower() in ("1", "true", "yes"),
        interval_seconds=max(0, interval),
    )


def _scan_roles_dir(directory: Path) -> dict[str, Role]:
    """Roles in a directory, each as `<name>/ROLE.md` or a flat `<name>.md`."""
    found: dict[str, Role] = {}
    if not directory.is_dir():
        return found
    candidates: list[Path] = []
    for entry in sorted(directory.iterdir()):
        if entry.is_dir():
            role_md = entry / "ROLE.md"
            if role_md.is_file():
                candidates.append(role_md)
        elif entry.suffix == ".md" and entry.is_file():
            candidates.append(entry)
    for path in candidates:
        try:
            text = path.read_text(encoding="utf-8-sig")
        except OSError:
            continue
        role = _parse_role_md(text, path)
        if role is not None:
            found[role.name] = role
    return found


def discover_roles(project_root: Path | None = None) -> dict[str, Role]:
    """Every role available, lowest precedence first: builtin, then user, then project.

    Same precedence order as skills, for the same reason: a project that ships a role should override the
    machine's copy, and both should override the one bundled with the harness.
    """
    roles = _scan_roles_dir(BUILTIN_ROLES_DIR)
    roles.update(_scan_roles_dir(USER_ROLES_DIR))
    root = project_root if project_root is not None else Path.cwd()
    roles.update(_scan_roles_dir(root / PROJECT_ROLES_SUBDIR))
    return roles


def validate_role(role: Role) -> RoleReport:
    """Check a role is usable. Errors refuse an install; warnings do not.

    The section check is the substantive one. A role whose body does not say what it hands back produces work
    that cannot be accepted or rejected on its merits, which is the failure the format exists to prevent — so it
    is an error rather than a suggestion.
    """
    report = RoleReport(name=role.name or "(unnamed)")

    if not role.name:
        report.errors.append("name is empty")
    elif not _VALID_ROLE_NAME.match(role.name):
        report.errors.append(
            "name must be lowercase letters, digits and hyphens, starting with a letter or digit "
            "(so it is safe as a directory name and a Locus name)"
        )
    elif len(role.name) > 64:
        report.errors.append("name is longer than 64 characters")
    else:
        report.checks.append("name is valid and portable")

    if not role.description.strip():
        report.errors.append("description is empty; it is what `roles` lists")
    elif len(role.description) > MAX_DESCRIPTION_CHARS:
        report.errors.append(
            f"description is {len(role.description)} chars, limit is {MAX_DESCRIPTION_CHARS}"
        )
    else:
        report.checks.append("description present")

    body = role.body.strip()
    if len(body) < 40:
        report.errors.append("body is too short to be an operating program")
    elif len(body) > MAX_OUTPUT_CHARS * 4:
        report.errors.append(f"body is {len(body)} chars; a role this large will dominate every run's prompt")
    else:
        report.checks.append(f"body is {len(body)} chars")

    lowered = body.lower()
    for label, pattern in _REQUIRED_SECTIONS:
        if re.search(pattern, lowered):
            report.checks.append(f"answers '{label}'")
        else:
            report.errors.append(
                f"body does not answer '{label}' -- every role must state its scope, its procedure, what it "
                "hands back, and when to escalate"
            )

    if role.interval_seconds and role.interval_seconds < 30:
        report.warnings.append(
            f"interval_seconds is {role.interval_seconds}; the Locus floor is 30s and it will be raised"
        )
    if role.proactive and not role.interval_seconds:
        report.warnings.append("proactive is set but interval_seconds is not; the Locus default will be used")

    report.ok = not report.errors
    return report


def role_to_dot_kwargs(role: Role, name: str | None = None) -> dict:
    """The `LocusRegistry.create` arguments a role implies.

    Goals are deliberately absent: a role says what kind of work this is, not what this install wants done.
    `suggested_goal` is returned separately so a caller can offer it rather than impose it.
    """
    return {
        "name": (name or role.name),
        "role": role.name,
        "instructions": role.instructions,
        "proactive": role.proactive,
        "interval_seconds": role.interval_seconds or 900,
    }


def install_role(
    role: Role,
    registry,
    *,
    name: str | None = None,
    goals: list[str] | None = None,
    proactive: bool | None = None,
):
    """Create a Locus from a role. Returns `(locus, report)`.

    Refuses an invalid role rather than creating a Locus from it: a Locus made from a role with no handoff contract
    would run indefinitely producing work nobody can accept, and the user would have no reason to suspect the
    template was at fault.
    """
    report = validate_role(role)
    if not report.ok:
        return None, report

    kwargs = role_to_dot_kwargs(role, name)
    if proactive is not None:
        kwargs["proactive"] = proactive
    try:
        locus = registry.create(goals=list(goals or []), **kwargs)
    except ValueError as exc:
        # `LocusRegistry.create` raises when the name is taken. That is a refusal, not a crash: installing a role
        # under an existing Locus's name must report the collision rather than overwrite a live agent or blow up
        # the caller, which is a CLI loop that installs several roles in a row and must report one failure and
        # continue with the rest. Found by a test that installed the same role twice and got a traceback.
        report.errors.append(str(exc))
        report.ok = False
        return None, report
    locus.log("created", f"created from the {role.name!r} role ({role.source})")
    registry.persist(locus)
    return locus, report


# --------------------------------------------------------------------------- teams

# A coordinator plus specialists, mirroring OpenClaw's team preset. Kept as a plain mapping so a team can be
# defined in a file later without changing this shape.
DEFAULT_TEAM: dict[str, list[str]] = {
    "coordinator": ["researcher", "reviewer"],
    "researcher": ["writer"],
    "reviewer": [],
}


def install_team(registry, roles: dict[str, Role], members: list[str] | None = None) -> tuple[list, list[str]]:
    """Create a Locus for each named role. Returns `(created_dots, errors)`.

    Installs in dependency-free fashion: each Locus is independent, so one bad role does not prevent the others.
    Errors are collected rather than raised, because a partially-installed team is more useful than none and the
    caller needs to know exactly which member failed.
    """
    wanted = members or [name for name in roles if name in _TEAM_ORDER]
    created: list = []
    errors: list[str] = []
    for name in wanted:
        role = roles.get(name)
        if role is None:
            errors.append(f"{name}: no such role")
            continue
        existing = registry.get(name)
        if existing is not None:
            errors.append(f"{name}: a Locus with that name already exists")
            continue
        locus, report = install_role(role, registry, name=name)
        if locus is None:
            errors.append(f"{name}: {report.errors[0] if report.errors else 'invalid role'}")
            continue
        created.append(locus)
    return created, errors


# Order used when `--team` is called with no members: the coordinator first, then specialists, so the person
# sees the shape of the team in the order it is meant to operate.
_TEAM_ORDER = ("coordinator", "researcher", "writer", "reviewer")
