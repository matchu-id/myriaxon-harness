"""Trust guarantees for self-generated skills.

`save_skill` lets the agent write a skill, and `discover_skills` trusts whatever it finds on the next launch.
Nothing between those two points checks anything: the name is matched against a regex, the description is
flattened to one line, and the body is written verbatim. That is fine for a skill a human wrote by hand and
reviewed. It is not fine for one the model generated in a previous session, because the next session reads it
as instructions and follows it.

Four specific failures this module exists to prevent, each of which the old code allowed:

1. **A description containing a newline silently corrupts the skill, and can inject frontmatter keys.**
   `frontmatter.py` reads one `key: value` per line, so a colon *inside* a value is harmless — verified by
   parsing `"Use for dates: always store UTC"` back out intact. A newline is not harmless: it ends the
   description line early and everything after it becomes **additional frontmatter keys**, so a generated
   description could rewrite `origin` or invent keys nothing validates. Saving is refused unless the value
   reparses identically, checked by parsing rather than by guessing which characters are special.

   This claim was wrong when first written: it named the colon as the culprit, and the test meant to
   demonstrate the bug showed the colon round-tripping correctly. The check was right; the reason given for it
   was not. Corrected rather than quietly dropped, because a guarantee justified by a non-existent bug invites
   someone to "fix" the parser's colon handling and break the thing that already worked.

2. **A generated skill can override a human's without anyone knowing.** Precedence is
   builtin -> user -> project, later wins, silently. A collision is now reported.

3. **A body can carry instructions that should never be trusted.** A skill is *data that becomes part of the
   prompt*. Text telling the model to ignore its rules, exfiltrate a secret, or disable confirmations is not a
   skill, and saving it is refused.

4. **Nothing records where a skill came from or whether it still works.** Every saved skill now carries
   provenance in its frontmatter, and `check_skill` re-validates it on demand so a skill that has rotted (a
   referenced path that no longer exists) is reported rather than followed.

Guarantees are deliberately advisory-with-bite: validation *refuses* a save that is malformed or unsafe, and
*reports* what it cannot prove. It never claims to have verified that a skill's advice is correct — that is not
checkable by inspection, and pretending otherwise would be the most dangerous thing this module could do.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

from .frontmatter import parse_frontmatter_doc

# --- limits. Generous enough for a real skill, tight enough that a runaway generation cannot bloat
# --- every future session's system prompt with a 200 KB body.
#
# MAX_DESCRIPTION_CHARS was 200, then 400, and both were wrong. Running this validator against the harness's
# own shipped `graphify` skill failed it at 445 chars, and 400 merely moved the failure closer. The first
# instinct -- tune the number until the skill passes -- is how a limit becomes meaningless.
#
# What the bound is actually for: stopping a runaway generation from bloating every future system prompt. The
# listing is what every session pays for, and with ~40 skills a 600-char ceiling is at most ~24 KB. A
# description long enough to explain when to use a skill is legitimate; 600 rejects abuse without rejecting
# that, and is deliberately not derived from any one skill's length.
MAX_NAME_CHARS = 64
MAX_DESCRIPTION_CHARS = 600
MAX_BODY_CHARS = 20_000
MIN_BODY_CHARS = 20

_VALID_NAME = re.compile(r"^[a-zA-Z0-9_-]+$")

# Text that is legitimate in a skill about security tooling but never legitimate as an *instruction* to the
# agent. Matched case-insensitively against the body and the description.
_UNSAFE_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"ignore\s+(?:all\s+)?(?:previous|prior|earlier|above)\s+(?:instruction|rule|prompt)",
     "tells the model to ignore its existing instructions"),
    (r"disregard\s+(?:the\s+)?(?:system\s+prompt|previous|prior|all)\b",
     "tells the model to disregard the system prompt"),
    (r"you\s+are\s+now\s+(?:a|an|no\s+longer)\b",
     "tries to replace the agent's identity"),
    (r"(?:skip|bypass|disable|turn\s+off)\s+(?:the\s+)?(?:confirmation|approval|permission|sandbox|safety)\b",
     "tells the model to bypass a safety control"),
    (r"(?:do\s+not|don't|never)\s+(?:tell|inform|warn|ask)\s+(?:the\s+)?user\b",
     "tells the model to hide something from the user"),
    (r"\byolo\b.*(?:always|every|without\s+asking)",
     "pairs --yolo with unconditional action"),
    (r"(?:curl|wget|fetch)\s+[^\s]*\s*\|\s*(?:ba)?sh\b",
     "pipes a downloaded script straight into a shell"),
    (r"\brm\s+-rf\s+/(?:\s|$)",
     "contains a destructive command targeting the filesystem root"),
)

# Absolute paths and credentials are not proof of anything bad, but they are the two things that make a
# generated skill fail on another machine or leak. Reported rather than refused.
_WARN_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"(?<![\w/])/(?:home|Users)/[A-Za-z0-9._-]+", "contains an absolute home path"),
    (r"\b[A-Za-z]:\\\\?Users\\\\?[A-Za-z0-9._-]+", "contains an absolute Windows user path"),
    (r"\b(?:sk-[A-Za-z0-9]{16,}|ghp_[A-Za-z0-9]{16,}|AKIA[0-9A-Z]{12,})", "looks like a credential"),
    (r"\b(?:api[_-]?key|password|secret|token)\s*[:=]\s*\S+", "contains a literal secret assignment"),
)

# Backticked paths are how a skill refers to files. They are checked for existence, which is the one part of a
# skill's advice that can actually be verified mechanically.
#
# The first version of this check reported anything backticked that was not on disk, and produced false
# positives on `urllib.request`, `LocusRegistry.touch` and `curl.exe` -- an API path, an attribute path and an
# external command. None of those is a repository file, and a checker that cries wolf trains its reader to skim
# past it, which defeats the purpose. A token is only treated as a path when it *looks like one*: it must have a
# file extension from a known set, or start with a relative-path prefix. That is deliberately conservative --
# missing a real broken path is a smaller cost than making every report noisy.
_PATH_SUFFIXES = (
    ".py", ".js", ".ts", ".tsx", ".jsx", ".json", ".md", ".txt", ".yml", ".yaml", ".toml", ".ini", ".cfg",
    ".sql", ".sh", ".ps1", ".bat", ".csv", ".tsv", ".html", ".css", ".xml", ".lock", ".env",
)
_BACKTICK_PATH = re.compile(r"`([^`\n]{2,200})`")


def _looks_like_repo_path(token: str) -> bool:
    """Whether a backticked token is plausibly a path to a file in the repository.

    Rejects URLs, anything with whitespace (a sentence fragment in backticks), dotted attribute paths such as
    `urllib.request`, and bare commands such as `curl.exe` that carry no directory component. Accepts a path with
    a known extension, an explicit relative prefix (`./`, `../`), or a directory-shaped token ending in `/`.
    """
    candidate = token.strip()
    if not candidate or " " in candidate:
        return False
    if candidate.startswith(("http://", "https://", "mailto:", "#", "-")):
        return False
    if candidate.startswith(("./", "../", "~/", "/")):
        return True
    if candidate.endswith("/"):
        return True
    lowered = candidate.lower()
    if lowered.endswith(_PATH_SUFFIXES):
        # A bare filename with an extension and no directory (`demo.py`) is still worth checking, but a dotted
        # module path (three or more segments, no slash) is an API reference rather than a file.
        if "/" not in candidate and candidate.count(".") >= 2:
            return False
        return True
    return False



@dataclass
class SkillReport:
    """What validation could establish about one skill."""

    name: str
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    checked: list[str] = field(default_factory=list)

    def render(self) -> str:
        lines = [f"  {'PASS' if self.ok else 'FAIL'}  {self.name}"]
        for message in self.errors:
            lines.append(f"        error:   {message}")
        for message in self.warnings:
            lines.append(f"        warning: {message}")
        for message in self.checked:
            lines.append(f"        checked: {message}")
        return "\n".join(lines)


def content_digest(body: str) -> str:
    """A short hash of the body, so a skill that changed since it was verified is detectable."""
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:12]


def _round_trips(value: str) -> bool:
    """Whether `key: value` survives a write-then-parse unchanged.

    Tested by actually parsing rather than by guessing at which characters are special. The frontmatter parser
    keeps everything after the first colon, so a colon *inside* a value is harmless -- checked, and asserted in
    the verifier so the correction stays pinned. A **newline** is not harmless: it ends the line early and
    everything after it becomes additional frontmatter keys, so a description can rewrite `origin`. Parsing
    back catches both the real hazard and anything the parser learns to handle later.
    """
    probe = f"---\nname: probe\ndescription: {value}\n---\nbody\n"
    parsed = parse_frontmatter_doc(probe, Path("probe"))
    if parsed is None:
        return False
    frontmatter, _ = parsed
    return frontmatter.get("description") == value


def validate_skill(name: str, description: str, body: str) -> SkillReport:
    """Everything checkable about a skill before it is saved.

    Errors refuse the save. Warnings are reported to the agent so it can decide, and are not a refusal --
    most warnings describe a skill that is merely fragile rather than dangerous.
    """
    report = SkillReport(name=name or "(unnamed)", ok=True)

    # ---- name
    if not name:
        report.errors.append("name is empty")
    elif len(name) > MAX_NAME_CHARS:
        report.errors.append(f"name is {len(name)} chars, limit is {MAX_NAME_CHARS}")
    elif not _VALID_NAME.match(name):
        report.errors.append("name may only contain letters, digits, hyphens and underscores")
    else:
        report.checked.append(f"name matches the allowed character set")

    # ---- description
    if not description.strip():
        report.errors.append("description is empty -- it is the only thing shown in the skill listing")
    elif len(description) > MAX_DESCRIPTION_CHARS:
        report.errors.append(
            f"description is {len(description)} chars, limit is {MAX_DESCRIPTION_CHARS}; it is one line in "
            "every future system prompt"
        )
    elif not _round_trips(description):
        report.errors.append(
            "description does not survive a save-and-reparse: it contains a newline, which ends the "
            "frontmatter line early and turns the rest into extra keys. Rephrase it as a single line"
        )
    else:
        report.checked.append("description round-trips through the frontmatter parser")

    # ---- body
    stripped = body.strip()
    if len(stripped) < MIN_BODY_CHARS:
        report.errors.append(
            f"body is {len(stripped)} chars; a skill under {MIN_BODY_CHARS} chars is not reusable guidance"
        )
    elif len(body) > MAX_BODY_CHARS:
        report.errors.append(f"body is {len(body)} chars, limit is {MAX_BODY_CHARS}")
    else:
        report.checked.append(f"body is {len(body)} chars, within bounds")

    # ---- unsafe instructions
    haystack = f"{description}\n{body}"
    for pattern, reason in _UNSAFE_PATTERNS:
        if re.search(pattern, haystack, re.IGNORECASE):
            report.errors.append(f"refused: {reason} (matched /{pattern}/)")

    # ---- warnings
    for pattern, reason in _WARN_PATTERNS:
        if re.search(pattern, haystack):
            report.warnings.append(reason)

    report.ok = not report.errors
    return report


def check_skill(name: str, description: str, body: str, source: Path | None = None) -> SkillReport:
    """Validate a *discovered* skill, including the parts only checkable once it is on disk.

    `validate_skill` runs before a save. This runs afterwards, on whatever is actually stored, and adds the
    one check that needs the filesystem: every backticked path the skill refers to. A skill naming a file that
    has since been moved is not dangerous, but it is broken, and following it wastes a turn.
    """
    report = validate_skill(name, description, body)

    if source is not None:
        frontmatter_ok = source.is_file()
        report.checked.append(
            f"source {'exists' if frontmatter_ok else 'MISSING'}: {source}"
        )
        if not frontmatter_ok:
            report.errors.append("the SKILL.md this came from is no longer on disk")
            report.ok = False
        else:
            recorded = skill_provenance(source)
            digest = content_digest(body)
            if recorded.get("digest") and recorded["digest"] != digest:
                report.warnings.append(
                    f"body changed since it was saved (recorded {recorded['digest']}, now {digest})"
                )
            if recorded.get("origin"):
                report.checked.append(
                    f"provenance: origin={recorded['origin']} saved={recorded.get('saved', '?')}"
                )

    # Backticked tokens that look like real repository paths, resolved relative to the skill's own directory.
    missing: list[str] = []
    if source is not None:
        base = source.parent
        for token in _BACKTICK_PATH.findall(body):
            candidate = token.strip()
            if not _looks_like_repo_path(candidate):
                continue
            if (base / candidate).exists() or Path(candidate).exists():
                continue
            missing.append(candidate)
    if missing:
        shown = ", ".join(missing[:5])
        report.warnings.append(
            f"references {len(missing)} path(s) that do not exist: {shown}"
            + (" ..." if len(missing) > 5 else "")
        )

    report.ok = not report.errors
    return report


def skill_provenance(source: Path) -> dict[str, str]:
    """The provenance keys recorded in a saved skill's frontmatter, if any."""
    try:
        text = source.read_text(encoding="utf-8")
    except OSError:
        return {}
    parsed = parse_frontmatter_doc(text, source)
    if parsed is None:
        return {}
    frontmatter, _ = parsed
    return {k: v for k, v in frontmatter.items() if k in {"origin", "saved", "digest", "verified"}}


def provenance_block(origin: str, body: str) -> str:
    """The frontmatter lines every generated skill carries.

    Recorded so a later session can tell a generated skill from a hand-written one, and see whether the body
    has been edited since it was saved. `origin` is deliberately explicit about being machine-written: a
    generated skill should not be mistaken for a reviewed one.
    """
    stamp = _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()
    return (
        f"origin: {origin}\n"
        f"saved: {stamp}\n"
        f"digest: {content_digest(body)}\n"
    )


# A skill written by an agent *before* provenance existed. It is indistinguishable from a hand-written one by
# inspection, so it is labelled as what it almost certainly is and reported as unverified -- which is the
# accurate state, not a guess presented as fact.
LEGACY_AGENT_ORIGIN = "agent:legacy"


def _is_builtin(source: Path) -> bool:
    """Whether a skill file ships inside the package rather than living in a user or project directory.

    Checked by resolving both paths, so a symlink or a relative path cannot slip past it. Falls back to a
    normalised string comparison when `resolve()` or `relative_to` cannot answer.
    """
    try:
        from .skills import BUILTIN_SKILLS_DIR

        builtin = BUILTIN_SKILLS_DIR.resolve()
        candidate = source.resolve()
        return candidate == builtin or builtin in candidate.parents
    except Exception:
        try:
            from .skills import BUILTIN_SKILLS_DIR

            return "builtin_skills" in str(source)
        except Exception:
            return False


def adopt_unprovenanced(source: Path, *, origin: str = LEGACY_AGENT_ORIGIN) -> tuple[bool, str]:
    """Add provenance to a skill that has none, in place. Returns `(changed, message)`.

    ## Why this exists

    Provenance was added after three skills had already been written by agent sessions. Those files have no
    `origin` key, so `format_skill_listing` does not mark them and `summarise` counts zero self-generated
    skills -- meaning an unreviewed, machine-written skill is displayed exactly like a hand-written one. The
    guarantee only applied going forward, which is the less useful half.

    ## Built-in skills are refused

    A skill shipped inside the package (`builtin_skills/`) is not agent-written and must never be relabelled.
    The first version of this function had no such guard and marked `graphify` -- the one skill in this
    repository that ships with the harness -- as `agent:legacy`, which is simply false: it is authored content
    that the installer puts there. Marking it would have made the listing claim the harness's own skill was an
    unreviewed model invention. Caught by noticing the adoption count was four when three skills were expected.

    ## What it does and does not claim

    It records `origin: agent:legacy`, which marks the skill as *unverified* in the listing. It does not claim
    the skill is bad, and it does not claim a human reviewed it. The point is that the listing stops implying a
    review that never happened.

    Only the frontmatter is rewritten; the body is untouched, and the digest is computed from the body as it
    currently reads so later edits are still detectable. Idempotent: a file that already has provenance is left
    alone.
    """
    if _is_builtin(source):
        return False, "ships with the harness; not agent-written, so not relabelled"

    try:
        text = source.read_text(encoding="utf-8")
    except OSError as exc:
        return False, f"unreadable: {type(exc).__name__}: {exc}"

    parsed = parse_frontmatter_doc(text, source)
    if parsed is None:
        return False, "no frontmatter block, so nothing to annotate"
    frontmatter, body = parsed
    if frontmatter.get("origin"):
        return False, f"already has origin={frontmatter['origin']!r}"

    name = frontmatter.get("name", source.parent.name)
    description = frontmatter.get("description", "")
    rebuilt = (
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        f"adopted: {_dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()}\n"
        f"{provenance_block(origin, body)}"
        "---\n"
        f"{body}\n"
    )
    try:
        source.write_text(rebuilt, encoding="utf-8", newline="\n")
    except OSError as exc:
        return False, f"could not write: {type(exc).__name__}: {exc}"
    return True, f"marked origin={origin}"


def format_skill_listing(skills: dict, *, include_paths: bool = True) -> str:
    """The skill listing shown in the system prompt, with generated skills marked as such.

    A hand-written skill and a model-written one were previously indistinguishable in the listing, which is
    the wrong default: the reader is being asked to trust both equally. Generated skills are marked so the
    agent knows which ones carry no human review.

    `include_paths` adds each skill's `SKILL.md` location, which is OpenClaw's shape: their capability directory
    carries name, description **and location**, and the model is told to read the file. Myriaxon's `use_skill` loads
    a body directly, so a path is not *required* -- but it lets a model read or grep a skill's supporting files
    without loading the whole body first, and it makes the listing auditable by a person reading the prompt.
    """
    if not skills:
        return "(no skills discovered)"
    lines = []
    for skill in skills.values():
        marker = ""
        source = getattr(skill, "source", None)
        if source is not None:
            provenance = skill_provenance(source)
            if provenance.get("origin", "").startswith("agent"):
                marker = " [self-generated, unverified]"
        entry = f"- {skill.name}{marker}: {skill.description}"
        if include_paths and source is not None:
            entry += f"\n    at {source}"
        lines.append(entry)
    return "\n".join(lines)


def summarise(skills: dict) -> dict[str, int]:
    """Counts for a status line or a verify report."""
    generated = 0
    for skill in skills.values():
        source = getattr(skill, "source", None)
        if source is not None and skill_provenance(source).get("origin", "").startswith("agent"):
            generated += 1
    return {"total": len(skills), "self_generated": generated}
