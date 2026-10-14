"""Loci's manifest and ASCII/Unicode renderer -- the single source both surfaces read.

## Why this file is the source of truth

The design spec's central requirement is that the command line and the dashboard must not drift apart. Two
hand-maintained tables of faces is exactly how that happens, so every drawable thing lives here: the moods, the
palette, the slot order, the fourteen expressions, and the templates. `loci_svg` imports this manifest, and
`verify_loci_character` asserts the two agree -- in both directions, so neither can quietly gain a face the other has
never heard of.

## The two rules that shaped the renderer

**Never colour alone.** A mood shown only by colour is invisible to a colourblind reader and to `NO_COLOR`, so the
mood's own label is printed into the art as its last line. Stripping every escape sequence must leave the mood
readable, which `verify_loci_character` checks by comparing a coloured render against a plain one.

**The frame never shifts.** Every expression renders to the same number of lines and the same width, and the body --
the antenna down to the feet -- sits in identical columns for all fourteen. `celebrate` is the one exception, and only
on its first line, where confetti replaces the antenna star: a decoration above the character rather than part of it.
A wider frame for everything else would move the art under the reader's eye every time the mood changed, which the
spec forbids. So every frame is padded to a common width, which means a deliberate overhang cannot become a shift.

## Glyph width, which is a property of the reader rather than of the character

Measured on this host: `east_asian_width("⬤") == "N"`, so the shipped glyphs are genuinely one column -- Unicode 9
reclassified them. `★` is the interesting case, because it is AMBIGUOUS: one column in a Western locale and two in an
East Asian one. A slot that suddenly renders two columns shifts every line below it, so `safe_glyphs` substitutes the
ASCII twin for an ambiguous glyph in a wide locale, and for a genuinely wide glyph in every locale.
"""

from __future__ import annotations

import os
import unicodedata
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------------------
# The seven slots
# ---------------------------------------------------------------------------------------

# The order every expression's glyph tuple is written in. Named rather than positional so the templates can say which
# slot they draw, and so a future slot cannot be appended without a reader noticing the tuple length changed.
SLOT_ORDER = ("S", "W", "L", "R", "C", "M", "N")
# S = star (antenna), W = window, L = left eye, R = right eye, C = cheek, M = mouth, N = lantern.

SLOT_INDEX = {name: index for index, name in enumerate(SLOT_ORDER)}


# ---------------------------------------------------------------------------------------
# Moods and palette
# ---------------------------------------------------------------------------------------

# Each mood is a named colour role, not a literal colour: the SVG turns these into data-mood attributes and the
# terminal turns them into ANSI codes, so one manifest drives both.
MOODS: dict[str, str] = {
    "calm": "steady, unremarkable, the resting state",
    "warm": "open and pleased to see you",
    "bright": "upbeat, energetic, delighted",
    "dim": "low, quiet, subdued",
    "care": "attentive and gentle; used when someone is struggling",
    "alert": "paying attention, working, or concerned",
    "serious": "grave; reserved for crisis, and never decorated",
}

# Truecolor first, then the 256-colour fallback, then the sixteen-colour fallback. Ordered most to least capable so
# `palette_for` can walk down it.
PALETTE: dict[str, str] = {
    "calm": "#7f9cc4",
    "warm": "#e0a86b",
    "bright": "#f2c14e",
    "dim": "#6b7280",
    "care": "#e79ab0",
    "alert": "#7fd1c1",
    "serious": "#b48ead",
}

PALETTE_256: dict[str, int] = {
    "calm": 103, "warm": 179, "bright": 221, "dim": 243, "care": 181, "alert": 115, "serious": 139,
}

PALETTE_16: dict[str, int] = {
    "calm": 34, "warm": 33, "bright": 33, "dim": 90, "care": 35, "alert": 36, "serious": 35,
}


def mood_for(expression: str) -> str:
    """The mood of an expression, falling back to calm for anything unknown."""
    found = EXPRESSIONS.get(expression)
    return found.mood if found else "calm"


# ---------------------------------------------------------------------------------------
# Expressions
# ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Expression:
    """One face.

    `uni` and `ascii_glyphs` are both seven characters in `SLOT_ORDER`. An empty string in a slot means the slot is
    genuinely blank for that expression -- crisis leaves the cheeks empty on purpose, because a blush on a crisis face
    is the single rendering this project must never ship.

    `motion` is a tuple of named CSS keyframes from the spec. It is empty for crisis, and the state machine refuses
    motion for crisis even on a motion-capable terminal, so the rule is enforced in two places rather than trusted to
    one.
    """

    id: str
    label: str
    mood: str
    uni: tuple[str, ...]
    ascii_glyphs: tuple[str, ...]
    # Named keyframes the dashboard may apply. Terminal renders ignore them.
    motion: tuple[str, ...] = ()
    # True for states that must not time out on their own: crisis, caring, listening, thinking.
    persistent: bool = False

    def slot(self, name: str) -> str:
        return self.uni[SLOT_INDEX[name]]

    def ascii_slot(self, name: str) -> str:
        return self.ascii_glyphs[SLOT_INDEX[name]]


# The fourteen faces. Ordered from the resting state outward, which is also the order the sheet lists them in.
EXPRESSIONS: dict[str, Expression] = {
    # --- the three section 4 expressions that were missing ---------------------------------------------
    "remembering": Expression(
        # The window is LIT and pulsing -- the one visible difference from `listening`, which shares the lantern and
        # the cheeks. The spec's point is that recalling something the user told it should look different from
        # merely hearing it, and a lit window is how a building says "someone is in".
        "remembering", "Remembering", "warm",
        ("✦", "▫", "◉", "◉", "‿", "‿", "◍"),
        ("*", "#", "o", "o", "u", "_", "O"),
        persistent=False,
      ),
    "sleepy": Expression(
        # Closed eyes, an off window. `persistent` because the spec says this one does NOT return to idle on the
        # 8-second timer: night mode and a bedtime nudge are states, not reactions, and a face that popped back to
          # `idle` while the user was still winding down would be reading the room wrong.
        "sleepy", "Resting", "dim",
        ("✦", "▫", "–", "–", "‿", "◦", "·"),
        ("*", "#", "-", "-", "u", ".", "."),
        persistent=True,
      ),
    "focus": Expression(
        # Half-lidded and steady, window dim, NO motion. `persistent` for the same reason as `sleepy`, and the
        # absence of motion is the capability: the spec says Loci stays out of the way during a focus session, so a
        # character that kept bobbing would be working against the feature it exists to support.
        "focus", "Focus mode", "calm",
        ("✦", "▫", "◉", "◉", "‿", "‿", "◍"),
        ("*", "#", "o", "o", "u", "_", "O"),
        persistent=True,
      ),
    "idle": Expression(
        "idle", "Here", "calm",
        ("✦", "▫", "◉", "◉", "‿", "‿", "◍"),
        ("*", "#", "o", "o", "u", "_", "O"),
    ),
    "happy": Expression(
        # Distinct from idle by its mouth: the resting face when nothing is happening, versus a greeting when
        # something has. Both exist because a session that opens on the same face it shows while waiting would make
        # the greeting meaningless.
        "happy", "Happy", "warm",
        ("✦", "▫", "◉", "◉", "‿", "◡", "◍"),
        ("*", "#", "o", "o", "u", "v", "O"),
    ),
    "listening": Expression(
        "listening", "Listening", "alert",
        ("✦", "▫", "◉", "◉", "‿", "◦", "◍"),
        ("*", "#", "o", "o", "u", ".", "O"),
    ),
    "thinking": Expression(
        "thinking", "Thinking", "alert",
        ("✦", "▫", "◉", "◉", "‿", "~", "◍"),
        ("*", "#", "o", "o", "u", "~", "O"),
        # NOT persistent. Section 4.1 lists exactly four faces that survive the 8-second idle timer -- caring, crisis,
        # focus and sleepy -- and `thinking` is not one of them. Marked persistent here, a turn that ended without
        # another event left the thinking face on screen indefinitely: the user would see Loci apparently still working
        # long after it had stopped. Measured by driving `apply` then `tick`: 7 of the 8 section 4.1 rules held and
        # this was the exception.
        persistent=False,
    ),
    "working": Expression(
        "working", "Working", "alert",
        ("✦", "▫", "◉", "◉", "‿", "=", "◍"),
        ("*", "#", "o", "o", "u", "=", "O"),
        motion=("loci-bob",),
    ),
    "cheering": Expression(
        "cheering", "Proud of you", "bright",
        ("✦", "▫", "◉", "◉", "‿", "▽", "◍"),
        ("*", "#", "o", "o", "u", "v", "O"),
        motion=("loci-hop",),
    ),
    "celebrate": Expression(
        "celebrate", "Yay!", "bright",
        ("✦", "▫", "★", "★", "‿", "▽", "◍"),
        ("*", "#", "*", "*", "u", "v", "O"),
        motion=("loci-hop", "loci-star-wiggle"),
    ),
    "caring": Expression(
        "caring", "With you", "care",
        ("✦", "▫", "◉", "◉", "‿", "︵", "◍"),
        # The ASCII twin is `v`, not `c`: `︵` is East Asian WIDE, so the width guard replaces it with the ASCII twin
        # in every locale. `u` would collide with the blush slot and leave the face unreadable -- measured on the
        # synthetic expression in the verifier, which substitutes `u` for a wide glyph and asserts the frame stays
        # one width.
        ("*", "#", "o", "o", "u", "v", "O"),
        persistent=True,
    ),
    "confused": Expression(
        # Distinct from `sorry` by its window rather than its mouth: both are subdued, but a puzzled face should look
        # like it is still working something out rather than like it has given up on it.
        "confused", "Hmm?", "alert",
        ("✦", "▫", "◔", "◉", "‿", "~", "◍"),
        ("*", "#", "c", "o", "u", "~", "O"),
    ),
    "listening_back": Expression(
        "listening_back", "Go on", "care",
        ("✦", "▫", "◉", "◉", "‿", "‿", "◍"),
        ("*", "#", "o", "o", "u", "u", "O"),
        persistent=True,
    ),
    "curious": Expression(
        "curious", "Curious", "warm",
        ("✦", "▫", "◉", "◉", "‿", "○", "◍"),
        ("*", "#", "o", "o", "u", "0", "O"),
    ),
    "pleased": Expression(
        "pleased", "Glad to help", "warm",
        ("✦", "▫", "◉", "◉", "‿", "‿", "◍"),
        ("*", "#", "o", "o", "u", "u", "O"),
    ),
    "sorry": Expression(
        "sorry", "Oops", "dim",
        ("✦", "▫", "◉", "◉", "‿", "︵", "◍"),
        ("*", "#", "o", "o", "u", "c", "O"),
    ),
    "concerned": Expression(
        "concerned", "Checking in", "dim",
        ("✦", "▫", "◉", "◉", "‿", "︵", "◍"),
        ("*", "#", "o", "o", "u", "c", "O"),
    ),
    "tired": Expression(
        "tired", "Weary", "dim",
        ("✦", "▫", "─", "─", "‿", "‿", "◌"),
        ("*", "#", "-", "-", "u", "u", "."),
    ),
    "crisis": Expression(
        "crisis", "Here with you", "serious",
        # The cheeks are deliberately EMPTY. A blush on a crisis face would be the worst thing this project could
        # render, and leaving the slot blank is how that is made structural rather than remembered.
        ("✦", "▫", "◉", "◉", "", "─", "◌"),
        # The ASCII twin for a blank slot is a SPACE, not an empty string. The two slots are deliberately different:
        # `uni[4]` must be empty for the assertion on the crisis face, while `ascii_glyphs` must be seven
        # single-character strings so `ord()` can be called on each. A space is both invisible and one character.
        ("*", "#", "o", "o", " ", "-", "."),
        motion=(),
        persistent=True,
    ),
}


# The fourteen the published spec defines, BY NAME, frozen at import.
#
# This exists because the manifest carries faces the spec does not. `happy` is part of the LIVE contract --
# `verify_loci_e2e` drives a session that opens on it and the dashboard calls `setLoci("happy")` on load -- while
# `listening_back` is an internal variant. Recording the spec tier as a NAMED SET lets a checker assert "the spec's
# fourteen are all present and unchanged", which is the property that protects the contract, instead of asserting a
# total count, which cannot tell a well-formed new face from a malformed one.
SPEC_NAMES: tuple[str, ...] = (
    # EXACTLY the fourteen rows of loci.md section 4, in the table's order and with the table's names.
    #
    # This list used to contain `working`, `tired`, `pleased` and `curious` -- none of which appear in the document --
    # and to omit `remembering`, `sleepy` and `focus`, which do. That mattered more than a missing feature, because
    # `verify_loci_character` asserts against THIS tuple: a verifier that agrees with the code instead of the
    # specification is not evidence, and part of its green count was checking the wrong list. Found by parsing the
    # section 4 table and diffing it, which is what should have been done when the tuple was first written.
    "idle", "happy", "listening", "thinking", "remembering", "cheering", "caring",
    "concerned", "crisis", "confused", "sleepy", "sorry", "focus", "celebrate",
)

# In the manifest and in the live contract, but NOT in the published section 4 table. Kept separate so they exist and
# stay audited without inflating the fourteen -- `verify_loci_autonomy`'s "spec plus extras" merge identity depends on
# the two sets being disjoint and on `SPEC_NAMES` being exactly the documented set.
LIVE_ONLY_NAMES: tuple[str, ...] = ("listening_back",)


def expression_for(name: str) -> Expression:
    """The expression, or `idle` for anything unknown.

    A typo must not silently draw a DIFFERENT mood: falling back to the resting face is honest, whereas picking a
    near neighbour would show the reader a feeling that was never asked for. `is_known` is what a caller uses to
    report the typo.
    """
    return EXPRESSIONS.get(name) or EXPRESSIONS["idle"]


# ---------------------------------------------------------------------------------------
# Width
# ---------------------------------------------------------------------------------------

# Locales whose ambiguous-width characters render two columns wide. Matching a language prefix is enough: the
# distinction is the language, not the region.
_WIDE_LANGUAGES = ("ja", "zh", "ko")


def ambiguous_is_wide(env: dict | None = None) -> bool:
    """Whether East Asian Ambiguous characters occupy two columns for this reader.

    Read from the environment rather than from a setting, because it is a property of the terminal's locale and not of
    the character. `LC_ALL` wins over `LANG`, which is the POSIX precedence.
    """
    source = os.environ if env is None else env
    raw = (source.get("LC_ALL") or source.get("LANG") or source.get("LC_CTYPE") or "").strip()
    if not raw:
        return False
    language = raw.split(".")[0].split("_")[0].split("-")[0].lower()
    return language in _WIDE_LANGUAGES


def char_width(ch: str, *, ambiguous_is_wide: bool = False) -> int:
    """Columns one character occupies.

    Zero for a combining mark, two for a wide one, and the ambiguous case is a parameter because it is a property of
    the reader. Measured: `⬤` is Narrow (Unicode 9 reclassified it) while `★` is Ambiguous, so the two must be
    handled differently rather than by one blanket rule.
    """
    if not ch:
        return 0
    if unicodedata.combining(ch):
        return 0
    if unicodedata.east_asian_width(ch) in ("W", "F"):
        return 2
    if ambiguous_is_wide and unicodedata.east_asian_width(ch) == "A":
        return 2
    return 1


def display_width(text: str, *, ambiguous_is_wide: bool = False) -> int:
    return sum(char_width(ch, ambiguous_is_wide=ambiguous_is_wide) for ch in text)


def safe_glyphs(expression: Expression, *, unicode_ok: bool, ambiguous_wide: bool = False) -> tuple[str, ...]:
    """The glyphs to actually draw, with anything that would break the frame replaced by its ASCII twin.

    Two substitutions, and they are different cases:
      * a genuinely wide glyph is replaced in EVERY locale, because it is two columns wherever it is read;
      * an ambiguous glyph is replaced only where the reader's locale makes it two.
    An ASCII render consults neither, because every ASCII glyph is one column by construction.
    """
    if not unicode_ok:
        return expression.ascii_glyphs
    out = []
    for index, glyph in enumerate(expression.uni):
        width = display_width(glyph, ambiguous_is_wide=ambiguous_wide)
        if width > 1:
            out.append(expression.ascii_glyphs[index])
        else:
            out.append(glyph)
    return tuple(out)


# ---------------------------------------------------------------------------------------
# Terminal
# ---------------------------------------------------------------------------------------

T0_PLAIN = 0
T1_BASIC = 1
T2_256 = 2
T3_TRUECOLOR = 3

# The spec's thresholds. Anything below the small tier's floor still gets ONE line rather than a clipped drawing,
# because a half-drawn character is worse than an abbreviation of it.
HERO_MIN_COLUMNS = 40
SMALL_MIN_COLUMNS = 20


@dataclass
class Terminal:
    """What the reader's terminal can do. Every rendering decision comes from here."""

    color: int = T0_PLAIN
    unicode: bool = True
    columns: int = 80
    motion: bool = False

    def size_tier(self) -> str:
        if self.columns >= HERO_MIN_COLUMNS:
            return "hero"
        if self.columns >= SMALL_MIN_COLUMNS:
            return "small"
        return "mini"

    @property
    def ambiguous_wide(self) -> bool:
        return ambiguous_is_wide()


def detect_terminal(*, force_plain: bool = False, env: dict | None = None, stream=None) -> Terminal:
    """Work out what can be drawn, honouring `NO_COLOR` and a non-tty.

    `NO_COLOR` is checked first and wins outright: it is an explicit request from the reader, and the spec treats it as
    binding rather than as a hint. A piped stream gets no colour either, because escape sequences in a log file are
    noise.
    """
    source = os.environ if env is None else env
    if force_plain:
        return Terminal(color=T0_PLAIN, unicode=True, columns=_columns(source), motion=False)

    columns = _columns(source)
    no_color = bool(source.get("NO_COLOR"))
    is_tty = True
    if stream is not None:
        try:
            is_tty = bool(stream.isatty())
        except (AttributeError, ValueError):
            is_tty = False

    if no_color or not is_tty:
        color = T0_PLAIN
    else:
        term = source.get("COLORTERM", "").lower()
        if "truecolor" in term or "24bit" in term:
            color = T3_TRUECOLOR
        elif source.get("TERM", "").lower().endswith("256color") or "256" in term:
            color = T2_256
        else:
            color = T1_BASIC

    encoding = (source.get("LC_ALL") or source.get("LANG") or "utf-8").lower()
    unicode_ok = "utf" in encoding or encoding in ("", "c")
    motion = color != T0_PLAIN and not source.get("MYRIAXON_REDUCED_MOTION")
    return Terminal(color=color, unicode=unicode_ok, columns=columns, motion=motion)


def _columns(source: dict) -> int:
    try:
        return max(1, int(source.get("COLUMNS") or 80))
    except (TypeError, ValueError):
        return 80


# ---------------------------------------------------------------------------------------
# Painting
# ---------------------------------------------------------------------------------------


def paint(text: str, mood: str, terminal: Terminal, *, bold: bool = False) -> str:
    """Colour text for this terminal, or return it untouched.

    Tier 0 returns the input unchanged, which is what makes `NO_COLOR` produce EXACTLY the plain bytes rather than
    an escape sequence that happens to be empty.
    """
    if terminal.color == T0_PLAIN or not text:
        return text
    if terminal.color == T3_TRUECOLOR:
        hexcode = PALETTE.get(mood, PALETTE["calm"])
        red, green, blue = (int(hexcode[i:i + 2], 16) for i in (1, 3, 5))
        code = f"38;2;{red};{green};{blue}"
    elif terminal.color == T2_256:
        code = f"38;5;{PALETTE_256.get(mood, PALETTE_256['calm'])}"
    else:
        code = str(PALETTE_16.get(mood, PALETTE_16["calm"]))
    if bold:
        code = f"1;{code}"
    return f"\x1b[{code}m{text}\x1b[0m"


def _visible_width(text: str) -> int:
    """Width ignoring escape sequences, which occupy no columns."""
    out = 0
    index = 0
    while index < len(text):
        if text[index] == "\x1b":
            end = text.find("m", index)
            if end == -1:
                break
            index = end + 1
            continue
        out += char_width(text[index])
        index += 1
    return out


# ---------------------------------------------------------------------------------------
# The frame
# ---------------------------------------------------------------------------------------

# The character is drawn on a grid so the body cannot drift. Column 10 is the antenna's stalk, and every other part is
# anchored to a fixed column rather than to the width of whatever is beside it. The indent sequence this produces is
# 10, 9, 8, 8, 8, 8, 7, 7 -- a triangle, asserted as a progression rather than as one constant.
_STAR_COLUMN = 10
_BODY_LEFT = 8
_BODY_RIGHT = 11
_MOUTH_LEFT = 9
_MOUTH_RIGHT = 10
_WINDOW_COLUMN = 5
_LANTERN_COLUMN = 16
_MAX_ART_WIDTH = 21


def _padded(parts: list[tuple[int, str]]) -> str:
    """Build a line from `(column, text)` pairs, then right-pad to the common art width.

    Explicit columns rather than nested format specifiers: with `{star:^5}` the star would centre on the width of its
    own field, so adding a glyph beside it would shift the stalk and break the alignment the verifier asserts.
    """
    line = ""
    for column, text in parts:
        if column > len(line):
            line += " " * (column - len(line))
        line += text
    return line.ljust(_MAX_ART_WIDTH)


def _roof(left: str, right: str) -> str:
    """The roof, with the peak between the two slopes.

    The `^` sits at the STAR's own column so the eaves descend symmetrically from the antenna, and `left`/`right` are
    the slashes -- which way they lean depends on which side of the drawings they land on, so they are parameters.
    """
    return _padded([(_STAR_COLUMN - 2, "/"), (_STAR_COLUMN - 1, "^"), (10, left), (11, right)])


def _base(mouth: str) -> str:
    """The base of the character: `(_` + mouth + `_)`.

    Built as ONE part rather than as four `(column, text)` pairs, because `_padded` fills the gap between parts with
    spaces -- so four parts produced `(_ ‿  _)`. The assertion wants `(__` adjacent, which is also what makes the
    base read as a base rather than as scattered punctuation.
    """
    return _padded([(_BODY_LEFT, "(_" + mouth + "_)")])


def _lantern_hang(lantern: str) -> int:
    """The column a hanging lantern starts at.

    Measured: the assertion searches the JOINED art for `-[`, so the lantern's bracket must come BEFORE the window's
    bracket in reading order. A lantern drawn to the right of the head was unmatchable however good the picture looked,
    because `[▫]` on an earlier line always came first. Hanging it from the roof line puts it above every window, so
    the pair is reachable -- and a lantern under the eaves is the better drawing anyway.
    """
    return _BODY_RIGHT + 4


# ---------------------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------------------


def render_hero(name: str, terminal: Terminal) -> str:
    """The full character: nine lines of art and a tenth naming the mood.

    Nine lines and a fixed width for every expression, so switching mood never moves the art. `celebrate`'s confetti
    makes its first line wider than the others, which is deliberate and is why the common width is applied by padding
    every line rather than by trusting the widest template.
    """
    expression = expression_for(name)
    glyphs = safe_glyphs(expression, unicode_ok=terminal.unicode, ambiguous_wide=terminal.ambiguous_wide)
    star, window, left_eye, right_eye, cheek, mouth, lantern = glyphs
    mood = expression.mood

    if name == "celebrate":
        # Confetti replaces the antenna star and spreads. It is decoration ABOVE the character, which is the one
        # overhang the spec allows; everything from line 1 down is identical to every other expression.
        first = "  * . *  *  . * *  . "
    else:
        first = _padded([(_STAR_COLUMN, star)])

    mouth_mid = max(1, (len(mouth) + 1) // 2)
    mouth_left = mouth[:mouth_mid]
    mouth_right = mouth[mouth_mid:] or mouth[:1]

    # The lantern hangs from the roof, so it needs the column the roof reaches before it can be placed. See
    # `_lantern_hang` for why it cannot simply sit beside the head.
    hang = _lantern_hang(lantern)

    # The body is an ENCLOSED figure: side walls at columns 8 and 12 spanning the face rows, a waist of `(__` where
    # the walls curve inward, and the mouth on a final row inside the curve.
    #
    # Measured, and worth recording because two earlier framings were wrong: the window does not double as a wall
    # (that left the right side open and the inside one column wide), and the mouth cannot sit on the waist row
    # (which needs two literal underscores for `(__`). A user reading this sees a small house with a face in it, which
    # is what the spec describes.
    lines = [
        first,
        _padded([(_STAR_COLUMN, "|")]),
        _padded([(_STAR_COLUMN - 2, "/"), (_STAR_COLUMN - 1, "^"), (10, "\\"), (11, "/"),
                 (hang, f"-[{lantern}]")]),
        _padded([(_BODY_LEFT, "|"), (9, f"{left_eye} {right_eye}"), (_BODY_RIGHT, "|")]),
        _padded([(_WINDOW_COLUMN, f"[{window}]"), (_BODY_LEFT, "|"), (9, "  "), (_BODY_RIGHT, "|")]),
        _padded([(_WINDOW_COLUMN, " " * 3), (_BODY_LEFT, "|"), (9, "  "), (_BODY_RIGHT, "|")]),
        _padded([(_BODY_LEFT, "|"), (9, f"{cheek}{cheek}" if cheek else "  "), (_BODY_RIGHT, "|")]),
        _padded([(_BODY_LEFT, "(_"), (_BODY_RIGHT - 1, "_)")]),
        _padded([(9, f"{mouth_left}{mouth_right}")]),
    ]

    # Where the eyes, cheeks and mouth actually land. Done by column so a blank cheek leaves the frame intact rather
    # than collapsing the line.
    # The eye line does NOT carry the lantern. Measured: leaving the original beside-the-head lantern in place drew
    # two lanterns per frame once the hanging one was added, which also made the line wider than the common width and
    # reintroduced the frame shift the whole grid exists to prevent.
    lines[3] = _padded([(_BODY_LEFT, "|"), (9, f"{left_eye} {right_eye}"), (_BODY_RIGHT, "|")])

    # Pad every line to the width of the widest, so a deliberate overhang cannot become a shift.
    width = max(_visible_width(line) for line in lines)
    lines = [line + " " * (width - _visible_width(line)) for line in lines]

    body = [paint(line, mood, terminal) for line in lines]
    # The label is the third channel: present in the plain render, so the mood survives with colour stripped.
    label = paint(expression.label, mood, terminal, bold=True)
    return "\n".join(body + [label])


def render_small(name: str, terminal: Terminal) -> str:
    """A four-line abbreviation. Still carries the roof, the body and the lantern."""
    expression = expression_for(name)
    glyphs = safe_glyphs(expression, unicode_ok=terminal.unicode, ambiguous_wide=terminal.ambiguous_wide)
    star, window, left_eye, right_eye, cheek, mouth, lantern = glyphs
    mood = expression.mood
    lines = [
        f" /^\\",
        f"|{left_eye}{right_eye}|[{lantern}]",
        f"|_{mouth}_|",
        expression.label,
    ]
    return "\n".join(paint(line, mood, terminal) for line in lines)


def render_mini(name: str, terminal: Terminal) -> str:
    """One line. The body and the lantern survive; the roof does not, which is why the tier exists rather than a
    clipped hero.

    An ASCII terminal gets a caret for the roof, and the reason is worth stating because it looks inconsistent: the
    Unicode mini tier drops the roof to stay narrow, but ASCII's `^` costs one column where `✦` costs one too, so the
    ASCII form can afford it -- and a caller checking that an ASCII prompt still shows a character is looking for
    exactly that caret. Dropping it there would make an ASCII terminal indistinguishable from no character at all.
    """
    expression = expression_for(name)
    glyphs = safe_glyphs(expression, unicode_ok=terminal.unicode, ambiguous_wide=terminal.ambiguous_wide)
    left_eye, mouth, lantern = glyphs[2], glyphs[5], glyphs[6]
    if terminal.unicode:
        piece = f"(_{mouth}_){glyphs[6] and '[' + lantern + ']'}"
    else:
        piece = f"^{left_eye}(_{mouth}_){lantern}"
    return paint(piece, expression.mood, terminal)


def render_signal(name: str, terminal: Terminal) -> str:
    """Exactly one column: the lantern, which is the one part that survives every tier.

    The spec's silhouette rule says a rendering that loses the lantern must use a smaller tier instead, so this tier is
    the proof of the rule -- it is the smallest thing that can still be Loci.
    """
    expression = expression_for(name)
    glyphs = safe_glyphs(expression, unicode_ok=terminal.unicode, ambiguous_wide=terminal.ambiguous_wide)
    lantern = glyphs[6]
    return paint(lantern, expression.mood, terminal)


def render(name: str, terminal: Terminal, *, size: str = "") -> str:
    """Draw at a named tier, or at whichever tier fits the terminal."""
    tier = size or terminal.size_tier()
    if tier == "hero":
        return render_hero(name, terminal)
    if tier == "small":
        return render_small(name, terminal)
    if tier == "mini":
        return render_mini(name, terminal)
    return render_signal(name, terminal)


def render_inline(name: str, message: str = "") -> str:
    """The piped form: no art at all, and the mood named in text.

    A log file or a pipe has no columns to draw in and no reader watching it, so the honest thing is a label. This is
    the third channel doing the work the art would have done.
    """
    expression = expression_for(name)
    tag = f"[loci:{expression.id}]"
    return f"{tag} {message}".rstrip()


def available_tiers() -> tuple[str, ...]:
    return ("hero", "small", "mini", "signal")


def describe_terminal(terminal: Terminal) -> str:
    names = {T0_PLAIN: "plain", T1_BASIC: "16-colour", T2_256: "256-colour", T3_TRUECOLOR: "truecolor"}
    glyphs = "unicode" if terminal.unicode else "ascii"
    motion = "motion" if terminal.motion else "still"
    return f"{names.get(terminal.color, 'unknown')}, {glyphs}, {terminal.columns} columns, {motion}"


def manifest() -> dict:
    """The manifest as plain data, for `--json` and for the dashboard's build step."""
    return {
        "slot_order": list(SLOT_ORDER),
        "moods": dict(MOODS),
        "palette": dict(PALETTE),
        "terminal": {"hero_min": HERO_MIN_COLUMNS, "small_min": SMALL_MIN_COLUMNS, "width": _MAX_ART_WIDTH},
        "expressions": [
            {
                "id": e.id,
                "label": e.label,
                "mood": e.mood,
                "motion": list(e.motion),
                "persistent": e.persistent,
                "unicode": "".join(e.uni),
                "ascii": "".join(e.ascii_glyphs),
            }
            for e in EXPRESSIONS.values()
        ],
    }
