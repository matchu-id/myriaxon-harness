import re
from dataclasses import dataclass
from pathlib import Path

from . import skill_trust, templating
from .constants import PROJECT_SKILLS_SUBDIR, USER_SKILLS_DIR
from .frontmatter import parse_frontmatter_doc

_VALID_SKILL_NAME = re.compile(r"^[a-zA-Z0-9_-]+$")

# Skills shipped with Myriaxon itself (package data, not a user/project file) -- lowest priority,
# so a user or project skill with the same name overrides it. See builtin_skills/graphify/.
BUILTIN_SKILLS_DIR = Path(__file__).parent / "builtin_skills"

USE_SKILL_TOOL_DEF = {
    "name": "use_skill",
    "description": (
        "Load a discovered skill's full instructions by exact name (see the skill listing in the "
        "system prompt). Read-only, no confirmation."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "args": {"type": "string", "description": "Optional arguments substituted into the skill body."},
        },
        "required": ["name"],
    },
}


SAVE_SKILL_TOOL_DEF = {
    "name": "save_skill",
    "description": (
        "Save a genuinely reusable pattern you just discovered as a new skill, so future "
        "sessions (or use_skill calls later in this one) can reuse it without rediscovering it "
        "from scratch. Only call this for real, generalizable approaches -- not for one-off "
        "trivial actions; it's normal and expected to not call this most of the time. No "
        "confirmation needed. Pass tool_code ONLY when the skill's value is a mechanical step that "
        "would be better run than re-described; it is smoke-tested before it can ever be called."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Short, unique name (letters/digits/hyphens/underscores only).",
            },
            "description": {"type": "string", "description": "One-line summary shown in the skill listing."},
            "body": {"type": "string", "description": "The full reusable instructions/approach."},
            "scope": {
                "type": "string",
                "enum": ["project", "user"],
                "description": "'project' (default) saves it for this project only; 'user' makes it available across all projects.",
            },
            "tool_code": {
                "type": "string",
                "description": (
                    "Optional Python source for a plugin that makes this skill runnable instead of merely "
                    "descriptive. Define register(api) and call api.add_tool(name, description, "
                    "input_schema, handler). The code is quarantined and smoke-tested: it becomes callable "
                    "only if it imports cleanly and every handler runs on a dummy payload. Code that shells "
                    "out, evaluates strings, or touches the network is refused."
                ),
            },
        },
        "required": ["name", "description", "body"],
    },
}


@dataclass
class Skill:
    name: str
    description: str
    body: str
    source: Path


def _parse_skill_md(text: str, source: Path) -> Skill | None:
    parsed = parse_frontmatter_doc(text, source)
    if parsed is None:
        return None
    frontmatter, body = parsed
    if "name" not in frontmatter:
        return None

    return Skill(
        name=frontmatter["name"],
        description=frontmatter.get("description", ""),
        body=body,
        source=source,
    )


def _scan_skills_dir(skills_dir: Path) -> dict[str, Skill]:
    found: dict[str, Skill] = {}
    if not skills_dir.is_dir():
        return found

    for entry in sorted(skills_dir.iterdir()):
        skill_md = entry / "SKILL.md"
        if not skill_md.is_file():
            continue
        try:
            text = skill_md.read_text(encoding="utf-8")
        except OSError:
            continue
        skill = _parse_skill_md(text, skill_md)
        if skill is not None:
            found[skill.name] = skill

    return found


def discover_skills(project_root: Path | None = None) -> dict[str, Skill]:
    skills = _scan_skills_dir(BUILTIN_SKILLS_DIR)  # lowest priority -- shipped with Myriaxon
    skills.update(_scan_skills_dir(USER_SKILLS_DIR))

    root = project_root if project_root is not None else Path.cwd()
    skills.update(_scan_skills_dir(root / PROJECT_SKILLS_SUBDIR))

    return skills


def format_skill_listing(skills: dict[str, Skill]) -> str:
    """The listing shown in the system prompt.

    Delegates to `skill_trust`, which marks self-generated skills. A model-written skill and a hand-reviewed
    one used to look identical here, which asks the reader to trust both equally.
    """
    return skill_trust.format_skill_listing(skills)


def execute_use_skill(tool_input: dict, skills: dict[str, Skill]) -> tuple[str, bool]:
    name = tool_input["name"]
    skill = skills.get(name)
    if skill is None:
        available = ", ".join(sorted(skills)) or "(none)"
        return f"No skill named {name!r} found. Available: {available}", True

    # Usage telemetry. Recorded on *successful* lookup only, so a typo does not count as a use of whatever
    # skill happened to be near the name. This is what makes `skills --usage` able to say a skill has never
    # been invoked -- a fact `verify` cannot establish, because loading a file and using it are different
    # things and only the second one justifies the skill's cost in every future system prompt.
    try:
        from . import skill_usage

        skill_usage.record_use(
            name,
            source=str(skill.source),
            agent_id=str(tool_input.get("_agent_id") or ""),
        )
    except Exception:
        # Telemetry must never stop a skill from loading. A missing use count is a missing statistic.
        pass

    body = templating.substitute_args(skill.body, tool_input.get("args", ""))
    return f"(Skill directory: {skill.source.parent})\n\n{body}", False


def execute_save_skill(tool_input: dict, ctx) -> tuple[str, bool]:
    """Save a generated skill, but only after it passes the trust checks in `skill_trust`.

    This is the gate. Everything the model writes arrives here, and from the next launch onward whatever is
    written is read back as instructions -- so a save that is malformed or unsafe is refused rather than
    stored. Refusals name the specific problem and what to do instead, because a generated skill that fails
    validation is usually fixable in one more attempt.
    """
    name = tool_input["name"]
    # Whitespace is collapsed rather than the newline being rejected, because a generated description
    # frequently arrives with a trailing newline and refusing it would be a poor trade. Collapsing BEFORE
    # validation closes the injection hole: the previous order validated the raw newline-containing string,
    # then flattened it, so the check never saw the value that was actually written.
    description = re.sub(r"\s+", " ", tool_input["description"]).strip()
    body = tool_input["body"]
    scope = tool_input.get("scope", "project")

    report = skill_trust.validate_skill(name, description, body)
    if not report.ok:
        detail = "\n".join(f"  - {message}" for message in report.errors)
        return (
            f"Skill not saved: it failed validation.\n{detail}\n"
            "Fix the above and call save_skill again.",
            True,
        )

    base_dir = USER_SKILLS_DIR if scope == "user" else (Path.cwd() / PROJECT_SKILLS_SUBDIR)
    skill_dir = base_dir / name
    skill_md = skill_dir / "SKILL.md"

    # A collision means this save would shadow an existing skill. Precedence (builtin -> user -> project) makes
    # that silent, so it is reported: overwriting a hand-written skill with a generated one should be a
    # decision, not a side effect.
    shadowed = _find_shadowed(name, skill_md)

    # Staging. A generated skill is *proposed*, not published: it goes to the workshop as a pending record and
    # only a separate explicit apply writes it to the live root, capturing a rollback record first. This is the
    # difference between "the gate refused a malformed skill" and "nothing reaches the live instruction set
    # without someone deciding it should" -- the first still let a well-formed but bad idea become real
    # immediately, from the next launch onward, with no way back.
    #
    # An explicit `publish: true` writes live, for a caller that has decided it does not want the review step.
    if not tool_input.get("publish"):
        from . import skill_workshop

        outcome = skill_workshop.propose(
            name, description, body,
            origin="agent:save_skill",
            project_root=Path.cwd(),
        )
        if not outcome.ok:
            detail = "\n".join(f"  - {message}" for message in outcome.errors)
            return f"Skill not proposed: it failed validation.\n{detail}", True

        proposal = outcome.proposal
        # Usable in this session only. It is deliberately NOT in the on-disk live root, so the next launch does
        # not load it: a staged skill must not become trusted instructions just because it was written once.
        ctx.skills[name] = Skill(name=name, description=description, body=body, source=skill_md)
        notes = (
            f"\nStaged as proposal {proposal.id} ({proposal.kind}) -- NOT yet live, so future sessions will "
            f"not load it.\nReview it with `myriaxon skills --show {proposal.id}`, then "
            f"`myriaxon skills --apply {proposal.id}` to write it, or `--reject` to discard it.\n"
            "It is usable via use_skill for the rest of this session only."
        )
        if shadowed:
            notes += f"\nNote: applying it would shadow an existing skill at {shadowed}."
        if outcome.notes:
            notes += "\n" + "\n".join(outcome.notes)
        if report.warnings:
            notes += "\nWarnings:\n" + "\n".join(f"  - {m}" for m in report.warnings)
        return f"Staged skill {name!r} as a proposal.{notes}", False

    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_md.write_text(
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        f"{skill_trust.provenance_block('agent:save_skill', body)}"
        "---\n"
        f"{body}\n",
        encoding="utf-8",
    )

    # Update the in-memory dict too, so it's usable via use_skill immediately in this same
    # session without waiting for the next launch's discover_skills() pass.
    ctx.skills[name] = Skill(name=name, description=description, body=body, source=skill_md)

    notes = ""
    if shadowed:
        notes = f"\nNote: this shadows an existing skill at {shadowed}."
    if report.warnings:
        notes += "\nWarnings:\n" + "\n".join(f"  - {m}" for m in report.warnings)

    # Optional executable companion. A skill is prose; when the reusable part is a *mechanical* step, shipping
    # a plugin makes it runnable instead of re-described every session. The gate is in `skill_tools`: the code
    # is staged under a directory the plugin loader skips, smoke-tested, and promoted only if it passes.
    tool_code = tool_input.get("tool_code")
    if isinstance(tool_code, str) and tool_code.strip():
        try:
            from . import skill_tools

            check = skill_tools.stage_and_verify(name, tool_code, project_root=Path.cwd())
        except Exception as exc:  # noqa: BLE001
            notes += (
                f"\nTool: NOT saved -- the verifier could not run "
                f"({type(exc).__name__}: {exc}). The skill itself is saved."
            )
        else:
            if check.ok:
                notes += (
                    f"\nTool: verified and installed at {check.promoted_path}. "
                    f"Tools declared: {', '.join(check.tools_found) or 'none'}. "
                    "It becomes callable after the plugin registry reloads."
                )
                for warning in check.warnings:
                    notes += f"\n  warning: {warning}"
            else:
                # The skill is still worth keeping -- a failed tool does not invalidate the prose -- but the
                # failure is reported in full so the next attempt can fix it rather than repeat it.
                notes += (
                    f"\nTool: NOT installed -- it failed the smoke test and is quarantined at "
                    f"{check.staged_path}. The skill itself is saved."
                )
                for error in check.errors:
                    notes += f"\n  error: {error}"

    return f"Saved skill {name!r} ({scope}) to {skill_md}.{notes}", False


def _find_shadowed(name: str, target: Path) -> Path | None:
    """An existing skill with this name that this save would take precedence over."""
    for directory in (BUILTIN_SKILLS_DIR, USER_SKILLS_DIR, Path.cwd() / PROJECT_SKILLS_SUBDIR):
        candidate = directory / name / "SKILL.md"
        if candidate.is_file() and candidate != target:
            return candidate
    return None
