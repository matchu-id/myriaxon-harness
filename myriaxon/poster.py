"""Poster design: a spec in, print-ready artwork out.

This is a *designer*, not a text-to-image model. It composes typography, layout and the Myriaxon
palette into a finished poster, which is what makes the output predictable: the same spec always
produces the same poster, the text is always spelled correctly, and nothing is invented that the
caller did not ask for. A generative model is the right tool for illustration and the wrong tool
for a poster whose title must be exact.

Two backends, chosen by what the machine can actually do:

* **SVG** is written directly, always, with no dependencies. It is vector, so it is the better
  master for print and it scales to any size.
* **PNG** is rasterised with Pillow when it is importable. Pillow is an optional extra here
  (``pip install myriaxon[poster]``), and when it is absent the PNG backend reports itself as
  unavailable rather than writing a file that is not what was asked for.

Both backends draw the same design. They are separate drawing routines rather than "render SVG
then convert", because a rasteriser for SVG is a much larger dependency than a drawing library,
and because the two files should not be able to disagree about what they contain.
"""

from __future__ import annotations

import html
import math
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

# --------------------------------------------------------------------------------------
# palette
#
# Read from the brand module rather than repeated, so a poster cannot drift from the product's
# own colours. The literals below are the fallback for the one case where importing fails: a
# poster is still worth producing on a machine where the package is half-installed.
# --------------------------------------------------------------------------------------
_FALLBACK = {
    "signal": "#7CF5C8",
    "ink": "#0A0C11",
    "panel": "#10131A",
    "raise": "#171B24",
    "text": "#EDEFF5",
    "muted": "#A3ABBD",
    "faint": "#7C8599",
    "line": "#232835",
    "line2": "#2E3444",
    "blue": "#8FB4FF",
    "amber": "#FFB86B",
    "violet": "#C9A2FF",
    "red": "#FF8A8A",
}


def palette() -> dict[str, str]:
    """The brand colours, from the single source of truth when it is importable."""
    try:
        from . import brand
    except Exception:  # noqa: BLE001 - a missing brand module must not stop a poster
        return dict(_FALLBACK)
    try:
        colours = getattr(brand, "PALETTE", None) or getattr(brand, "COLORS", None)
        if isinstance(colours, dict):
            merged = dict(_FALLBACK)
            for key, value in colours.items():
                if isinstance(value, str) and value.startswith("#"):
                    merged[str(key).replace("-", "").replace("_", "")] = value
            return merged
    except Exception:  # noqa: BLE001
        pass
    return dict(_FALLBACK)


# --------------------------------------------------------------------------------------
# fonts
#
# Resolved by filename from the system font directory, with a preference order per role. The
# brand's own faces (Unbounded, Instrument Sans, JetBrains Mono) are not installed on a stock
# Windows machine, so the poster uses the closest widely-present face and says which one it used:
# a design that silently falls back to a default font looks broken rather than substituted.
# --------------------------------------------------------------------------------------
FONT_CHOICES: dict[str, tuple[str, ...]] = {
    "display": ("bahnschrift.ttf", "segoeuib.ttf", "arialbd.ttf", "verdana.ttf"),
    "display-light": ("bahnschrift.ttf", "segoeuil.ttf", "segoeui.ttf", "arial.ttf"),
    "body": ("segoeui.ttf", "arial.ttf", "verdana.ttf"),
    "body-bold": ("segoeuib.ttf", "arialbd.ttf", "verdanab.ttf"),
    "mono": ("consola.ttf", "consolab.ttf", "cour.ttf"),
    "serif": ("georgia.ttf", "times.ttf"),
}


def _font_dirs() -> list[Path]:
    import os

    dirs = [Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"]
    local = os.environ.get("LOCALAPPDATA")
    if local:
        dirs.append(Path(local) / "Microsoft" / "Windows" / "Fonts")
    dirs.append(Path("/usr/share/fonts"))
    dirs.append(Path("/Library/Fonts"))
    return dirs


def find_font(role: str) -> Path | None:
    """The first installed file for a typographic role, or None."""
    wanted = FONT_CHOICES.get(role, FONT_CHOICES["body"])
    for directory in _font_dirs():
        if not directory.is_dir():
            continue
        for name in wanted:
            candidate = directory / name
            if candidate.is_file():
                return candidate
        # Case-insensitive sweep, because Windows font directories are inconsistent about it.
        lowered = {p.name.lower(): p for p in directory.glob("*.[to]tf")}
        for name in wanted:
            if name.lower() in lowered:
                return lowered[name.lower()]
    return None


def font_report() -> dict[str, str]:
    """Which file each role resolved to, for the caller to state honestly."""
    out: dict[str, str] = {}
    for role in FONT_CHOICES:
        path = find_font(role)
        out[role] = path.name if path else "unavailable"
    return out


# --------------------------------------------------------------------------------------
# spec
# --------------------------------------------------------------------------------------
@dataclass
class PosterSpec:
    """Everything the designer needs. Only `title` is required.

    Defaults are chosen so that a one-line call produces something presentable, and every field
    is honoured rather than decorative: a value left empty removes its block from the layout
    instead of leaving a gap, which is what keeps a sparse poster from looking unfinished.
    """

    title: str
    subtitle: str = ""
    eyebrow: str = ""
    body: str = ""
    bullets: list[str] = field(default_factory=list)
    footer: str = ""
    mark_text: str = "MYRIAXON"
    theme: str = "ink"            # ink | light | signal
    accent: str = ""              # overrides the palette's signal colour
    size: str = "portrait"        # portrait | story | square | wide | a4
    motif: str = "axon"           # axon | grid | rings | none
    signature: str = ""           # a name or handle, set small at the foot
    date: str = ""

    def normalised(self) -> "PosterSpec":
        """Trim, and reject the combinations that cannot be drawn."""
        cleaned = PosterSpec(
            title=" ".join(str(self.title).split()),
            subtitle=" ".join(str(self.subtitle).split()),
            eyebrow=" ".join(str(self.eyebrow).split()),
            body=str(self.body).strip(),
            bullets=[b for b in (" ".join(str(b).split()) for b in (self.bullets or [])) if b],
            footer=" ".join(str(self.footer).split()),
            mark_text=" ".join(str(self.mark_text or "MYRIAXON").split()),
            theme=str(self.theme or "ink").lower().strip(),
            accent=str(self.accent or "").strip(),
            size=str(self.size or "portrait").lower().strip(),
            motif=str(self.motif or "axon").lower().strip(),
            signature=" ".join(str(self.signature).split()),
            date=" ".join(str(self.date).split()),
        )
        if cleaned.theme not in THEMES:
            cleaned.theme = "ink"
        if cleaned.size not in SIZES:
            cleaned.size = "portrait"
        if cleaned.motif not in MOTIFS:
            cleaned.motif = "axon"
        if cleaned.accent and not _is_hex(cleaned.accent):
            cleaned.accent = ""
        if not cleaned.title:
            raise ValueError("a poster needs a title")
        return cleaned


SIZES: dict[str, tuple[int, int]] = {
    "portrait": (1080, 1350),   # 4:5, the feed-friendly default
    "story": (1080, 1920),      # 9:16
    "square": (1080, 1080),     # 1:1
    "wide": (1920, 1080),       # 16:9, for a slide or a screen
    "a4": (2480, 3508),         # 300 dpi A4, for print
}
THEMES = ("ink", "light", "signal")
MOTIFS = ("axon", "grid", "rings", "none")

# How far the content block may be stretched to fill the page. Past this the gaps read as a
# layout fault rather than as rhythm, and it is better to leave air at the foot of a genuinely
# short poster than to space four lines across a whole sheet.
MAX_LEADING_STRETCH = 1.34

# Print sizing.
#
# A poster written at screen resolution and then printed is soft, and a PDF page is a *physical*
# size -- so the print file has to be generated at a resolution that produces the intended paper
# size, which means the image dimensions and the declared resolution have to agree. Two mistakes were
# made here before this worked:
#
#   * declaring 300 dpi while writing 1080-pixel pixels gave a 3.6-inch page;
#   * scaling the canvas up to 300 dpi and still declaring 300 dpi gave a 25-inch page for A4.
#
# So the target is stated as a physical width, and both the scale and the declared resolution are
# derived from it. There is one number per canvas and it cannot disagree with itself.
PT_PER_INCH = 72.0
BASE_PRINT_DPI = 300

# Intended printed width, in inches, per canvas. `a4` is the real thing; the screen sizes are sized
# to look right on paper rather than to be a particular sheet.
PRINT_TARGET_INCHES: dict[str, float] = {
    "a4": 8.27,        # A4 is 8.27 in wide
    "portrait": 7.2,   # a US Letter / A4-ish sheet with margins
    "story": 7.2,
    "square": 7.2,
    "wide": 12.8,      # 16:9 at the same height as the others
}


def print_target_inches(size: str) -> float:
    return PRINT_TARGET_INCHES.get(size, 7.2)


def print_geometry(size: str, width_px: int, height_px: int) -> tuple[float, int, tuple[float, float]]:
    """(scale, declared_dpi, page_pt) for a canvas.

    Returned together because they are one decision, and the arithmetic is easy to get wrong in a way
    that looks locally sensible. The resolution is *constant* at BASE_PRINT_DPI; the scale is what
    varies, so the printed width is `width_px * scale / BASE_PRINT_DPI` inches.

    Two wrong versions preceded this, both caught by reading the MediaBox back out of the file:

      * declaring 300 dpi while writing 1080-pixel pixels -- a 3.6-inch page;
      * declaring `BASE / scale`, which cancels the scale out and gave 14.4 inches for a canvas whose
        target was 7.2. The declared resolution has to be a property of the *print*, not a function
        of how much the design was enlarged.
    """
    target = print_target_inches(size)
    scale = max(0.05, (target * BASE_PRINT_DPI) / max(1, width_px))
    page = (
        width_px * scale / BASE_PRINT_DPI * PT_PER_INCH,
        height_px * scale / BASE_PRINT_DPI * PT_PER_INCH,
    )
    return scale, BASE_PRINT_DPI, page


def print_dpi(size: str) -> int:
    """The resolution a canvas is declared at for print. Kept for callers that want just that."""
    return print_geometry(size, *SIZES.get(size, (1080, 1350)))[1]


def page_size_pt(width_px: int, height_px: int, dpi: int = BASE_PRINT_DPI) -> tuple[float, float]:
    """A canvas's page size in PostScript points, at the given resolution."""
    return (width_px / dpi * PT_PER_INCH, height_px / dpi * PT_PER_INCH)


def _is_hex(value: str) -> bool:
    text = value.lstrip("#")
    return len(text) in (3, 6) and all(c in "0123456789abcdefABCDEF" for c in text)


def theme_colours(theme: str, accent: str = "") -> dict[str, str]:
    """Background, panel and text colours for a theme, with the accent applied."""
    base = palette()
    signal = accent if accent and _is_hex(accent) else base["signal"]
    if theme == "light":
        return {
            "bg": "#F7F8FA", "panel": "#FFFFFF", "text": "#0A0C11", "muted": "#5A6273",
            "faint": "#8A92A6", "line": "#DDE1E8", "line2": "#C7CDD8",
            "signal": _darken(signal, 0.42), "accent": _darken(signal, 0.42),
        }
    if theme == "signal":
        return {
            "bg": signal, "panel": _darken(signal, 0.08), "text": "#07130E", "muted": "#123323",
            "faint": "#1D4632", "line": _darken(signal, 0.18), "line2": _darken(signal, 0.28),
            "signal": "#07130E", "accent": "#07130E",
        }
    return {
        "bg": base["ink"], "panel": base["panel"], "text": base["text"], "muted": base["muted"],
        "faint": base["faint"], "line": base["line"], "line2": base["line2"],
        "signal": signal, "accent": signal,
    }


def _hex_to_rgb(value: str) -> tuple[int, int, int]:
    text = value.lstrip("#")
    if len(text) == 3:
        text = "".join(c * 2 for c in text)
    return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)


def _mix(a: str, b: str, amount: float) -> str:
    ar, ag, ab = _hex_to_rgb(a)
    br, bg, bb = _hex_to_rgb(b)
    amount = max(0.0, min(1.0, amount))
    return "#%02X%02X%02X" % (
        round(ar + (br - ar) * amount),
        round(ag + (bg - ag) * amount),
        round(ab + (bb - ab) * amount),
    )


def _darken(value: str, amount: float) -> str:
    return _mix(value, "#000000", amount)


def _lighten(value: str, amount: float) -> str:
    return _mix(value, "#FFFFFF", amount)


# --------------------------------------------------------------------------------------
# layout
#
# One layout engine, two renderers. Producing a list of drawing primitives means the SVG and the
# PNG cannot disagree: there is a single description of the poster and each backend is only a
# way of writing it down.
# --------------------------------------------------------------------------------------
@dataclass
class Text:
    x: float
    y: float
    text: str
    size: int
    role: str
    colour: str
    anchor: str = "start"       # start | middle | end
    weight: str = "normal"
    tracking: float = 0.0       # extra letter spacing, in px


@dataclass
class Rect:
    x: float
    y: float
    w: float
    h: float
    fill: str
    radius: float = 0.0
    opacity: float = 1.0
    stroke: str = ""
    stroke_width: float = 0.0


@dataclass
class Circle:
    cx: float
    cy: float
    r: float
    fill: str = ""
    stroke: str = ""
    stroke_width: float = 1.0
    opacity: float = 1.0


@dataclass
class Line:
    x1: float
    y1: float
    x2: float
    y2: float
    stroke: str
    width: float = 1.0
    opacity: float = 1.0
    dash: str = ""


Primitive = Text | Rect | Circle | Line


@dataclass
class Layout:
    width: int
    height: int
    background: str
    primitives: list[Primitive]
    fonts_used: dict[str, str] = field(default_factory=dict)
    # How far the copy overruns the space between the header rule and the foot, in pixels. Zero for
    # a composition that fits. Surfaced so an over-full poster is reported rather than discovered.
    overflow: float = 0.0


def _wrap_to_width(text: str, size: int, max_width: float, per_char: float = 0.52) -> list[str]:
    """Greedy wrap using an average advance width.

    An approximation, deliberately: measuring exactly needs the font metrics, which only the PNG
    backend has, and the two backends must produce the same layout. 0.52em is the average advance
    of the faces used here, so the wrap is right for normal prose and only differs for strings of
    unusually wide or narrow glyphs.
    """
    if not text:
        return []
    chars = max(8, int(max_width / max(1.0, size * per_char)))
    lines: list[str] = []
    for paragraph in text.split("\n"):
        paragraph = paragraph.strip()
        if not paragraph:
            lines.append("")
            continue
        lines.extend(textwrap.wrap(paragraph, width=chars, break_long_words=False) or [""])
    return lines


def _fit_title(title: str, max_width: float, max_lines: int = 3) -> tuple[list[str], int]:
    """The largest title size at which the title fits in `max_lines`.

    Shrinking to fit is what stops a long title from being clipped or spilling out of the margin,
    which is the single most common way a generated poster looks broken.
    """
    for size in range(132, 30, -6):
        lines = _wrap_to_width(title, size, max_width)
        if len(lines) <= max_lines:
            return lines, size
    return _wrap_to_width(title, 36, max_width)[:max_lines], 36


def build_layout(spec: PosterSpec) -> Layout:
    """The whole design, as primitives. Deterministic: same spec, same poster."""
    width, height = SIZES[spec.size]
    colours = theme_colours(spec.theme, spec.accent)
    margin = round(width * 0.072)
    content_w = width - margin * 2
    primitives: list[Primitive] = []

    # --- ground -----------------------------------------------------------------------
    primitives.append(Rect(0, 0, width, height, colours["bg"]))

    # The header's type size is needed by the motif (to keep clear of the date) and by the header
    # itself, so it is resolved once, here, rather than in the order the blocks happen to be drawn.
    head_size = max(15, round(width * 0.0175))

    if spec.motif == "grid":
        step = round(width / 16)
        for x in range(step, width, step):
            primitives.append(Line(x, 0, x, height, colours["line"], 1, 0.5))
        for y in range(step, height, step):
            primitives.append(Line(0, y, width, y, colours["line"], 1, 0.5))
    elif spec.motif == "rings":
        cx, cy = width * 0.82, height * 0.2
        for index, radius in enumerate(range(round(width * 0.10), round(width * 0.72), round(width * 0.075))):
            primitives.append(Circle(cx, cy, radius, stroke=colours["line2"], stroke_width=1.4,
                                     opacity=max(0.10, 0.5 - index * 0.055)))
    elif spec.motif == "axon":
        # A nervous-system motif: a spine with branching fibres, echoing the product's own mark.
        # Held inside the right edge and stopped short of the foot, because the fibres previously
        # ran through the footer rule and behind the signature -- decoration crossing the fine
        # print looked like a rendering fault rather than a design.
        #
        # The top of the spine starts below the header rule, and its branches stop short of the
        # date: a mint node landing on top of "OCTOBER 2026" made the date unreadable, and a
        # decorative layer must never win against text.
        spine_x = width * 0.885
        top = height * 0.055
        bottom = height * 0.855
        # Horizontal limit for the fibres: the date sits at the right margin on the header line, so
        # the spine is pulled left of it rather than allowed to run through it. Measured: on a square
        # canvas the spine landed between "2026" and "-10-01", splitting the date in half.
        date_guard = width - margin - (len(spec.date) + 6) * head_size * 0.62 if spec.date else width
        if date_guard < spine_x:
            spine_x = max(width * 0.62, date_guard)
        primitives.append(Line(spine_x, top, spine_x, bottom, colours["line2"], 2, 0.5))
        for index in range(7):
            y = top + (bottom - top) * (index / 6.0)
            reach = min(width * (0.085 + 0.040 * ((index * 7) % 5)),
                        max(width * 0.05, spine_x - date_guard))
            bend = reach * 0.42
            primitives.append(Line(spine_x, y, spine_x - bend, y + reach * 0.30, colours["line2"], 1.6, 0.45))
            primitives.append(Line(spine_x - bend, y + reach * 0.30, spine_x - reach, y + reach * 0.16,
                                   colours["line2"], 1.6, 0.32))
            primitives.append(Circle(spine_x - reach, y + reach * 0.16, 4.4, fill=colours["accent"], opacity=0.8))
        for index in range(4):
            y = top + (bottom - top) * (0.14 + index * 0.24)
            primitives.append(Circle(spine_x, y, 3.2, fill=colours["accent"], opacity=0.65))

    # --- header -----------------------------------------------------------------------
    # The header and the foot are fixed to their margins; only the content between them moves.
    # Building the content into its own list first is what makes that possible: its height is not
    # known until it has been composed, and without measuring it there is no way to distribute the
    # leftover space instead of dumping all of it above the footer.
    content: list[Primitive] = []
    cursor = 0.0
    header: list[Primitive] = []
    if spec.mark_text:
        header.append(Rect(margin, margin, head_size * 0.42, head_size * 0.42, colours["accent"], 3))
        header.append(Text(margin + head_size * 0.85, margin + head_size * 0.78,
                           spec.mark_text.upper(), head_size, "body-bold", colours["text"],
                           tracking=head_size * 0.16))
    if spec.date:
        header.append(Text(width - margin, margin + head_size * 0.78, spec.date.upper(),
                           head_size, "mono", colours["faint"], anchor="end", tracking=head_size * 0.08))
    rule_y = margin + head_size * 2.4
    header.append(Line(margin, rule_y, width - margin, rule_y, colours["line"], 1.5, 0.9))

    # --- eyebrow ----------------------------------------------------------------------
    if spec.eyebrow:
        eye_size = max(16, round(width * 0.0195))
        content.append(Rect(margin, cursor, eye_size * 1.5, 3, colours["accent"], 2))
        content.append(Text(margin + eye_size * 2.1, cursor + eye_size * 0.1, spec.eyebrow.upper(),
                            eye_size, "mono", colours["accent"], tracking=eye_size * 0.22))
        cursor += eye_size * 1.9

    # --- title ------------------------------------------------------------------------
    title_lines, title_size = _fit_title(spec.title, content_w)
    line_height = title_size * 1.03
    for index, line in enumerate(title_lines):
        content.append(Text(margin, cursor + title_size * 0.82 + index * line_height, line,
                            title_size, "display", colours["text"], tracking=-title_size * 0.012))
    cursor += line_height * len(title_lines) + height * 0.018

    # --- subtitle ---------------------------------------------------------------------
    if spec.subtitle:
        sub_size = max(20, round(width * 0.0275))
        for line in _wrap_to_width(spec.subtitle, sub_size, content_w):
            content.append(Text(margin, cursor + sub_size * 0.82, line, sub_size, "display-light",
                                colours["muted"]))
            cursor += sub_size * 1.34
        cursor += height * 0.012

    # --- rule -------------------------------------------------------------------------
    content.append(Rect(margin, cursor, min(content_w * 0.30, width * 0.22),
                        max(3, round(height * 0.0035)), colours["accent"], 2))
    cursor += height * 0.036

    # --- body -------------------------------------------------------------------------
    if spec.body:
        body_size = max(18, round(width * 0.0225))
        for line in _wrap_to_width(spec.body, body_size, content_w * 0.88):
            content.append(Text(margin, cursor + body_size * 0.8, line, body_size, "body", colours["muted"]))
            cursor += body_size * 1.52
        cursor += height * 0.016

    # --- bullets ----------------------------------------------------------------------
    if spec.bullets:
        bullet_size = max(17, round(width * 0.0215))
        for item in spec.bullets:
            content.append(Circle(margin + bullet_size * 0.22, cursor + bullet_size * 0.52,
                                  bullet_size * 0.20, fill=colours["accent"]))
            wrapped = _wrap_to_width(item, bullet_size, content_w - bullet_size * 1.6)
            for index, line in enumerate(wrapped):
                content.append(Text(margin + bullet_size * 1.35, cursor + bullet_size * 0.8, line,
                                    bullet_size, "body", colours["text" if index == 0 else "muted"]))
                cursor += bullet_size * 1.42
            cursor += bullet_size * 0.42

    # --- place the content ------------------------------------------------------------
    # The text block is justified to the space between the header rule and the foot, the way a
    # page is set rather than merely poured. A measured void at the foot is the difference between
    # a poster and a paragraph on a coloured rectangle: the first attempt left 600px of empty
    # ground under the last bullet.
    #
    # The stretch is applied to *vertical* offsets only, so glyphs keep their proportions -- this
    # is leading and paragraph spacing, not a scale transform. It is capped, because past about a
    # third again the gaps stop reading as intentional rhythm and start reading as a bug.
    foot_size = max(15, round(width * 0.0165))
    foot_y = height - margin
    foot_top = (foot_y - foot_size * 2.2) if (spec.footer or spec.signature) else foot_y
    body_top = rule_y + height * 0.055
    available = foot_top - body_top
    stretch = 1.0
    if cursor > 0:
        stretch = min(MAX_LEADING_STRETCH, max(1.0, available / cursor))
    placed_height = cursor * stretch
    offset = max(0.0, (available - placed_height) * 0.5)
    for item in content:
        _shift(item, 0.0, body_top + offset)
        _stretch_vertical(item, body_top + offset, stretch)
    primitives.extend(header)
    primitives.extend(content)

    # --- foot -------------------------------------------------------------------------
    if spec.footer or spec.signature:
        primitives.append(Line(margin, foot_y - foot_size * 2.2, width - margin,
                               foot_y - foot_size * 2.2, colours["line"], 1.2, 0.85))
    if spec.footer:
        for index, line in enumerate(_wrap_to_width(spec.footer, foot_size, content_w * 0.66)):
            primitives.append(Text(margin, foot_y - foot_size * 0.5 + index * foot_size * 1.35,
                                   line, foot_size, "body", colours["faint"]))
    # The elapsed step is measured, not assumed: `cursor` is the height of the content block, and
    # if it overruns the available space the poster is over-full. Reporting it lets the caller
    # shrink the copy rather than discovering the overflow by eye.
    overflow = 0.0
    if cursor > available:
        overflow = cursor - available
    layout = Layout(width=width, height=height, background=colours["bg"], primitives=primitives,
                    fonts_used=font_report())
    layout.overflow = overflow
    return layout


def _shift(item: Primitive, dx: float, dy: float) -> None:
    """Move a primitive in place. Keeps the two-pass layout from needing a second data model."""
    if isinstance(item, Text):
        item.x += dx
        item.y += dy
    elif isinstance(item, Rect):
        item.x += dx
        item.y += dy
    elif isinstance(item, Circle):
        item.cx += dx
        item.cy += dy
    elif isinstance(item, Line):
        item.x1 += dx
        item.y1 += dy
        item.x2 += dx
        item.y2 += dy


def _stretch_vertical(item: Primitive, origin: float, factor: float) -> None:
    """Scale a primitive's vertical *distance from the origin*, leaving its own size alone.

    Applied after `_shift`, so the origin is the top of the content block and the arithmetic is
    simply "how far down the block is this, times the factor".
    """
    if factor == 1.0:
        return
    if isinstance(item, Text):
        item.y = origin + (item.y - origin) * factor
    elif isinstance(item, Rect):
        item.y = origin + (item.y - origin) * factor
    elif isinstance(item, Circle):
        item.cy = origin + (item.cy - origin) * factor
    elif isinstance(item, Line):
        item.y1 = origin + (item.y1 - origin) * factor
        item.y2 = origin + (item.y2 - origin) * factor


# --------------------------------------------------------------------------------------
# SVG backend
# --------------------------------------------------------------------------------------
def _svg_escape(value: str) -> str:
    return html.escape(str(value), quote=True)


def render_svg(layout: Layout) -> str:
    """The poster as SVG. No dependencies, so this backend always works."""
    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{layout.width}" height="{layout.height}" '
        f'viewBox="0 0 {layout.width} {layout.height}" role="img">',
        f'<rect width="{layout.width}" height="{layout.height}" fill="{layout.background}"/>',
    ]
    for item in layout.primitives:
        if isinstance(item, Rect):
            radius = f' rx="{item.radius:.1f}"' if item.radius else ""
            stroke = f' stroke="{item.stroke}" stroke-width="{item.stroke_width:.1f}"' if item.stroke else ""
            opacity = f' opacity="{item.opacity:.3f}"' if item.opacity < 1 else ""
            parts.append(f'<rect x="{item.x:.1f}" y="{item.y:.1f}" width="{item.w:.1f}" '
                         f'height="{item.h:.1f}" fill="{item.fill}"{radius}{stroke}{opacity}/>')
        elif isinstance(item, Circle):
            fill = f'fill="{item.fill}"' if item.fill else 'fill="none"'
            stroke = f' stroke="{item.stroke}" stroke-width="{item.stroke_width:.1f}"' if item.stroke else ""
            opacity = f' opacity="{item.opacity:.3f}"' if item.opacity < 1 else ""
            parts.append(f'<circle cx="{item.cx:.1f}" cy="{item.cy:.1f}" r="{item.r:.1f}" '
                         f'{fill}{stroke}{opacity}/>')
        elif isinstance(item, Line):
            dash = f' stroke-dasharray="{item.dash}"' if item.dash else ""
            opacity = f' opacity="{item.opacity:.3f}"' if item.opacity < 1 else ""
            parts.append(f'<line x1="{item.x1:.1f}" y1="{item.y1:.1f}" x2="{item.x2:.1f}" '
                         f'y2="{item.y2:.1f}" stroke="{item.stroke}" stroke-width="{item.width:.1f}"'
                         f'{dash}{opacity}/>')
        elif isinstance(item, Text):
            anchor = {"start": "start", "middle": "middle", "end": "end"}[item.anchor]
            tracking = f' letter-spacing="{item.tracking:.2f}"' if item.tracking else ""
            weight = f' font-weight="{item.weight}"' if item.weight != "normal" else ""
            family = "'Bahnschrift','Segoe UI',system-ui,-apple-system,'Helvetica Neue',Arial,sans-serif"
            parts.append(
                f'<text x="{item.x:.1f}" y="{item.y:.1f}" font-family="{family}" '
                f'font-size="{item.size}" fill="{item.colour}" text-anchor="{anchor}"'
                f'{weight}{tracking}>{_svg_escape(item.text)}</text>'
            )
    parts.append("</svg>")
    return "\n".join(parts)


# --------------------------------------------------------------------------------------
# PNG backend
# --------------------------------------------------------------------------------------
def png_available() -> bool:
    try:
        import PIL  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return True


_FONT_CACHE: dict[tuple[str, int], Any] = {}


def _pil_font(role: str, size: int):
    from PIL import ImageFont

    key = (role, size)
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]
    path = find_font(role)
    font = None
    if path is not None:
        try:
            font = ImageFont.truetype(str(path), size)
        except Exception:  # noqa: BLE001 - a broken font file must not stop the render
            font = None
    if font is None:
        font = ImageFont.load_default()
    _FONT_CACHE[key] = font
    return font


def _measure(draw, text: str, font) -> float:
    try:
        return draw.textlength(text, font=font)
    except Exception:  # noqa: BLE001
        return len(text) * 0.5 * getattr(font, "size", 10)


def render_png(layout: Layout, scale: float = 1.0) -> Any:
    """The poster as a Pillow image, or None when Pillow is unavailable.

    Returns the image rather than bytes so the caller decides the file format and quality.
    """
    if not png_available():
        return None
    from PIL import Image, ImageDraw

    width = max(1, round(layout.width * scale))
    height = max(1, round(layout.height * scale))
    image = Image.new("RGB", (width, height), layout.background)
    draw = ImageDraw.Draw(image, "RGBA")

    for item in layout.primitives:
        if isinstance(item, Rect):
            box = [item.x * scale, item.y * scale, (item.x + item.w) * scale, (item.y + item.h) * scale]
            radius = item.radius * scale
            fill = _rgba(item.fill, item.opacity)
            if radius > 0:
                draw.rounded_rectangle(box, radius=radius, fill=fill,
                                       outline=_rgba(item.stroke, 1.0) if item.stroke else None,
                                       width=max(1, round(item.stroke_width * scale)) if item.stroke else 0)
            else:
                draw.rectangle(box, fill=fill,
                               outline=_rgba(item.stroke, 1.0) if item.stroke else None,
                               width=max(1, round(item.stroke_width * scale)) if item.stroke else 0)
        elif isinstance(item, Circle):
            box = [(item.cx - item.r) * scale, (item.cy - item.r) * scale,
                   (item.cx + item.r) * scale, (item.cy + item.r) * scale]
            draw.ellipse(box,
                         fill=_rgba(item.fill, item.opacity) if item.fill else None,
                         outline=_rgba(item.stroke, 1.0) if item.stroke else None,
                         width=max(1, round(item.stroke_width * scale)) if item.stroke else 1)
        elif isinstance(item, Line):
            draw.line([item.x1 * scale, item.y1 * scale, item.x2 * scale, item.y2 * scale],
                      fill=_rgba(item.stroke, item.opacity), width=max(1, round(item.width * scale)))
        elif isinstance(item, Text):
            font = _pil_font(item.role, max(6, round(item.size * scale)))
            x, y = item.x * scale, item.y * scale
            if item.tracking:
                _draw_tracked(draw, item, font, x, y, scale)
                continue
            anchor = {"start": "ls", "middle": "ms", "end": "rs"}[item.anchor]
            draw.text((x, y), item.text, font=font, fill=item.colour, anchor=anchor)
    return image


def _draw_tracked(draw, item: Text, font, x: float, y: float, scale: float) -> None:
    """Draw text with extra letter spacing.

    Pillow has no letter-spacing option, so tracked text is placed glyph by glyph. The anchors are
    resolved against the *tracked* width, otherwise a right-aligned tracked string overflows its
    margin -- which is exactly what the header's date and the eyebrow do.
    """
    spacing = item.tracking * scale
    widths = [draw.textlength(ch, font=font) for ch in item.text]
    total = sum(widths) + spacing * max(0, len(item.text) - 1)
    if item.anchor == "middle":
        cursor = x - total / 2
    elif item.anchor == "end":
        cursor = x - total
    else:
        cursor = x
    for ch, advance in zip(item.text, widths):
        draw.text((cursor, y), ch, font=font, fill=item.colour, anchor="ls")
        cursor += advance + spacing


def _rgba(colour: str, opacity: float = 1.0):
    if not colour:
        return None
    red, green, blue = _hex_to_rgb(colour)
    return (red, green, blue, max(0, min(255, round(opacity * 255))))


# --------------------------------------------------------------------------------------
# public entry points
# --------------------------------------------------------------------------------------
@dataclass
class PosterResult:
    title: str
    size: str
    theme: str
    written: list[str]
    fonts: dict[str, str]
    width: int
    height: int
    overflow: float = 0.0
    # The PDF page size in points, when one was produced. Reported so a caller can tell a print file
    # that is actually printable from one that is a screenshot with a .pdf extension.
    page_pt: tuple[float, float] | None = None
    print_dpi: int = 0

    def summary(self) -> str:
        files = ", ".join(self.written) if self.written else "nothing"
        lines = [
            f'Poster "{self.title}" — {self.size} {self.width}×{self.height}, {self.theme} theme.',
            f"Wrote: {files}",
            f"Fonts: display={self.fonts.get('display')}, body={self.fonts.get('body')}, "
            f"mono={self.fonts.get('mono')}",
        ]
        if self.page_pt:
            inches = (self.page_pt[0] / 72.0, self.page_pt[1] / 72.0)
            lines.append(
                f"Print: {inches[0]:.1f}×{inches[1]:.1f} in at {self.print_dpi} dpi "
                f"({self.page_pt[0]:.0f}×{self.page_pt[1]:.0f} pt)"
            )
        if self.overflow > 0:
            # Reported because the only other way to find out is to open the file and notice the
            # last line sitting under the footer rule.
            lines.append(
                f"Warning: the copy overruns the page by about {round(self.overflow)}px. "
                "Shorten the body or the bullets, or drop one block."
            )
        return "\n".join(lines)


def design_poster(
    spec: PosterSpec | dict[str, Any],
    out_dir: str | Path,
    *,
    stem: str = "poster",
    formats: Iterable[str] = ("svg", "png"),
    scale: float = 1.0,
) -> PosterResult:
    """Design a poster and write it. Returns what was actually produced.

    `formats` is honoured as a request, not a promise: a PNG is silently impossible without
    Pillow, and the result reports only the files that exist, so a caller cannot be told a PNG was
    made when it was not.
    """
    if isinstance(spec, dict):
        known = {f for f in PosterSpec.__dataclass_fields__}
        unknown = sorted(set(spec) - known)
        if unknown:
            raise ValueError(
                f"unknown poster field(s): {', '.join(unknown)}. "
                f"Valid fields: {', '.join(sorted(known))}"
            )
        spec = PosterSpec(**spec)
    spec = spec.normalised()

    layout = build_layout(spec)
    directory = Path(out_dir)
    directory.mkdir(parents=True, exist_ok=True)

    wanted = {str(f).lower().lstrip(".") for f in formats}
    written: list[str] = []
    # Vector first: an SVG is the master, and a caller that asked for it should get it even if a
    # later, heavier format fails.
    if "svg" in wanted:
        target = directory / f"{stem}.svg"
        target.write_text(render_svg(layout), encoding="utf-8", newline="\n")
        written.append(str(target))

    # The PNG is drawn at print resolution when a PDF is also wanted, so the page and the image are
    # the same artwork at the same quality rather than one being an upscaled copy of the other.
    # Scale, declared resolution and page size come from one call: they are a single decision, and
    # taking them from separate places is how the page ended up 25 inches wide.
    pdf_scale, pdf_dpi, pdf_page = print_geometry(spec.size, layout.width, layout.height)
    image = None
    needs_pixels = bool(wanted & {"png", "pdf"})
    if needs_pixels:
        effective_scale = scale
        if "pdf" in wanted and scale <= 1.0:
            effective_scale = pdf_scale
        image = render_png(layout, scale=effective_scale)

    if image is not None and "png" in wanted:
        target = directory / f"{stem}.png"
        image.save(target, "PNG", optimize=True)
        written.append(str(target))

    page = pdf_page
    if image is not None and "pdf" in wanted:
        target = directory / f"{stem}.pdf"
        try:
            image.save(target, "PDF", resolution=pdf_dpi, title=spec.title)
            written.append(str(target))
        except Exception:  # noqa: BLE001
            # A PDF the writer refuses is reported by its absence: `written` lists only files that
            # exist, so a caller cannot be told a print file was made when it was not.
            page = None

    return PosterResult(title=spec.title, size=spec.size, theme=spec.theme, written=written,
                        fonts=layout.fonts_used, width=layout.width, height=layout.height,
                        overflow=layout.overflow,
                        page_pt=page if any(f.endswith(".pdf") for f in written) else None,
                        print_dpi=pdf_dpi)


# --------------------------------------------------------------------------------------
# plugin-facing tool handler
# --------------------------------------------------------------------------------------
POSTER_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "The poster's headline. Required."},
        "subtitle": {"type": "string", "description": "One supporting line under the headline."},
        "eyebrow": {"type": "string", "description": "Small label above the headline, e.g. 'RELEASE 1.2'."},
        "body": {"type": "string", "description": "A short paragraph of body copy."},
        "bullets": {
            "type": "array", "items": {"type": "string"},
            "description": "Up to about five short points, drawn as a list.",
        },
        "footer": {"type": "string", "description": "Small print along the foot."},
        "signature": {"type": "string", "description": "A name or handle, set at the bottom right."},
        "date": {"type": "string", "description": "Text for the top right, e.g. 'OCTOBER 2026'."},
        "mark_text": {"type": "string", "description": "Wordmark in the top left. Defaults to MYRIAXON."},
        "theme": {"type": "string", "enum": list(THEMES), "description": "ink (dark), light, or signal (accent-filled)."},
        "accent": {"type": "string", "description": "Hex colour overriding the brand accent, e.g. '#FFB86B'."},
        "size": {"type": "string", "enum": list(SIZES), "description": "Canvas: portrait 4:5, story 9:16, square, wide 16:9, a4 print."},
        "motif": {"type": "string", "enum": list(MOTIFS), "description": "Background treatment."},
        "out_dir": {"type": "string", "description": "Directory to write into. Defaults to the workspace."},
        "stem": {"type": "string", "description": "Filename without extension. Defaults to a slug of the title."},
        "formats": {
            "type": "array", "items": {"type": "string", "enum": ["svg", "png", "pdf"]},
            "description": (
                "Which files to write. SVG needs no dependencies; PNG and PDF need the poster "
                "extra (Pillow). The PDF is a print-ready page at 300 dpi, sized from the canvas, "
                "so `a4` really is A4."
            ),
        },
    },
    "required": ["title"],
}


def _closest(word: str, candidates: Iterable[str]) -> str:
    """The nearest candidate by edit distance, when it is near enough to be worth suggesting.

    A "did you mean" that guesses wildly is worse than none, so this only offers a suggestion within
    a small edit distance of the word actually written.
    """
    best = ""
    best_distance = 3
    for candidate in candidates:
        distance = _edit_distance(word.lower(), candidate.lower())
        if distance < best_distance:
            best, best_distance = candidate, distance
    return best


def _edit_distance(a: str, b: str) -> int:
    if a == b:
        return 0
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        current = [i]
        for j, cb in enumerate(b, start=1):
            current.append(min(previous[j] + 1, current[j - 1] + 1,
                               previous[j - 1] + (ca != cb)))
        previous = current
    return previous[-1]


def _slug(text: str) -> str:
    keep = [c.lower() if c.isalnum() else "-" for c in text]
    slug = "".join(keep)
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug.strip("-")[:60] or "poster"


POSTER_TOOL_DEF = {
    "name": "design_poster",
    "description": (
        "Design a finished poster and write it as SVG and PNG. Composes typography, layout and the "
        "brand palette into print-ready artwork — this draws, it does not generate an image, so the "
        "text is exactly what you pass and the same input always produces the same poster.\n\n"
        "Use it for a launch card, an event flyer, a quote card, a cover, or a social post. Put the "
        "headline in `title`; everything else is optional and an omitted block is removed from the "
        "layout rather than left as a gap. Pick `size` for the destination (portrait for a feed, "
        "story for a phone screen, wide for a slide, a4 for print).\n\n"
        "Aim for one idea: a short title, at most a sentence of body, and up to about five bullets. "
        "If the result reports that the copy overruns the page, cut a block rather than shrinking "
        "the type by hand — the designer already sizes the title to fit."
    ),
    "input_schema": POSTER_TOOL_SCHEMA,
}


def poster_tool(tool_input: dict, ctx: Any = None) -> tuple[str, bool]:
    """Handler for the `design_poster` tool. Returns (message, is_error).

    The workspace comes from the process working directory, matching every other file tool in this
    codebase (`tools.py` resolves write paths against `Path.cwd()`), rather than from the context:
    `ToolContext` carries no workspace, and inventing one here would mean posters landed somewhere
    the rest of the agent's tools cannot see.
    """
    workspace = Path.cwd()
    for attribute in ("workspace", "root"):
        candidate = getattr(ctx, attribute, None)
        if candidate:
            workspace = Path(candidate)
            break

    out_dir = tool_input.get("out_dir") or (workspace / "posters")
    formats = tool_input.get("formats") or ["svg", "png"]
    # Reject unknown fields here, in the tool, rather than relying on the dispatcher.
    #
    # The registry validates the *required* fields from a tool's schema and passes everything else
    # through untouched, so a misspelled "titel" would otherwise be dropped in silence and the
    # poster would come back untitled -- an error the caller only notices by looking at the file.
    # A model that misremembers a field name is exactly the case worth being loud about.
    #
    # `formats` and `out_dir` and `stem` are tool-level, not part of the design spec, so `spec_fields`
    # is the design dataclass plus those three.
    known = set(PosterSpec.__dataclass_fields__) | {"out_dir", "stem", "formats"}
    unknown = sorted(set(tool_input) - known)
    if unknown:
        did_you_mean = _closest(unknown[0], known)
        hint = f" Did you mean {did_you_mean!r}?" if did_you_mean else ""
        return (
            f"Unknown poster field(s): {', '.join(unknown)}.{hint} "
            f"Valid fields are: {', '.join(sorted(known))}.",
            True,
        )

    needs_pillow = [f for f in formats if str(f).lower().lstrip(".") in ("png", "pdf")]
    if needs_pillow and not png_available():
        # Say so plainly instead of quietly writing only an SVG: the caller asked for a raster or
        # print file and needs to know it did not get one, and how to get it.
        requested = ", ".join(str(f) for f in needs_pillow)
        formats = [f for f in formats if str(f).lower().lstrip(".") not in ("png", "pdf")]
        note = (f"\nNote: {requested} output needs Pillow. Install it with "
                "`pip install pillow` (or `pip install myriaxon[poster]`) and run again.")
    else:
        note = ""

    try:
        result = design_poster(
            {
                key: value
                for key, value in tool_input.items()
                if key in PosterSpec.__dataclass_fields__
            },
            out_dir,
            stem=tool_input.get("stem") or _slug(tool_input.get("title", "poster")),
            formats=formats,
        )
    except ValueError as exc:
        return str(exc), True
    except Exception as exc:  # noqa: BLE001
        return f"poster design failed: {type(exc).__name__}: {exc}", True

    return result.summary() + note, False
