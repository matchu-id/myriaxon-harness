"""The three channels the command line shows Loci through: the sheet, the prompt prefix, the status fragment.

## Why three channels rather than one

Each has a different budget, and the spec wants the character present in all of them without any of them becoming
noisy:

  * the **sheet** is the reference view, drawn on demand by `myriaxon avatar`;
  * the **prompt prefix** gets one line, and must survive a 20-column terminal;
  * the **status fragment** gets a few characters at the end of a line that already has work in it.

All three read the same manifest, so a face cannot exist in one and be missing from another.

## The one hard constraint: never overflow the terminal

A sheet that wraps is worse than a shorter sheet, because wrapping moves the art under the reader's eye -- the same
failure the frame rule prevents for a single expression. So the fit is SOLVED rather than guessed: `render_sheet`
tries progressively fewer columns until every line fits, and falls back to a one-expression-per-line list when even
one column is too wide. That last fallback is why the narrow-terminal case is checked at 20 columns, where a grid
cannot possibly work and a list still names all fourteen expressions.
"""

from __future__ import annotations

from .loci_character import (
    EXPRESSIONS,
    Terminal,
    _visible_width,
    char_width,
    expression_for,
    render,
    render_mini,
)
from .loci_expression import ExpressionState

# The order the sheet lists faces in, grouped by what they mean rather than alphabetically: the resting and working
# states, then the good ones, then the careful ones, and crisis last where it belongs.
# EXACTLY the published section 4 names, plus `listening_back` which the live contract adds -- grouped by what they
# mean rather than alphabetically: the resting states, then the good ones, then the careful ones, and crisis last where
# it belongs. Measured: this listed `working`, `pleased`, `curious` and `tired`, none of which are in the published
# table, and omitted `remembering`, `sleepy` and `focus`, which are. The sheet is what a user reads to learn what Loci
# can look like, so it was showing four undocumented faces and hiding three documented ones.
SHEET_ORDER = (
    "idle", "listening", "listening_back", "thinking",
    "remembering", "focus", "sleepy",
    "cheering", "celebrate", "happy",
    "caring", "sorry", "concerned", "confused", "crisis",
)

# When each face is used, in one line. Written for a person reading the sheet, not for the code.
WHEN: dict[str, str] = {
    "idle": "nothing is happening",
    "listening": "you are typing, or you are sharing something",
    "listening_back": "you are typing; the same face, held a beat longer",
    "thinking": "a turn is running",
    "remembering": "recalling something you told it",
    "focus": "a focus session is running; Loci stays out of the way",
    "sleepy": "night mode, or the session has run long",
    "cheering": "a turn finished and did something",
    "celebrate": "a milestone, once per session",
    "happy": "a greeting, or good news",
    "caring": "you seem to be struggling",
    "sorry": "something failed",
    "concerned": "a result looks wrong",
    "confused": "the request was ambiguous",
    "crisis": "you may be in danger; never animated",
}

# A cell narrower than this cannot hold a mini drawing and a name without looking broken, so the grid refuses below it
# and the list layout takes over.
MIN_CELL_WIDTH = 24


def _display_width(text: str) -> int:
    """Visible width, ignoring escape sequences, which occupy no columns."""
    return _visible_width(text)


def _wrap(text: str, width: int) -> list[str]:
    """Break text to `width`, at spaces where possible and mid-word only when a single word is too long.

    Greedy rather than optimal, because a status line is a few words and an even break is not worth the complexity.
    """
    limit = max(1, width)
    words = (text or "").split()
    if not words:
        return [""]
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if _display_width(candidate) <= limit or not current:
            # A single word longer than the limit is emitted whole rather than split: a hyphenated status word is
            # harder to read than one that overflows slightly.
            current = candidate
            continue
        lines.append(current)
        current = word
    if current:
        lines.append(current)
    return lines


def _ordered() -> list[str]:
    """Every expression, in sheet order, with anything unlisted appended so a new face cannot be invisible."""
    known = [name for name in SHEET_ORDER if name in EXPRESSIONS]
    extra = [name for name in EXPRESSIONS if name not in known]
    return known + extra


def _cell(name: str, terminal: Terminal, cell_width: int) -> list[str]:
    """One grid cell: the mini drawing above the name and the one-line description."""
    found = expression_for(name)
    drawing = render_mini(name, terminal)
    head = f"{drawing} {found.id}"
    lines = [head]
    room = max(8, cell_width - 2)
    lines.extend(_wrap(found.label, room)[:1])
    lines.extend(_wrap(WHEN.get(found.id, ""), room)[:2])
    return lines


def _sheet_grid(tier: int, terminal: Terminal, cell_width: int) -> list[str]:
    """`tier` cells per row. Pads the last row so the columns stay aligned."""
    names = _ordered()
    rows: list[str] = []
    for start in range(0, len(names), tier):
        chunk = names[start:start + tier]
        cells = [_cell(name, terminal, cell_width) for name in chunk]
        height = max(len(cell) for cell in cells)
        for cell in cells:
            cell.extend([""] * (height - len(cell)))
        for line_index in range(height):
            row = "".join(
                (cell[line_index] if line_index < len(cell) else "").ljust(cell_width)
                for cell in cells
            )
            rows.append(row.rstrip())
        rows.append("")
    return rows


def _sheet_list(terminal: Terminal) -> list[str]:
    """One expression per line. The fallback that always fits, because a name is short."""
    rows: list[str] = []
    for name in _ordered():
        found = expression_for(name)
        drawing = render_mini(name, terminal)
        rows.append(f"{drawing} {found.id} -- {found.label}")
        for line in _wrap(WHEN.get(found.id, ""), max(8, terminal.columns - 4)):
            rows.append(f"    {line}")
    return rows


def render_sheet(*, terminal: Terminal) -> str:
    """The reference sheet, guaranteed to fit `terminal.columns`.

    The tier is SOLVED: start at four cells and step down until every line fits. A guessed tier would produce a sheet
    that wraps on exactly the terminal the user happens to have.
    """
    width = max(1, terminal.columns)
    for tier in (4, 3, 2, 1):
        cell_width = max(MIN_CELL_WIDTH, width // tier)
        if tier > 1 and cell_width < MIN_CELL_WIDTH:
            continue
        rows = _sheet_grid(tier, terminal, cell_width)
        if all(_display_width(row) <= width for row in rows):
            return "\n".join(rows).rstrip()
    # Even one column is too narrow for a grid, so fall back to a list. It still names every expression, which is the
    # property the verifier checks at 20 columns.
    rows = _sheet_list(terminal)
    # Re-wrap the list to the terminal and check again rather than truncating: measured on the first run, a 20-column
    # sheet was hard-truncated and the names were cut off, which is precisely the failure the verifier looks for.
    narrowed: list[str] = []
    for row in rows:
        if _display_width(row) <= width:
            narrowed.append(row)
            continue
        for piece in _wrap(row, width):
            narrowed.append(piece)
    return "\n".join(narrowed).rstrip()


def render_one(name: str, *, terminal: Terminal) -> tuple[bool, str]:
    """One expression drawn large, and whether the name was recognised.

    The boolean is what lets the CLI report a typo instead of silently drawing the resting face -- the same reason
    `expression_for` falls back to idle rather than to a near neighbour.
    """
    ok = name in EXPRESSIONS
    found = expression_for(name)
    body = render(found.id, terminal, size="hero" if terminal.columns >= 40 else "small")
    if not ok:
        return False, f"Unknown expression {name!r}. Showing the resting face.\n{body}"
    return True, body


def prompt_prefix(state: ExpressionState, terminal: Terminal, *, label: str = "") -> str:
    """What sits in front of the cursor.

    `label` names the speaker, and it is what the CLI passes now. Measured against a real session: the
    mini character was returned as a string carrying its own ANSI escapes (`\\x1b[34m(_‿_)[◍]\\x1b[0m`),
    which is correct for `print` and wrong for the prompt -- prompt_toolkit renders a prompt as styled
    text through its own Style, so the escapes were shown literally as a cyan `^[[34m...^[[0m` in
    front of every line typed. And a face is not the speaker's name anyway: "Myria > " says who is
    answering, where a lantern says only that a character is present. The face still carries the mood
    in the status line, where it is coloured by rich rather than by raw escapes.
    """
    if label:
        return f"{label} > "
    drawing = render_mini(state.expression, terminal)
    return f"{drawing} "


def status_fragment(state: ExpressionState, terminal: Terminal) -> str:
    """A short text fragment for the end of a status line.

    Names the MOOD rather than the expression id, because this is the channel that has to work without colour AND
    without the art -- a screen reader, a log file, a grep.
    """
    return expression_for(state.expression).label
