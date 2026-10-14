"""Regenerate the distributable brand assets from `myriaxon/brand/__init__.py`.

The SVG files in `brand/` and the palette sheet are *outputs*, not sources. They exist so a
designer or a web page can use the identity without importing Python, and they are generated
from the same tokens the product renders with -- so the two can never disagree, which is the
failure mode that made the previous logo live in four places at once.

Run from the repository root::

    python tools/export_brand_assets.py
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from myriaxon import brand  # noqa: E402

# Kept for the few callers that referenced it; `write` now fans out to both destinations.
OUT = REPO / "brand"


def write(name: str, content: str) -> None:
    """Write one asset to every destination that must carry it.

    Two copies exist for two audiences and both are load-bearing:
      * ``brand/`` at the repository root is the distributable set -- a designer, a deck, a
        partner integrating the identity, none of whom should have to import Python.
      * ``myriaxon/brand/assets/`` is package data, so an installed wheel can serve the mark
        without the repository.

    They are written together, from one source, in one command. Writing only one -- which is
    what happened when the repository layout changed and the root directory was left behind --
    silently loses the other, and a missing asset is discovered by a user rather than by a test.
    """
    for directory in (REPO / "brand", REPO / "myriaxon" / "brand" / "assets"):
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")
        print(f"  {path.relative_to(REPO)}  ({len(content)} bytes)")


def palette_sheet() -> str:
    """A human-readable palette reference, generated so it cannot go stale."""
    rows = []
    for name, value in brand.dark_tokens().items():
        light = brand.light_tokens().get(name, "—")
        rows.append(
            f"| `{name}` | `{value}` | `{light}` | "
            f'<span style="display:inline-block;width:44px;height:14px;'
            f'background:{value};border:1px solid #8888"></span> |'
        )
    table = "\n".join(rows)
    return f"""# Myriaxon palette

Generated from `myriaxon/brand/__init__.py` by `tools/export_brand_assets.py` — do not edit
by hand. That module is the single source of truth: the CLI theme, the syntax highlighter,
the prompt_toolkit REPL style and the dashboard's CSS custom properties are all derived from
or asserted against the values below.

## Core palette

| Token | Dark | Light | Swatch |
|---|---|---|---|
{table}

## Meaning

Colour is bound to meaning, never used decoratively.

| Token | Carries |
|---|---|
| `--signal` | The live thing: the running state, the active nav item, the core of the mark. The only brand hue. |
| `--ink`, `--ink-2`, `--ink-3` | The three surfaces. Every panel is exactly one of them, which is what keeps a dense console from becoming a patchwork. |
| `--line`, `--line-2` | Dividers; `--line-2` marks a border as interactive. |
| `--text`, `--text-2`, `--text-3` | Primary, secondary and tertiary text. |
| `--blue` | Informational: routing, providers, file paths. |
| `--amber` | Needs attention: pending confirmation, warnings, scheduled work, literals in code. |
| `--violet` | Memory and skills. |
| `--red` | Failure and denial. |

## Type

| Role | Stack | Tracking |
|---|---|---|
| Display / wordmark | {brand.FONT_DISPLAY} | `{brand.WORDMARK_TRACKING}` |
| Interface | {brand.FONT_UI} | — |
| Monospace / code | {brand.FONT_MONO} | — |

The dashboard makes **no network requests**, so these are font stacks rather than fetched
webfonts: when the brand faces are installed the rendering is exact, and when they are not the
page still renders in the closest available system face. That is a deliberate security and
air-gap decision — a harness that calls a font CDN on first paint leaks that it is running and
cannot be used on the isolated networks this tool is built for.

## Mark

Eight rays converging on one open ring and a single signal-green core: the myriad paths and the
one mind. Four tapered diamonds on the cardinal axes, four needle spikes on the diagonals.

- `myriaxon-mark.svg` — the lockup mark, `currentColor` rays, for use on any surface.
- `myriaxon-app-icon.svg` — the square app icon / favicon; heavier rays, four points, because at
  16 px the lockup's hairlines disappear.

Tagline: **{brand.TAGLINE}**
"""


def lockup_svg() -> str:
    """The full horizontal lockup: mark, wordmark, tagline."""
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="720" height="180" viewBox="0 0 720 180" role="img" aria-label="Myriaxon">
  <title>Myriaxon</title>
  <rect width="720" height="180" fill="{brand.INK}"/>
  <g transform="translate(40 40) scale(0.5)">
    <g fill="{brand.TEXT}">
      <polygon points="100,6 104.5,68 100,80 95.5,68"/>
      <polygon points="100,6 104.5,68 100,80 95.5,68" transform="rotate(90 100 100)"/>
      <polygon points="100,6 104.5,68 100,80 95.5,68" transform="rotate(180 100 100)"/>
      <polygon points="100,6 104.5,68 100,80 95.5,68" transform="rotate(270 100 100)"/>
      <polygon points="100,36 103,70 100,80 97,70" transform="rotate(45 100 100)"/>
      <polygon points="100,36 103,70 100,80 97,70" transform="rotate(135 100 100)"/>
      <polygon points="100,36 103,70 100,80 97,70" transform="rotate(225 100 100)"/>
      <polygon points="100,36 103,70 100,80 97,70" transform="rotate(315 100 100)"/>
      <circle cx="100" cy="100" r="12"/>
    </g>
    <circle cx="100" cy="100" r="8.5" fill="{brand.INK}"/>
    <circle cx="100" cy="100" r="4" fill="{brand.SIGNAL}"/>
  </g>
  <text x="150" y="100" fill="{brand.TEXT}" font-family="Unbounded, 'Arial Black', sans-serif"
        font-size="58" font-weight="600" letter-spacing="{brand.WORDMARK_TRACKING}">MYRIAXON</text>
  <text x="152" y="132" fill="{brand.TEXT_3}" font-family="'JetBrains Mono', monospace"
        font-size="15" letter-spacing="0.08em">{brand.TAGLINE.upper()}</text>
</svg>
"""


def main() -> int:
    print("exporting brand assets")
    write("myriaxon-mark.svg", brand.mark_svg_standalone(size=200))
    write("myriaxon-app-icon.svg", brand.app_icon_svg(size=512))
    write("myriaxon-lockup.svg", lockup_svg())
    write("PALETTE.md", palette_sheet())
    print("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
