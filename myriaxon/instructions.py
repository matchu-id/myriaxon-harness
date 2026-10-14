"""Instruction files: what loads, from where, in what order, and what it costs.

The instruction layer runs on **every single turn**, and it had three defects that this module fixes.

## What was wrong

**1. Nested `AGENTS.md` files were never read.** The old loader read `<root>/AGENTS.md` and stopped. In a
monorepo, a per-package `AGENTS.md` was invisible. This is the same defect that is an enterprise blocker for
Codex ([#12115](https://github.com/openai/codex/issues/12115)) — the difference is that Codex at least documents
its precedence. Here precedence was not defined because there was nothing to order.

**2. There was no size bound and no warning.** Every byte was injected into the system prompt on every turn.
Anthropic's own guidance is explicit that this backfires — "target under 200 lines per CLAUDE.md file. Longer
files consume more context and *reduce adherence*" — and their auto-memory is truncating-loaded. A 5,000-line
file silently degraded every turn of every session with no signal anywhere.

**3. Nothing surfaced which files had loaded.** Two separate upstream requests for exactly this were closed
`not_planned` (Codex [#8547](https://github.com/openai/codex/issues/8547)). A user cannot audit an instruction
layer they cannot see, and they cannot debug "the agent ignored my rule" if they do not know the rule never
loaded.

## The three decisions that matter

**Precedence is defined, not emergent.** Files are ordered outermost (user-global) to innermost (nearest
directory), and the assembled prompt says so explicitly. The model is told the later text is more specific, so
a package-level rule beats a repo-level one instead of the two silently competing. Undefined precedence is worse
than wrong precedence, because it is undebuggable.

**Nothing is silently truncated.** Over-budget content is *reported*, with the byte count, a percentage, the
remedy, and the per-turn cost. Dropping text to fit a budget changes what the agent knows without telling
anyone, which is the failure this whole module exists to remove. A warning the owner can act on is worth more
than a silent edit.

**Every source is recorded, including the ones that lost.** `InstructionSource.loaded` distinguishes "this file
is on disk but was shadowed" from "this file does not exist". Those look identical in a rendered prompt and
mean completely different things when a rule is not being followed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .constants import (
    AGENTS_MD_FILENAME,
    LEGACY_INSTRUCTIONS_FILENAME,
    PROJECT_INSTRUCTIONS_FILENAME,
    USER_INSTRUCTIONS_FILE,
)

# A line containing only "@relative/path" pulls that file's content in inline, following gemini-cli's GEMINI.md
# convention. Whole-line-only, because matching "@word" mid-sentence would fire on an email address or an @mention.
_MAX_IMPORT_DEPTH = 5

# === THE BUDGET =====================================================================================
#
# An entire instruction file is injected into the system prompt on **every turn**, so its cost is not paid once
# per session: it is paid on every request, and it competes with the actual task for the model's attention.
#
# These numbers are a recommendation, not a hard limit, and they are derived from published guidance rather than
# invented. Anthropic's Claude Code memory docs give "under 200 lines per CLAUDE.md file" and warn that "longer
# files consume more context and reduce adherence"; their auto-memory loader truncates at 200 lines / 25 KB. The
# character figure below is that same order of magnitude expressed in bytes, and the line figure matches theirs.
#
# Deliberately advisory. Refusing to load a large file would break working setups, and silently truncating it
# would change what the agent knows without telling anyone -- the exact class of failure this repository keeps
# finding. Over budget is therefore *loud*, never destructive.
RECOMMENDED_CHARS = 8_000
RECOMMENDED_LINES = 200
# Past this, adherence damage is severe enough that the warning escalates from "consider" to "this is hurting".
HARD_CHARS = 24_000

# Rough token estimate for the per-turn cost figure. Same ~4-chars/token heuristic the context budget code uses,
# and labelled as an estimate everywhere it is shown.
CHARS_PER_TOKEN = 4


@dataclass
class InstructionSource:
    """One candidate instruction file and what happened to it."""

    path: Path
    kind: str          # "user" | "project" | "agents" | "nested"
    loaded: bool
    chars: int = 0
    lines: int = 0
    depth: int = 0     # 0 = user-global/root, higher = closer to cwd
    note: str = ""     # why it was skipped, when it was

    @property
    def est_tokens(self) -> int:
        return self.chars // CHARS_PER_TOKEN if self.chars else 0

    def render(self) -> str:
        if self.loaded:
            return f"  loaded  [{self.kind}] {self.path}  ({self.chars:,} chars, ~{self.est_tokens:,} tokens)"
        return f"  skipped [{self.kind}] {self.path}  ({self.note})"


@dataclass
class InstructionSet:
    """Everything the instruction layer loaded, plus what it costs and what is wrong with it."""

    text: str = ""
    sources: list[InstructionSource] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def loaded(self) -> list[InstructionSource]:
        return [s for s in self.sources if s.loaded]

    @property
    def total_chars(self) -> int:
        return sum(s.chars for s in self.loaded)

    @property
    def total_lines(self) -> int:
        return sum(s.lines for s in self.loaded)

    @property
    def est_tokens(self) -> int:
        return self.total_chars // CHARS_PER_TOKEN

    @property
    def over_budget(self) -> bool:
        return self.total_chars > RECOMMENDED_CHARS or self.total_lines > RECOMMENDED_LINES

    def render(self) -> str:
        """A full report: what loaded, what was skipped, and the cost."""
        if not self.sources:
            return "  No instruction files found (checked MYRIAXON.md, IOH.md, AGENTS.md, and nested AGENTS.md)."
        lines = [source.render() for source in self.sources]
        lines.append("")
        lines.append(
            f"  total: {self.total_chars:,} chars, {self.total_lines:,} lines, "
            f"~{self.est_tokens:,} tokens per turn"
        )
        return "\n".join(lines)


def _expand_imports(text: str, base_dir: Path, depth: int = 0, seen: frozenset[Path] = frozenset()) -> str:
    if depth > _MAX_IMPORT_DEPTH:
        return text

    out_lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if not (stripped.startswith("@") and len(stripped) > 1 and " " not in stripped):
            out_lines.append(line)
            continue

        try:
            import_path = (base_dir / stripped[1:]).resolve()
        except OSError:
            out_lines.append(line)
            continue
        if import_path in seen or not import_path.is_file():
            out_lines.append(line)  # broken or cyclic import -- leave the literal "@path" as-is
            continue
        try:
            imported_text = import_path.read_text(encoding="utf-8-sig")
        except OSError:
            out_lines.append(line)
            continue

        expanded = _expand_imports(imported_text, import_path.parent, depth + 1, seen | {import_path})
        out_lines.append(f"--- Imported from {import_path.name} ---\n{expanded}")

    return "\n".join(out_lines)


def _read(path: Path) -> tuple[str, str]:
    """Read one instruction file, expanding `@imports`. Returns `(text, note)`; note is set when skipped."""
    if not path.is_file():
        return "", "not present"
    try:
        raw = path.read_text(encoding="utf-8-sig").strip()
    except OSError as exc:
        return "", f"unreadable ({type(exc).__name__})"
    if not raw:
        return "", "empty"
    return _expand_imports(raw, path.parent, seen=frozenset({path.resolve()})), ""


def _first_existing(candidates: list[Path]) -> Path | None:
    """The first candidate that exists. Keeps a renamed convention working rather than starting blind."""
    for path in candidates:
        if path.is_file():
            return path
    return None


def _walk_for_nested_agents(root: Path, cwd: Path) -> list[Path]:
    """Every `AGENTS.md` between `root` (exclusive) and `cwd` (inclusive), outermost first.

    The walk starts at the filesystem root and descends to `cwd`, then filters to those strictly inside the
    project. Doing it this way, rather than walking up from `cwd` and reversing, means the order is already
    correct and a repository nested inside another one still gets its own files ahead of the outer one's.

    `root` itself is excluded because the root-level `AGENTS.md` is handled separately as a named source -- it
    is a *project* file, not a nested one, and listing it twice would double-count its cost in the budget report.
    """
    try:
        cwd = cwd.resolve()
        root = root.resolve()
    except OSError:
        return []
    if not cwd.is_relative_to(root):
        # Ran from outside the project (a subdirectory passed via -p, or a different drive). There is no
        # sensible nesting to report, so report none rather than guessing.
        return []

    chain: list[Path] = []
    node = cwd
    while True:
        chain.append(node)
        if node == root or node.parent == node:
            break
        node = node.parent
    # chain is cwd -> ... -> root; reverse for outermost-first, then drop the root entry.
    found: list[Path] = []
    for directory in reversed(chain):
        if directory == root:
            continue
        candidate = directory / AGENTS_MD_FILENAME
        if candidate.is_file():
            found.append(candidate)
    return found


def load_instructions(project_root: Path | None = None, cwd: Path | None = None) -> InstructionSet:
    """Assemble the instruction layer, recording every source and every problem.

    This is the function the CLI should call. `load_project_instructions` remains as a thin wrapper that
    returns only the text, for the call sites that do not need the report.
    """
    root = (project_root if project_root is not None else Path.cwd())
    here = (cwd if cwd is not None else Path.cwd())
    sources: list[InstructionSource] = []
    parts: list[tuple[str, str]] = []  # (heading, text), in precedence order

    # ---- 1. user-global, lowest precedence
    user_path = _first_existing([
        USER_INSTRUCTIONS_FILE,
        USER_INSTRUCTIONS_FILE.with_name(LEGACY_INSTRUCTIONS_FILENAME),
    ])
    if user_path is None:
        sources.append(InstructionSource(USER_INSTRUCTIONS_FILE, "user", False, note="not present"))
    else:
        text, note = _read(user_path)
        sources.append(InstructionSource(
            user_path, "user", bool(text), chars=len(text),
            lines=text.count("\n") + 1 if text else 0, note=note,
        ))
        if text:
            parts.append(("User instructions (global, lowest precedence)", text))

    # ---- 2. project root
    project_path = _first_existing([
        root / PROJECT_INSTRUCTIONS_FILENAME,
        root / LEGACY_INSTRUCTIONS_FILENAME,
    ])
    if project_path is None:
        sources.append(InstructionSource(root / PROJECT_INSTRUCTIONS_FILENAME, "project", False,
                                         note="not present"))
    else:
        text, note = _read(project_path)
        sources.append(InstructionSource(
            project_path, "project", bool(text), chars=len(text),
            lines=text.count("\n") + 1 if text else 0, note=note,
        ))
        if text:
            parts.append(("Project instructions", text))

    # ---- 3. root AGENTS.md (the cross-tool convention)
    agents_path = root / AGENTS_MD_FILENAME
    agents_text, agents_note = _read(agents_path)
    sources.append(InstructionSource(
        agents_path, "agents", bool(agents_text), chars=len(agents_text),
        lines=agents_text.count("\n") + 1 if agents_text else 0, note=agents_note,
    ))
    if agents_text:
        parts.append(("Project instructions (AGENTS.md)", agents_text))

    # ---- 4. nested AGENTS.md, deepest wins, so these come last
    nested = list(_walk_for_nested_agents(root, here))
    if not nested:
        sources.append(InstructionSource(
            here / AGENTS_MD_FILENAME, "nested", False,
            note="none between the project root and the working directory" if here != root
                 else "working directory is the project root",
        ))
    for depth, path in enumerate(nested, start=1):
        text, note = _read(path)
        sources.append(InstructionSource(
            path, "nested", bool(text), chars=len(text),
            lines=text.count("\n") + 1 if text else 0, depth=depth, note=note,
        ))
        if text:
            try:
                shown = path.parent.relative_to(root)
            except ValueError:
                shown = path.parent
            parts.append((f"Instructions for {shown} (more specific, overrides the above)", text))

    # ---- assemble
    if not parts:
        return InstructionSet(text="", sources=sources, warnings=[])

    if len(parts) > 1:
        preamble = (
            "Instruction sources are listed least specific first. Where two disagree, the later and more "
            "specific one wins."
        )
        rendered = [preamble]
    else:
        rendered = []
    for heading, text in parts:
        rendered.append(f"--- {heading} ---\n{text}")
    text = "\n\n".join(rendered)

    result = InstructionSet(text=text, sources=sources)
    result.warnings = _budget_warnings(result)
    return result


def _budget_warnings(result: InstructionSet) -> list[str]:
    """Report an oversized instruction layer rather than trimming it.

    Returns human-readable warnings. An empty list means the layer is within the recommended budget.
    """
    warnings: list[str] = []
    chars, lines = result.total_chars, result.total_lines
    if chars == 0:
        return warnings

    if chars >= HARD_CHARS:
        warnings.append(
            f"Instruction files total {chars:,} chars (~{result.est_tokens:,} tokens) and are injected on "
            f"every turn. Published guidance puts the point where adherence measurably drops far below this. "
            "Move detail into a skill (save_skill) or a file pulled in on demand, and keep the instructions to "
            "rules the agent must always have."
        )
    elif chars > RECOMMENDED_CHARS or lines > RECOMMENDED_LINES:
        over = []
        if chars > RECOMMENDED_CHARS:
            over.append(f"{chars:,} chars vs ~{RECOMMENDED_CHARS:,} recommended")
        if lines > RECOMMENDED_LINES:
            over.append(f"{lines:,} lines vs ~{RECOMMENDED_LINES} recommended")
        warnings.append(
            "Instruction files are larger than recommended (" + "; ".join(over) + "). "
            "Longer instruction files consume more context and reduce adherence, so this is a correctness "
            "cost, not just a token cost. Consider `@path` imports to split by topic, or save_skill for "
            "anything procedural that is not needed on every turn."
        )

    # Name the single largest contributor, because "your instructions are big" is not actionable but
    # "this file is 80% of them" is.
    biggest = max(result.loaded, key=lambda s: s.chars, default=None)
    if biggest is not None and chars and biggest.chars / chars > 0.6 and len(result.loaded) > 1:
        warnings.append(
            f"{biggest.path} accounts for {biggest.chars * 100 // chars}% of the instruction layer "
            f"({biggest.chars:,} of {chars:,} chars)."
        )
    return warnings


def load_project_instructions(project_root: Path | None = None) -> str:
    """The assembled instruction text, for call sites that do not need the provenance report.

    Kept as the compatibility entry point. New code that wants to *report* on the instruction layer should call
    `load_instructions` and use the `InstructionSet` it returns.
    """
    return load_instructions(project_root=project_root).text
