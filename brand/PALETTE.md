# Myriaxon palette

Generated from `myriaxon/brand/__init__.py` by `tools/export_brand_assets.py` — do not edit
by hand. That module is the single source of truth: the CLI theme, the syntax highlighter,
the prompt_toolkit REPL style and the dashboard's CSS custom properties are all derived from
or asserted against the values below.

## Core palette

| Token | Dark | Light | Swatch |
|---|---|---|---|
| `--ink` | `#0A0C11` | `#F4F5F8` | <span style="display:inline-block;width:44px;height:14px;background:#0A0C11;border:1px solid #8888"></span> |
| `--ink-2` | `#10131A` | `#FFFFFF` | <span style="display:inline-block;width:44px;height:14px;background:#10131A;border:1px solid #8888"></span> |
| `--ink-3` | `#171B24` | `#EEF0F4` | <span style="display:inline-block;width:44px;height:14px;background:#171B24;border:1px solid #8888"></span> |
| `--line` | `#232835` | `#DDE0E7` | <span style="display:inline-block;width:44px;height:14px;background:#232835;border:1px solid #8888"></span> |
| `--line-2` | `#2E3444` | `#C9CED9` | <span style="display:inline-block;width:44px;height:14px;background:#2E3444;border:1px solid #8888"></span> |
| `--text` | `#EDEFF5` | `#0A0C11` | <span style="display:inline-block;width:44px;height:14px;background:#EDEFF5;border:1px solid #8888"></span> |
| `--text-2` | `#A3ABBD` | `#3F4658` | <span style="display:inline-block;width:44px;height:14px;background:#A3ABBD;border:1px solid #8888"></span> |
| `--text-3` | `#7C8599` | `#5A6275` | <span style="display:inline-block;width:44px;height:14px;background:#7C8599;border:1px solid #8888"></span> |
| `--signal` | `#7CF5C8` | `#0B8F63` | <span style="display:inline-block;width:44px;height:14px;background:#7CF5C8;border:1px solid #8888"></span> |
| `--signal-ink` | `#062A1E` | `#FFFFFF` | <span style="display:inline-block;width:44px;height:14px;background:#062A1E;border:1px solid #8888"></span> |
| `--blue` | `#8FB4FF` | `#2F5FD0` | <span style="display:inline-block;width:44px;height:14px;background:#8FB4FF;border:1px solid #8888"></span> |
| `--amber` | `#FFB86B` | `#B25E00` | <span style="display:inline-block;width:44px;height:14px;background:#FFB86B;border:1px solid #8888"></span> |
| `--violet` | `#C9A2FF` | `#7A45D6` | <span style="display:inline-block;width:44px;height:14px;background:#C9A2FF;border:1px solid #8888"></span> |
| `--red` | `#FF8A8A` | `#C62828` | <span style="display:inline-block;width:44px;height:14px;background:#FF8A8A;border:1px solid #8888"></span> |

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
| Display / wordmark | "Unbounded", "Arial Black", "Segoe UI Variable Display", system-ui, sans-serif | `0.14em` |
| Interface | "Instrument Sans", "Inter", "Geist", "Segoe UI Variable Text", "Segoe UI", -apple-system, BlinkMacSystemFont, "Helvetica Neue", Arial, sans-serif | — |
| Monospace / code | "JetBrains Mono", "Geist Mono", "Cascadia Mono", ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace | — |

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

Tagline: **Ten thousand paths. One mind.**
