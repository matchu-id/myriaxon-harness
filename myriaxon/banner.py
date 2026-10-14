"""The Myriaxon header: the terminal rendering of the brand.

The identity is an eight-ray astral compass -- the myriad paths -- converging on a single
signal-green core. In a terminal that cannot draw curves, the mark is rendered as braille
geometry (see :mod:`myriaxon.brand`, which derives it from a locus grid rather than storing a
hand-encoded glyph), and the wordmark is set in block letters beside it.

Layout, and why
---------------
The mark sits to the left of the wordmark rather than above it, because a terminal window
is wide and short: a stacked lockup wastes the scarce dimension. The version is stamped in
the signal colour to the right of the name, the connection line and descriptor below, and
the whole thing is framed in a panel whose border is the accent, so the header reads as one
object instead of three stacked lines.

The wordmark falls back to plain tracked-out capitals when the terminal is too narrow for
the block letters, which keeps the header intact on an 80-column session instead of
wrapping into garbage.
"""

from rich.console import Console, Group
from rich.markup import escape
from rich.panel import Panel
from rich.text import Text

from . import brand
from .config import Config
from .constants import APP_NAME, AUTHOR, VERSION

# Block letters for MYRIAXON, five rows tall. Derived from brand.wordmark_rows() so the
# terminal wordmark and any other renderer share one set of letterforms.
_WORDMARK = brand.wordmark_rows("MYRIAXON")
_WORDMARK_HEIGHT = len(_WORDMARK)
_WORDMARK_WIDTH = max(len(row) for row in _WORDMARK)
_MARK = brand.MARK_GLYPH_LINES
# The header mark: the compass star, at the size the header needs. `MARK_GLYPH_LINES` is the brand's
# 8x8 locus grid, two cell-rows tall -- beside a five-row wordmark it read as a colon (measured in a
# real session: "⠁⣧⡇⠁ ⠀⡟⡇⡀  MYRIAXON"). The star is the same geometry at four cell-rows, so the
# symbol stands as tall as the name beside it.
_STAR = brand.STAR_GLYPH_LINES or _MARK
_CORE_INDEX = brand.star_core_position()
_BRAILLE_BLANK = "\u2800"


def _narrow_wordmark() -> Text:
    """The fallback lockup: MYRIAXON tracked out, for terminals without room for blocks."""
    text = Text()
    text.append(APP_NAME.upper(), style="banner.wordmark")
    return text


def _mark_and_wordmark() -> Group:
    """The star beside the wordmark, baselined so the two optically align.

    The mark is shorter than the wordmark, so it is padded with blank lines above and below to sit
    on the wordmark's optical centre rather than its top edge -- the same correction a designer
    applies to a logotype and its symbol.

    The core is styled apart from the rays, which is what makes this read as a star with a lit centre
    instead of one flat silhouette: in the SVG the rays are ink and only the middle dot carries the
    signal colour, so the terminal keeps that relationship. Braille blanks are appended rather than
    dropped, because an empty cell still occupies columns and the wordmark must not shift.
    """
    mark_rows = list(_STAR)
    mark_width = max(len(row) for row in mark_rows)
    # Both columns are padded to the SAME height, each centred in it. Without the second half the
    # taller column overflows: the star is four rows and the wordmark five, so aligning the star to a
    # five-row block left the wordmark's last row printed after the star had already ended.
    #
    # The short column's padding is split with the extra row BELOW, not above, when the difference is
    # odd. That is what optical centring means here: a mark sitting one row high reads as intentional,
    # one row low reads as a mistake -- the same asymmetry a designer applies to a symbol beside a
    # wordmark.
    total = max(_WORDMARK_HEIGHT, len(mark_rows))
    mark_pad = (total - len(mark_rows) + 1) // 2
    word_pad = (total - _WORDMARK_HEIGHT + 1) // 2

    lines: list[Text] = []
    for index in range(total):
        line = Text()
        mark_index = index - mark_pad
        if 0 <= mark_index < len(mark_rows):
            for position, character in enumerate(mark_rows[mark_index]):
                if character == _BRAILLE_BLANK or character == "\u200a":
                    line.append(character)
                elif position == _CORE_INDEX:
                    line.append(character, style="banner.core")
                else:
                    line.append(character, style="banner.mark")
        else:
            line.append(" " * mark_width)
        line.append("   ")
        word_index = index - word_pad
        line.append(_WORDMARK[word_index] if 0 <= word_index < _WORDMARK_HEIGHT else "")
        lines.append(line)
    return Group(*lines)


def _meta_line(config: Config) -> Text:
    """Where Myriaxon is pointed, and with which model -- the first thing to check when
    output looks wrong."""
    line = Text()
    line.append("connected to ", style="banner.dim")
    line.append(config.base_url, style="banner.accent")
    line.append("   model ", style="banner.dim")
    line.append(config.model, style="banner.accent")
    return line


def _identity_line() -> Text:
    line = Text()
    line.append(f"{APP_NAME} {VERSION}", style="banner.wordmark")
    line.append("   ·   ", style="banner.dim")
    line.append(brand.DESCRIPTOR, style="banner.dim")
    return line


def _tagline_line() -> Text:
    return Text(brand.TAGLINE, style="banner.tagline")


def should_use_block_wordmark(console: Console) -> bool:
    """Whether the terminal is wide enough for the block wordmark plus the panel chrome.

    The block lockup is roughly ``_WORDMARK_WIDTH + mark + padding + borders`` columns. A
    narrower terminal gets the tracked-out fallback rather than a wrapped mess.
    """
    needed = _WORDMARK_WIDTH + 12 + 6
    return console.width >= needed


def print_banner(console: Console, config: Config) -> None:
    """Print the Myriaxon header."""
    if should_use_block_wordmark(console):
        header: object = _mark_and_wordmark()
    else:
        header = _narrow_wordmark()

    body = Group(
        header,
        Text(""),
        _identity_line(),
        _meta_line(config),
        _tagline_line(),
    )
    console.print(Panel(body, border_style="banner.accent", padding=(1, 2), expand=False))


def print_banner_compact(console: Console, config: Config) -> None:
    """A one-line variant, for resumed sessions and non-interactive runs.

    The full panel is worth its vertical space exactly once, at the start of an interactive
    session. Re-printing it after ``/clear`` or on a resumed session spends six rows to say
    something the user already knows, so this form carries only what changes.
    """
    line = Text()
    line.append(brand.MARK_GLYPH_SMALL + " ", style="banner.mark")
    line.append(f"{APP_NAME} {VERSION}", style="banner.wordmark")
    line.append("  ·  ", style="banner.dim")
    line.append(config.model, style="banner.accent")
    line.append("  ·  ", style="banner.dim")
    line.append(escape(config.base_url), style="banner.dim")
    console.print(line)


__all__ = [
    "print_banner",
    "print_banner_compact",
    "should_use_block_wordmark",
    "AUTHOR",
]
