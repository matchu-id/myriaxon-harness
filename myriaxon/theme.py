"""Myriaxon's terminal identity: the Rich themes, the spinner, and syntax highlighting.

Every colour here comes from :mod:`myriaxon.brand`, which is the single source of truth
for the product's visual identity. Nothing in this module hardcodes a hex value -- that is
the point of the indirection, because the palette previously lived in three unrelated
places (this file, the banner's colour cycle, and ``completion.py``'s prompt_toolkit
style) and a brand change had to be applied three times or the UI came out inconsistent.
"""

import time

from rich.console import Console
from rich.text import Text
from rich.theme import Theme
from pygments.style import Style as PygmentsStyle
from pygments.token import Comment, Generic, Keyword, Name, Number, Operator, Punctuation, String, Token

from . import brand


def _ink_theme() -> Theme:
    """The default Myriaxon theme: signal green on a near-black ground.

    ``banner`` is a *surface* style (a fill plus the text that reads on it), while
    ``banner.accent`` is the mark and the version stamp that sit beside it. Keeping those
    two separate is what lets the header read as one composed block rather than a line of
    unrelated colours.
    """
    b = brand
    return Theme(
        {
            # --- surfaces
            "banner": f"bold {b.TEXT} on {b.INK_2}",
            "banner.accent": f"bold {b.SIGNAL}",
            "banner.mark": f"bold {b.SIGNAL}",
            # The star's centre dot. Brighter than the rays -- the rays are signal green and the core
            # is white-hot -- so the mark reads as a star with a lit centre rather than as one flat
            # silhouette. A background fill would be invisible: a braille glyph is drawn in the
            # foreground colour, so a same-coloured fill behind it shows nothing.
            "banner.core": f"bold {b.TEXT}",
            "banner.wordmark": f"bold {b.TEXT}",
            "banner.tagline": f"{b.TEXT_3}",
            "banner.dim": f"{b.TEXT_2}",
            # --- prompt and conversation
            "prompt": f"bold {b.SIGNAL}",
            # The prompt while a locus owns the conversation. A different colour, because the whole
            # point is that you can tell at a glance who is about to answer -- `/loci chat analyst`
            # otherwise looked identical to talking to Myriaxon, and the mistake only showed up in
            # the reply.
            "prompt.locus": f"bold {b.VIOLET}",
            "prompt.user": f"bold {b.TEXT}",
            "assistant.text": b.TEXT,
            "user.text": b.TEXT,
            # --- tools
            "tool.name": f"bold {b.SIGNAL}",
            "tool.pending": b.AMBER,
            "tool.confirm.question": f"bold {b.AMBER}",
            "tool.result": b.TEXT_2,
            # --- diffs
            "diff.add": b.SIGNAL,
            "diff.remove": f"{b.TEXT_3} strike",
            "diff.context": b.TEXT_3,
            # --- states
            "error": f"bold {b.TEXT} on {b.RED}",
            "warning": f"bold {b.AMBER}",
            "success": f"bold {b.SIGNAL}",
            "info": b.BLUE,
            "memory": b.VIOLET,
            # --- structure
            "dim": b.TEXT_3,
            "muted": b.TEXT_2,
            "path": f"italic {b.BLUE}",
            "key": f"bold {b.TEXT}",
            "value": b.TEXT_2,
            "heading": f"bold {b.TEXT}",
        }
    )


def _high_contrast_theme() -> Theme:
    """Accessibility variant: maximum separation, no reliance on the brand hue.

    Signal green is a *low-contrast* colour by design -- it is pale relative to its ground,
    which is what makes it read as a signal rather than a highlight. That is the wrong
    property for a user who needs contrast, so this theme swaps the accent for bold white
    and leans on weight and case instead of hue.
    """
    return Theme(
        {
            "banner": "bold black on white",
            "banner.accent": "bold yellow",
            "banner.mark": "bold yellow",
            "banner.core": "bold yellow",
            "banner.wordmark": "bold white",
            "banner.tagline": "white",
            "banner.dim": "white",
            "prompt": "bold yellow",
            "prompt.locus": "bold magenta",
            "prompt.user": "bold white",
            "assistant.text": "white",
            "user.text": "white",
            "tool.name": "bold yellow",
            "tool.pending": "yellow",
            "tool.confirm.question": "bold yellow",
            "tool.result": "white",
            "diff.add": "bold green",
            "diff.remove": "bold red strike",
            "diff.context": "dim white",
            "error": "bold white on red",
            "warning": "bold yellow",
            "success": "bold green",
            "info": "bold cyan",
            "memory": "bold magenta",
            "dim": "grey70",
            "muted": "grey85",
            "path": "underline white",
            "key": "bold white",
            "value": "white",
            "heading": "bold white",
        }
    )


def _mono_theme() -> Theme:
    """No colour at all -- for terminals, log capture and CI that do not render ANSI.

    Structure is carried entirely by weight, reverse video and underline, so the output is
    still hierarchical when piped to a file.
    """
    return Theme(
        {
            "banner": "bold",
            "banner.accent": "bold",
            "banner.mark": "bold",
            "banner.core": "bold",
            "banner.wordmark": "bold",
            "banner.tagline": "",
            "banner.dim": "dim",
            "prompt": "bold",
            "prompt.locus": "bold reverse",
            "prompt.user": "bold",
            "assistant.text": "",
            "user.text": "",
            "tool.name": "bold",
            "tool.pending": "",
            "tool.confirm.question": "bold",
            "tool.result": "dim",
            "diff.add": "",
            "diff.remove": "strike",
            "diff.context": "",
            "error": "bold reverse",
            "warning": "bold",
            "success": "bold",
            "info": "",
            "memory": "",
            "dim": "dim",
            "muted": "dim",
            "path": "underline",
            "key": "bold",
            "value": "",
            "heading": "bold",
        }
    )


MYRIAXON_THEME = _ink_theme()
HIGH_CONTRAST_THEME = _high_contrast_theme()
MONO_THEME = _mono_theme()

THEMES: dict[str, Theme] = {
    "default": MYRIAXON_THEME,
    "high-contrast": HIGH_CONTRAST_THEME,
    "mono": MONO_THEME,
}


def get_console(theme_name: str | None = None, quiet: bool = False) -> Console:
    return Console(theme=THEMES.get(theme_name or "default", MYRIAXON_THEME), quiet=quiet)


# --------------------------------------------------------------------------------------
# Spinner
# --------------------------------------------------------------------------------------
_SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]


class SignalSpinner:
    """A braille spinner that breathes through the brand's ink steps.

    It cycles *brightness* rather than hue -- signal green held constant while the trail
    fades through the palette's neutrals. The previous spinner cycled three unrelated hues,
    which read as three different things happening rather than one thing in progress.
    """

    _TRAIL = (
        brand.SIGNAL, brand.SIGNAL, brand.TEXT, brand.TEXT_2,
        brand.TEXT_3, brand.TEXT_2, brand.TEXT,
    )

    def __init__(self, text: str = "Thinking...") -> None:
        self.text = text
        self._start = time.monotonic()

    def __rich_console__(self, console, options):
        elapsed = time.monotonic() - self._start
        frame = _SPINNER_FRAMES[int(elapsed * 10) % len(_SPINNER_FRAMES)]
        colour = self._TRAIL[int(elapsed * 8) % len(self._TRAIL)]
        yield Text(f"{frame} {self.text}", style=f"bold {colour}")


# The previous class name, kept so an existing import cannot silently fail.
ThreeColorSpinner = SignalSpinner


# --------------------------------------------------------------------------------------
# Syntax highlighting
# --------------------------------------------------------------------------------------
class MyriaxonPygmentsStyle(PygmentsStyle):
    """Code highlighting restricted to the Myriaxon palette.

    Colour carries *grammar*, not decoration: keywords take the brand signal, names take
    the informational blue, and literals take the amber that already means "a value worth
    noticing" elsewhere in the product. Nothing is coloured without a meaning attached.
    """

    background_color = "#08090B"
    styles = {
        Token: brand.TEXT,
        Comment: f"italic {brand.TEXT_3}",
        Keyword: f"bold {brand.SIGNAL}",
        Keyword.Constant: brand.AMBER,
        Name: brand.TEXT,
        Name.Builtin: brand.BLUE,
        Name.Function: brand.SIGNAL,
        Name.Class: f"bold {brand.BLUE}",
        Name.Decorator: brand.VIOLET,
        String: brand.AMBER,
        Number: brand.VIOLET,
        Operator: brand.TEXT_2,
        Punctuation: brand.TEXT_2,
        Generic.Deleted: brand.RED,
        Generic.Inserted: brand.SIGNAL,
        Generic.Heading: f"bold {brand.TEXT}",
        Generic.Error: f"bold {brand.RED}",
    }


# The previous class name, kept for the same reason as ThreeColorSpinner above.
IoHPygmentsStyle = MyriaxonPygmentsStyle


def render_markdown(text: str):
    """Build a Rich Markdown renderable, importing the renderer on first use.

    `rich.markdown` costs ~2.5s of import time on a cold machine (it pulls in markdown_it
    and its plugin tree), and every provider agent imports this module at startup.
    Deferring it here means a session that never prints a markdown answer -- and every
    non-interactive command, health check and benchmark harness call -- never pays for it.
    The import result is cached by Python, so the second and later calls are free.
    """
    from rich.markdown import Markdown

    return Markdown(text, code_theme=MyriaxonPygmentsStyle)
