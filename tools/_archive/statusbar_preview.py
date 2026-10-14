"""Render an exact mock of the dashboard status bar, from the brand tokens and the page's own markup.

Chrome cannot be launched in this environment, so nothing visual can be screenshotted. This draws the
bar directly with Pillow using the same colours, type sizes and spacing as the CSS, and reads them
from `myriaxon.brand` rather than repeating hex codes -- so it cannot show a palette the product does
not have.

It is a mock, and it is honest about that: it proves the *layout* reads correctly (grouping, rules,
alignment, the stat cluster) and cannot prove how a real browser lays out a `<select>`.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from myriaxon import brand  # noqa: E402

FONTS = Path(r"C:\Windows\Fonts")
OUT = REPO / "bench-out"

# Font stacks from brand, mapped to what is actually installed on this machine. The brand faces are
# not present, which is the same fallback the poster tool documents.
MONO = FONTS / "consola.ttf"
UI = FONTS / "segoeui.ttf"
UI_BOLD = FONTS / "seguisb.ttf"
DISPLAY = FONTS / "bahnschrift.ttf"

SCALE = 2          # drawn at 2x and downsampled, so the hairlines and small caps stay crisp
WIDTH = 1500


def hex_rgb(value: str) -> tuple[int, int, int]:
    text = value.lstrip("#")
    if len(text) == 3:
        text = "".join(c * 2 for c in text)
    return tuple(int(text[i:i + 2], 16) for i in (0, 2, 4))


def main() -> int:
    from PIL import Image, ImageDraw, ImageFont

    def font(path: Path, size: int):
        return ImageFont.truetype(str(path), size * SCALE)

    f_label = font(UI_BOLD, 9)
    f_value = font(MONO, 12)
    f_key = font(UI_BOLD, 9)
    f_stat = font(MONO, 12)

    bg = hex_rgb(brand.INK_2)
    panel = hex_rgb(brand.INK_3)
    line = hex_rgb(brand.LINE)
    text = hex_rgb(brand.TEXT)
    faint = hex_rgb(brand.TEXT_3)
    signal = hex_rgb(brand.SIGNAL)
    amber = hex_rgb(brand.AMBER)

    height = 62
    image = Image.new("RGB", (WIDTH * SCALE, height * SCALE), bg)
    draw = ImageDraw.Draw(image)

    pad = 10 * SCALE
    bar = (pad, 8 * SCALE, WIDTH * SCALE - pad, (height - 8) * SCALE)
    draw.rounded_rectangle(bar, radius=11 * SCALE, fill=panel, outline=line, width=SCALE)

    def text_w(s: str, f) -> int:
        return int(draw.textlength(s, font=f))

    def put(x: int, y: int, s: str, f, colour) -> int:
        draw.text((x, y), s, font=f, fill=colour)
        return x + text_w(s, f)

    top = bar[1]
    mid = (top + bar[3]) // 2

    def label_cell(x: int, label: str, value: str, *, chevron: bool = False,
                   value_colour=None) -> int:
        """A captioned field: small-caps key above, mono value below."""
        ly = mid - 13 * SCALE
        vy = mid - 1 * SCALE
        put(x, ly, label, f_label, faint)
        end = put(x, vy, value, f_value, value_colour or text)
        if chevron:
            cx = end + 7 * SCALE
            cy = vy + 7 * SCALE
            draw.polygon([(cx, cy), (cx + 7 * SCALE, cy), (cx + 3 * SCALE, cy + 4 * SCALE)],
                         fill=faint)
            end = cx + 7 * SCALE
        return end

    def divider(x: int) -> int:
        draw.line([(x, top + 7 * SCALE), (x, bar[3] - 7 * SCALE)], fill=line, width=SCALE)
        return x

    def key_value(x: int, label: str, value: str, value_colour=None) -> int:
        """A measurement: small-caps key above a right-aligned mono value."""
        w = max(text_w(label, f_key), text_w(value, f_stat))
        put(x + (w - text_w(label, f_key)) // 2, mid - 13 * SCALE, label, f_key, faint)
        put(x + (w - text_w(value, f_stat)) // 2, mid - 1 * SCALE, value, f_stat,
            value_colour or text)
        return x + w

    # ---- layout, mirroring the CSS order and paddings ------------------------------------------
    x = bar[0] + 12 * SCALE
    x = label_cell(x, "MODE", "auto edit", chevron=True) + 12 * SCALE
    x = divider(x) + 12 * SCALE
    x = label_cell(x, "VENDOR", "netra", chevron=True) + 8 * SCALE
    # the dashed "+" affordance
    box = 19 * SCALE
    by = mid - box // 2 + 4 * SCALE
    draw.rounded_rectangle([x, by, x + box, by + box], radius=6 * SCALE, outline=line, width=SCALE)
    put(x + 6 * SCALE, by + 1 * SCALE, "+", font(UI, 12), faint)
    x += box + 12 * SCALE
    x = divider(x) + 12 * SCALE
    x = label_cell(x, "MODEL", "deepseek/deepseek-v4.1-flash", chevron=True) + 12 * SCALE

    # ---- the stat cluster, right-aligned ------------------------------------------------------
    stats = [
        ("STEPS", "1540", text),
        ("TOOK", "41s", text),
        ("RATE", "287 tok/s", text),
        ("REQUESTS", "15", text),
        ("TOKENS", "640.0M", text),
        ("CACHE HIT", "99.8%", signal),
    ]
    widths = [max(text_w(k, f_key), text_w(v, f_stat)) for k, v, _ in stats]
    total = sum(widths) + 11 * SCALE * 2 * len(stats)

    sx = bar[2] - 12 * SCALE - total
    draw.line([(sx - 12 * SCALE, top + 7 * SCALE), (sx - 12 * SCALE, bar[3] - 7 * SCALE)],
              fill=line, width=SCALE)
    sx += 0
    for index, ((label, value, colour), w) in enumerate(zip(stats, widths)):
        if index:
            draw.line([(sx, top + 7 * SCALE), (sx, bar[3] - 7 * SCALE)], fill=line, width=SCALE)
        sx = key_value(sx + 11 * SCALE, label, value, colour)
        sx += 11 * SCALE

    # ---- a second variant showing the low-cache warning tone ----------------------------------
    image = image.resize((WIDTH, height), Image.LANCZOS)
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / "status-bar-preview.png"
    image.save(path, "PNG", optimize=True)
    print(f"wrote {path} ({path.stat().st_size:,} bytes)")

    # A second row: the same bar with a low cache rate, so both tones can be compared.
    image2 = Image.new("RGB", (WIDTH * SCALE, height * SCALE), bg)
    draw2 = ImageDraw.Draw(image2)
    draw2.rounded_rectangle(bar, radius=11 * SCALE, fill=panel, outline=line, width=SCALE)
    x = bar[0] + 12 * SCALE
    def put2(x, y, s, f, c):
        draw2.text((x, y), s, font=f, fill=c)
        return x + int(draw2.textlength(s, font=f))
    x = put2(x, mid - 13 * SCALE, "MODE", f_label, faint)
    x = put2(x, mid - 1 * SCALE, "auto edit", f_value, text) + 12 * SCALE
    draw2.line([(x, top + 7 * SCALE), (x, bar[3] - 7 * SCALE)], fill=line, width=SCALE)
    x += 12 * SCALE
    x = put2(x, mid - 13 * SCALE, "VENDOR", f_label, faint)
    x = put2(x, mid - 1 * SCALE, "netra", f_value, text) + 12 * SCALE
    draw2.line([(x, top + 7 * SCALE), (x, bar[3] - 7 * SCALE)], fill=line, width=SCALE)
    x += 12 * SCALE
    x = put2(x, mid - 13 * SCALE, "MODEL", f_label, faint)
    put2(x, mid - 1 * SCALE, "deepseek/deepseek-v4.1-flash", f_value, text)

    stats2 = [("STEPS", "3", text), ("TOOK", "12s", text),
              ("REQUESTS", "4", text), ("TOKENS", "200k", text), ("CACHE HIT", "12%", amber)]
    widths2 = [max(text_w(k, f_key), text_w(v, f_stat)) for k, v, _ in stats2]
    total2 = sum(widths2) + 11 * SCALE * 2 * len(stats2)
    sx = bar[2] - 12 * SCALE - total2
    draw2.line([(sx - 12 * SCALE, top + 7 * SCALE), (sx - 12 * SCALE, bar[3] - 7 * SCALE)],
               fill=line, width=SCALE)
    for index, ((label, value, colour), w) in enumerate(zip(stats2, widths2)):
        if index:
            draw2.line([(sx, top + 7 * SCALE), (sx, bar[3] - 7 * SCALE)], fill=line, width=SCALE)
        lw, vw = text_w(label, f_key), text_w(value, f_stat)
        w = max(lw, vw)
        draw2.text((sx + 11 * SCALE + (w - lw) // 2, mid - 13 * SCALE), label, font=f_key, fill=faint)
        draw2.text((sx + 11 * SCALE + (w - vw) // 2, mid - 1 * SCALE), value, font=f_stat, fill=colour)
        sx += 11 * SCALE + w + 11 * SCALE

    both = Image.new("RGB", (WIDTH, height * 2 + 8), bg)
    both.paste(image, (0, 0))
    both.paste(image2.resize((WIDTH, height), Image.LANCZOS), (0, height + 8))
    path2 = OUT / "status-bar-preview-both.png"
    both.save(path2, "PNG", optimize=True)
    print(f"wrote {path2} ({path2.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
