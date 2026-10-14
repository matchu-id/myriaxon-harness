"""Myriaxon's visual identity, as importable tokens.

This is the single source of truth for the brand: the palette, the type stacks, the mark's
SVG geometry, the terminal braille mark, and the display copy. The CLI banner, the Rich
theme, the syntax highlighting, the prompt_toolkit REPL style and the dashboard's CSS
custom properties are all generated from or asserted against these values, so a brand
change is one edit rather than a hunt through the codebase.

The ``brand/`` directory at the repository root holds the same identity as distributable
assets (SVG files, the palette sheet); ``tools/export_brand_assets.py`` regenerates them
from this module, so the two can never drift.
"""

from __future__ import annotations

# --------------------------------------------------------------------------------------
# Palette -- dark ground
# --------------------------------------------------------------------------------------
# The near-black is deliberately blue-tinted rather than neutral grey: against pure black
# a blue-black reads as depth, while #000 reads as a hole. The three ink steps are the
# only surfaces in the product; every panel is one of them, which is what keeps a dense
# console from turning into a patchwork.
INK = "#0A0C11"        # the page
INK_2 = "#10131A"      # raised: panels, cards, dialogs, the composer
INK_3 = "#171B24"      # raised again: nav selection, inputs, hover targets, tags
LINE = "#232835"       # every border and divider
LINE_2 = "#2E3444"     # borders that need to read as interactive (inputs, buttons, chips)

TEXT = "#EDEFF5"       # primary text on ink
TEXT_2 = "#A3ABBD"     # secondary text on ink
TEXT_3 = "#7C8599"     # tertiary text on ink -- captions, timestamps, units

# The one colour that means Myriaxon. Used sparingly and always to mark the live thing:
# the running state, the active nav item, the core of the mark. SIGNAL_INK is the text
# colour that sits *on* a signal fill -- it is not a darker signal, it is the readable
# foreground, which is why it inverts wholesale between themes.
SIGNAL = "#7CF5C8"
SIGNAL_INK = "#062A1E"

# Supporting hues, each bound to one meaning so a colour is never decorative.
BLUE = "#8FB4FF"       # informational / routing
AMBER = "#FFB86B"      # needs attention / scheduled
VIOLET = "#C9A2FF"     # memory and skills
RED = "#FF8A8A"        # failure / denied

# --------------------------------------------------------------------------------------
# Palette -- light ground
# --------------------------------------------------------------------------------------
# The light theme is not the dark theme inverted; every hue is re-picked for contrast on
# a pale ground. Signal green on white fails WCAG outright, so the light accent is a deep
# teal-green that still reads as the same brand family, and the supporting hues darken to
# the same end.
INK_LIGHT = "#F4F5F8"
INK_2_LIGHT = "#FFFFFF"
INK_3_LIGHT = "#EEF0F4"
LINE_LIGHT = "#DDE0E7"
LINE_2_LIGHT = "#C9CED9"

TEXT_LIGHT = "#0A0C11"
TEXT_2_LIGHT = "#3F4658"
TEXT_3_LIGHT = "#5A6275"

SIGNAL_LIGHT = "#0B8F63"
SIGNAL_INK_LIGHT = "#FFFFFF"

BLUE_LIGHT = "#2F5FD0"
AMBER_LIGHT = "#B25E00"
VIOLET_LIGHT = "#7A45D6"
RED_LIGHT = "#C62828"


def dark_tokens() -> dict[str, str]:
    """The dark palette as ``--name: value`` CSS custom properties."""
    return {
        "--ink": INK,
        "--ink-2": INK_2,
        "--ink-3": INK_3,
        "--line": LINE,
        "--line-2": LINE_2,
        "--text": TEXT,
        "--text-2": TEXT_2,
        "--text-3": TEXT_3,
        "--signal": SIGNAL,
        "--signal-ink": SIGNAL_INK,
        "--blue": BLUE,
        "--amber": AMBER,
        "--violet": VIOLET,
        "--red": RED,
    }


def light_tokens() -> dict[str, str]:
    """The light palette as ``--name: value`` CSS custom properties."""
    return {
        "--ink": INK_LIGHT,
        "--ink-2": INK_2_LIGHT,
        "--ink-3": INK_3_LIGHT,
        "--line": LINE_LIGHT,
        "--line-2": LINE_2_LIGHT,
        "--text": TEXT_LIGHT,
        "--text-2": TEXT_2_LIGHT,
        "--text-3": TEXT_3_LIGHT,
        "--signal": SIGNAL_LIGHT,
        "--signal-ink": SIGNAL_INK_LIGHT,
        "--blue": BLUE_LIGHT,
        "--amber": AMBER_LIGHT,
        "--violet": VIOLET_LIGHT,
        "--red": RED_LIGHT,
    }


# --------------------------------------------------------------------------------------
# Type
# --------------------------------------------------------------------------------------
# The dashboard must stay fully offline -- the existing page makes zero network requests,
# and a check in the suite asserts that. So the brand faces are declared as stacks rather
# than fetched: when Myriaxon's own faces are installed (or dropped into
# ``~/.myriaxon/fonts`` by the font installer) the brand rendering is exact, and when they
# are not the page still renders in a stack chosen to sit as close as possible.
#
# This is the one deliberate compromise in the identity, and it is a security and
# air-gap decision rather than an aesthetic one: an agent harness that phones a font CDN
# on first paint leaks that it is running, and cannot be used on the isolated networks
# this tool is built for.
FONT_DISPLAY = '"Unbounded", "Arial Black", "Segoe UI Variable Display", system-ui, sans-serif'
FONT_UI = (
    '"Instrument Sans", "Inter", "Geist", "Segoe UI Variable Text", "Segoe UI", '
    '-apple-system, BlinkMacSystemFont, "Helvetica Neue", Arial, sans-serif'
)
FONT_MONO = (
    '"JetBrains Mono", "Geist Mono", "Cascadia Mono", ui-monospace, SFMono-Regular, '
    'Menlo, Consolas, "Liberation Mono", monospace'
)
# Tracking applied to the wordmark. The display face is wide by design; the letterspacing
# is what carries the brand when the real face is absent and a flat system font is used.
WORDMARK_TRACKING = "0.14em"
HERO_TRACKING = "0.02em"


# --------------------------------------------------------------------------------------
# Copy
# --------------------------------------------------------------------------------------
TAGLINE = "Ten thousand paths. One mind."
TAGLINE_COMPACT = "ten thousand paths · one mind"
DESCRIPTOR = "Advanced agentic harness"
DESCRIPTOR_LONG = "An advanced agentic harness for terminal and browser."


# --------------------------------------------------------------------------------------
# The mark
# --------------------------------------------------------------------------------------
# Eight rays on a 200x200 canvas, drawn from the origin rather than as four rotations of
# one path so the cardinal and diagonal rays can differ in length and taper -- the
# diagonals stop short of the cardinals, which is what stops the mark from reading as a
# generic eight-point star.
CARDINAL_RAY = "100,6 104.5,68 100,80 95.5,68"
DIAGONAL_RAY = "100,36 103,70 100,80 97,70"
CARDINAL_ANGLES = (0, 90, 180, 270)
DIAGONAL_ANGLES = (45, 135, 225, 315)

MARK_RING_R = 12.0      # outer core radius, filled with the surrounding ink
MARK_INNER_R = 8.5      # the ring itself
MARK_CORE_R = 4.0       # the signal locus at the centre


def mark_svg(size: int = 28, label: str = "Myriaxon", cls: str = "mark") -> str:
    """Inline SVG for the mark, ready to drop into HTML.

    The inner disc is filled with ``var(--ink-2)`` rather than a literal colour so the
    mark opens its centre to whatever surface it sits on -- that is what lets one markup
    string serve a panel, the page, and a tooltip without a second variant, and it is why
    the old four-circle logogram's duplicate dark/light colour sets are gone.
    """
    rays = []
    for angle in CARDINAL_ANGLES:
        rot = f' transform="rotate({angle} 100 100)"' if angle else ""
        rays.append(f'<polygon points="{CARDINAL_RAY}"{rot}/>')
    for angle in DIAGONAL_ANGLES:
        rays.append(f'<polygon points="{DIAGONAL_RAY}" transform="rotate({angle} 100 100)"/>')
    body = "".join(rays)
    return (
        f'<svg class="{cls}" width="{size}" height="{size}" viewBox="0 0 200 200" '
        f'role="img" aria-label="{label}" focusable="false">'
        f"<title>{label}</title>"
        f'<g fill="currentColor">{body}'
        f'<circle cx="100" cy="100" r="{MARK_RING_R}"/></g>'
        f'<circle cx="100" cy="100" r="{MARK_INNER_R}" fill="var(--ink-2, #10131A)"/>'
        f'<circle cx="100" cy="100" r="{MARK_CORE_R}" fill="var(--signal, {SIGNAL})"/>'
        f"</svg>"
    )


def mark_svg_standalone(size: int = 28, signal: str = SIGNAL, label: str = "Myriaxon") -> str:
    """The mark with no CSS-variable dependencies -- for the .svg asset and the favicon.

    ``mark_svg`` is for in-page use where the CSS custom properties exist. This variant
    bakes in concrete colours so it renders correctly in a file viewer, an <img>, or a
    favicon context where no stylesheet is applied.
    """
    rays = []
    for angle in CARDINAL_ANGLES:
        rot = f' transform="rotate({angle} 100 100)"' if angle else ""
        rays.append(f'<polygon points="{CARDINAL_RAY}"{rot}/>')
    for angle in DIAGONAL_ANGLES:
        rays.append(f'<polygon points="{DIAGONAL_RAY}" transform="rotate({angle} 100 100)"/>')
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" '
        f'viewBox="0 0 200 200" role="img" aria-label="{label}">'
        f"<title>{label}</title>"
        f'<g fill="{INK}">'
        + "".join(rays)
        + f'<circle cx="100" cy="100" r="{MARK_RING_R}"/></g>'
        f'<circle cx="100" cy="100" r="{MARK_INNER_R}" fill="{INK_2}"/>'
        f'<circle cx="100" cy="100" r="{MARK_CORE_R}" fill="{signal}"/>'
        f"</svg>"
    )


def app_icon_svg(size: int = 512, ground: str = INK, signal: str = SIGNAL) -> str:
    """The square app-icon / favicon: the mark reversed out of a rounded ink tile.

    At 16 px the lockup is a smudge -- eight hairlines and a 4 px locus -- so the icon is a
    deliberately heavier drawing of the same geometry (thick tapered rays, a solid core)
    rather than the lockup scaled down.
    """
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" '
        f'viewBox="0 0 200 200" role="img" aria-label="Myriaxon">'
        f"<title>Myriaxon</title>"
        f'<rect width="200" height="200" rx="46" fill="{ground}"/>'
        f'<g fill="{TEXT}">'
        f'<polygon points="100,14 106,66 100,76 94,66"/>'
        f'<polygon points="186,100 134,106 124,100 134,94"/>'
        f'<polygon points="100,186 94,134 100,124 106,134"/>'
        f'<polygon points="14,100 66,94 76,100 66,106"/>'
        f'<polygon points="100,38 104,68 100,78 96,68" transform="rotate(45 100 100)"/>'
        f'<polygon points="100,38 104,68 100,78 96,68" transform="rotate(135 100 100)"/>'
        f'<polygon points="100,38 104,68 100,78 96,68" transform="rotate(225 100 100)"/>'
        f'<polygon points="100,38 104,68 100,78 96,68" transform="rotate(315 100 100)"/>'
        f'<circle cx="100" cy="100" r="15"/></g>'
        f'<circle cx="100" cy="100" r="10.5" fill="{ground}"/>'
        f'<circle cx="100" cy="100" r="5.5" fill="{signal}"/>'
        f"</svg>"
    )


def app_icon_data_uri(signal: str = SIGNAL) -> str:
    """The app icon as a ``data:`` URI, for a favicon link with no extra request."""
    from urllib.parse import quote

    return "data:image/svg+xml," + quote(app_icon_svg(signal=signal), safe="")


# --------------------------------------------------------------------------------------
# The terminal mark
# --------------------------------------------------------------------------------------
# A terminal cannot draw curves, but a braille cell is a 2x4 locus grid -- enough to render
# the compass geometry rather than approximating it with an asterisk. Locus bits within one
# cell, keyed (column, row):
_BRAILLE_DOTS = (
    (0, 0, 1), (1, 0, 8),
    (0, 1, 2), (1, 1, 16),
    (0, 2, 4), (1, 2, 32),
    (0, 3, 64), (1, 3, 128),
)
_BRAILLE_ROWS_PER_CELL = 4
_BRAILLE_COLS_PER_CELL = 2

# The compass on a 7x8 grid. Seven columns because braille is 2 loci wide per cell and an
# odd width centres the core on a single locus column; eight rows because braille is exactly
# 4 loci tall, so the mark folds into two glyph rows with no padding. '#' is a lit locus.
#
#    # . # . # . #      upward ray + both upper diagonals
#    . . # . # . .
#    . . # # # . .
#    . . # # # . .
#    . . # # # . .
#    . . # # # . .
#    . . # . # . .
#    # . # . # . #      downward ray + both lower diagonals
_COMPASS = (
    "#.#.#.#",
    "..#.#..",
    "..###..",
    "..###..",
    "..###..",
    "..###..",
    "..#.#..",
    "#.#.#.#",
)


def _compass_glyph(grid: tuple[str, ...] = _COMPASS) -> str:
    """Render a locus grid as braille, cells joined with a hair space.

    Cells are separated by U+200A rather than a normal space so a terminal font's cell
    metrics cannot collapse the glyphs into one blob. Derived from the locus table rather
    than hand-encoded: a hand-encoded braille literal is impossible to check without a
    working renderer, which is exactly how the previous mark ended up wrong.
    """
    height = len(grid)
    width = len(grid[0])
    lines: list[str] = []
    for cell_row in range(0, height, _BRAILLE_ROWS_PER_CELL):
        line: list[str] = []
        for cell_col in range(0, width, _BRAILLE_COLS_PER_CELL):
            code = 0x2800
            for (dc, dr, bit) in _BRAILLE_DOTS:
                r, c = cell_row + dr, cell_col + dc
                if r < height and c < width and grid[r][c] == "#":
                    code |= bit
            line.append(chr(code))
        lines.append("\u200a".join(line))
    return "\n".join(lines)


MARK_GLYPH = _compass_glyph()
MARK_GLYPH_LINES = tuple(MARK_GLYPH.split("\n"))
MARK_GLYPH_SMALL = "✳"     # for dense contexts: status lines, the web header fallback
MYRIAXON_ART = MARK_GLYPH_LINES


# --------------------------------------------------------------------------------------
# The star, at the size the header actually needs
# --------------------------------------------------------------------------------------
# The braille mark above is derived from an 8x8 locus grid, so it is two cell-rows tall -- which
# is 2 terminal rows. Beside a five-row wordmark that renders as a small colon, and it did:
# measured in a real session the header read "⠁⣧⡇⠁ ⠀⡟⡇⡀  MYRIAXON", a smudge next to the name.
#
# This draws the SAME geometry the SVG logo uses -- four long rays on the axes, four shorter ones
# on the diagonals, converging on a solid core -- but at 10 rows, so the symbol stands as tall as
# the wordmark beside it and reads as a star rather than as punctuation.
#
# Kept separate from `_compass_glyph` rather than replacing it: that one is the brand's documented
# 8x8 locus grid, and other renderers (the CLI's `avatar` command, the poster generator) draw from
# it. Two sizes of one mark is a normal thing for a brand to have; changing the small one to fix
# the header would have silently changed every other surface.
_STAR_ROWS = 10
_STAR_COLUMNS = 10


# The grid side is ODD, and that is not cosmetic. The first version used 10 cells, whose centre
# falls between two cells -- so "equally far in x and y", which is exactly what a diagonal ray is,
# could never be true of any cell, and the axis rays failed the same way on a half-integer centre.
# The result was a star with four corner dots and a two-cell blob in the middle. An odd grid has a
# true centre cell, and the two families of rays become exact integer tests.
#
# 13 cells becomes 6 terminal rows, which is the height the header wants: the wordmark beside it is
# five rows, so a six-row mark centres on it with equal blank space above and below. A four-row mark
# left an odd row over, which reads as the symbol sitting low.
_STAR_CELLS = 13


def _star_grid() -> list[list[bool]]:
    """Which cells of an odd square braille grid the compass star covers.

    Distances are measured in whole cells from the centre cell, so:
      * an AXIS ray is a cell that is `d` away in one axis and not at all in the other;
      * a DIAGONAL ray is a cell that is `d` away in BOTH axes.
    Both are exact, which is the point -- a shape built from inequalities tuned to a resolution
    looks different at every resolution, and this one has to match the SVG at terminal size.
    """
    half = _STAR_CELLS // 2
    grid = [[False] * _STAR_CELLS for _ in range(_STAR_CELLS)]
    for row in range(_STAR_CELLS):
        for column in range(_STAR_CELLS):
            dx = abs(column - half)
            dy = abs(row - half)
            reach = max(dx, dy)
            if reach == 0:
                grid[row][column] = True                # the signal core
            elif reach == 1:
                continue                                # a gap, so the core reads as a core
            elif dx == 0 or dy == 0:
                grid[row][column] = True                # long rays, on the axes
            elif dx == dy:
                grid[row][column] = True                # the same length, on the diagonals
    return grid


def _star_glyph() -> str:
    grid = _star_grid()
    lines: list[str] = []
    for cell_row in range(0, len(grid), _BRAILLE_ROWS_PER_CELL):
        line: list[str] = []
        for cell_col in range(0, len(grid[0]), _BRAILLE_COLS_PER_CELL):
            code = 0x2800
            for (dc, dr, bit) in _BRAILLE_DOTS:
                r, c = cell_row + dr, cell_col + dc
                if r < len(grid) and c < len(grid[0]) and grid[r][c]:
                    code |= bit
            line.append(chr(code))
        lines.append("\u200a".join(line))
    # Empty rows are dropped: the outermost grid rows can miss every ray, and a leading blank row
    # would push the mark off the wordmark's centre line.
    return "\n".join(line for line in lines if line.replace("\u200a", "").strip("\u2800"))


STAR_GLYPH = _star_glyph()
STAR_GLYPH_LINES = tuple(STAR_GLYPH.split("\n"))


def star_core_position() -> int:
    """The character index of the core in the star's middle row, or -1 when there is none.

    The core is the one cell that carries the signal colour rather than the ray colour, so a
    renderer that wants a lit centre rather than one flat silhouette needs to know where it is.
    Derived from the grid instead of written down, so it cannot drift when the grid changes.
    """
    grid = _star_grid()
    middle_row = len(grid) // 2
    centre_column = len(grid[0]) // 2
    if not grid[middle_row][centre_column]:
        return -1
    # Each cell is followed by a hair space, so the character index of cell N is 2N.
    return centre_column * 2


# --------------------------------------------------------------------------------------
# The wordmark
# --------------------------------------------------------------------------------------
# Five-row block letters for the seven initials of MYRIAXON. The banner prints the letters
# large and the name in full beside the mark, which is how the brand reads in a terminal:
# mark, wordmark, descriptor.
#
# Every glyph carries a two-cell stroke, so the wordmark reads as one weight rather than a
# mix of bold and hairline. The uprights use the half-block bars (U+2590, U+258C) rather
# than full blocks (U+2588): a full block fills its cell edge to edge, so two adjacent
# uprights fuse into one slab and the counters between them disappear at the sizes a
# terminal actually renders. The bars leave a hairline gap that keeps them legible.
#
# Diagonals use the box-drawing set, because a terminal cell cannot hold a true diagonal -- a
# staircase of half-cells with rounded joins is the closest honest approximation.
_WORDMARK_GLYPHS = {
    "M": ("▐▌ ▐▌", "██▄██", "██ ██", "▐▌ ▐▌", "▐▌ ▐▌"),
    "Y": ("▐▌ ▐▌", "▐▌ ▐▌", " ███ ", "  █  ", "  █  "),
    "R": ("████ ", "▐▌ ▐▌", "████ ", "▐▌▐▌ ", "▐▌ ▐▌"),
    "I": ("████", " ▐▌ ", " ▐▌ ", " ▐▌ ", "████"),
    "A": (" ██ ", "▐▌▐▌", "████", "▐▌▐▌", "▐▌▐▌"),
    "X": ("▐▌▐▌", " ██ ", " ██ ", "▐▌▐▌", "▐▌▐▌"),
    "O": ("╔══╗", "║  ║", "║  ║", "║  ║", "╚══╝"),
    "N": ("▐▌ ▐▌", "██ ▐▌", "▐██▌", "▐▌ █", "▐▌ ▐▌"),
}


def wordmark_rows(word: str = "MYRIAXON") -> tuple[str, ...]:
    """Render ``word`` as five rows of block letters, one space between glyphs."""
    rows = []
    for index in range(5):
        rows.append(" ".join(_WORDMARK_GLYPHS[ch][index] for ch in word))
    return tuple(rows)


# --------------------------------------------------------------------------------------
# Legacy CSS aliases
# --------------------------------------------------------------------------------------
# The pre-rebrand dashboard markup and JavaScript reference the old variable names in
# hundreds of places (``var(--bg)``, ``var(--muted)``, ``var(--accent)``, ``var(--mono)``).
# These aliases let the new palette land without a single-pass rewrite of every rule, and
# they are emitted in the same ``:root`` block so a stray old name can never silently
# resolve to nothing -- which is precisely the class of bug (an undefined ``--mono``) that
# shipped in the previous design.
LEGACY_ALIASES: dict[str, str] = {
    "--bg": "var(--ink)",
    "--panel": "var(--ink-2)",
    "--panel-2": "var(--ink-3)",
    "--border": "var(--line)",
    "--muted": "var(--text-2)",
    "--faint": "var(--text-3)",
    "--accent": "var(--signal)",
    "--accent-2": "var(--blue)",
    "--accent-dim": "color-mix(in srgb, var(--signal) 26%, transparent)",
    "--ok": "var(--signal)",
    "--warn": "var(--amber)",
    "--err": "var(--red)",
    "--code-bg": "color-mix(in srgb, var(--ink) 70%, var(--ink-2))",
    "--hover": "var(--ink-3)",
    "--user": "var(--ink-3)",
    "--assistant": "color-mix(in srgb, var(--signal) 10%, var(--ink-2))",
    "--ok-bg": "color-mix(in srgb, var(--signal) 12%, var(--ink-2))",
    "--ok-border": "color-mix(in srgb, var(--signal) 32%, var(--line))",
    "--add-bg": "color-mix(in srgb, var(--signal) 13%, var(--ink-2))",
    "--del-bg": "color-mix(in srgb, var(--red) 13%, var(--ink-2))",
    "--shadow": "0 8px 26px rgba(0,0,0,.5)",
    "--mono": FONT_MONO,
    "--ui": FONT_UI,
    "--display": FONT_DISPLAY,
}


def css_root_block(selector: str, tokens: dict[str, str], *, include_aliases: bool = False) -> str:
    """Render one ``selector { --name: value; ... }`` block."""
    lines = [f"{selector}{{"]
    for name, value in tokens.items():
        lines.append(f"  {name}:{value};")
    if include_aliases:
        lines.append("")
        lines.append("  /* legacy names, aliased so old markup cannot resolve to nothing */")
        for name, value in LEGACY_ALIASES.items():
            lines.append(f"  {name}:{value};")
    lines.append("}")
    return "\n".join(lines)


__all__ = [
    # palette
    "INK", "INK_2", "INK_3", "LINE", "LINE_2",
    "TEXT", "TEXT_2", "TEXT_3", "SIGNAL", "SIGNAL_INK",
    "BLUE", "AMBER", "VIOLET", "RED",
    "INK_LIGHT", "INK_2_LIGHT", "INK_3_LIGHT", "LINE_LIGHT", "LINE_2_LIGHT",
    "TEXT_LIGHT", "TEXT_2_LIGHT", "TEXT_3_LIGHT",
    "SIGNAL_LIGHT", "SIGNAL_INK_LIGHT",
    "BLUE_LIGHT", "AMBER_LIGHT", "VIOLET_LIGHT", "RED_LIGHT",
    "dark_tokens", "light_tokens", "css_root_block", "LEGACY_ALIASES",
    # type
    "FONT_DISPLAY", "FONT_UI", "FONT_MONO", "WORDMARK_TRACKING", "HERO_TRACKING",
    # copy
    "TAGLINE", "TAGLINE_COMPACT", "DESCRIPTOR", "DESCRIPTOR_LONG",
    # mark
    "CARDINAL_RAY", "DIAGONAL_RAY", "CARDINAL_ANGLES", "DIAGONAL_ANGLES",
    "mark_svg", "mark_svg_standalone", "app_icon_svg", "app_icon_data_uri",
    "MARK_GLYPH", "MARK_GLYPH_LINES", "MARK_GLYPH_SMALL", "MYRIAXON_ART",
    "wordmark_rows",
]
