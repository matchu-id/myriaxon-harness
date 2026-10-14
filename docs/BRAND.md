# Myriaxon — brand and visual identity

**Tagline:** Ten thousand paths. One mind.
**Descriptor:** Advanced agentic harness.
**Mark:** an eight-ray astral compass converging on one signal-green core.

This document describes the identity as it is implemented. Every value below is quoted from
`myriaxon/brand/__init__.py`, which is the single source of truth for the palette, the type stacks,
the mark's geometry, the terminal glyphs and the display copy. Nothing in this document is a
separate truth: if this file and that module disagree, the module wins and this file is wrong.

---

## 1. The mark

Eight rays drawn from a shared origin on a 200×200 canvas, over a ringed core whose centre dot is the
only signal-green element. Many paths, one mind.

| Property | Value | Why |
|---|---|---|
| Canvas | `200 × 200`, `viewBox="0 0 200 200"` | one geometry reused at every size |
| Cardinal ray | `100,6 104.5,68 100,80 95.5,68` | reaches almost to the canvas edge |
| Diagonal ray | `100,36 103,70 100,80 97,70` | deliberately **stops short** of the cardinals |
| Cardinal angles | `0, 90, 180, 270` | up, right, down, left |
| Diagonal angles | `45, 135, 225, 315` | the four corners |
| Outer core ring | `r = 12.0`, filled with the surrounding ink | opens the centre to whatever surface it sits on |
| Inner disc | `r = 8.5` | the ring itself |
| Signal core | `r = 4.0`, filled with `--signal` | the one mind |

The rays are drawn from the origin as eight individual polygons rather than as four rotations of one
path, precisely so the cardinals and the diagonals can differ in length. Equal-length rays read as a
generic eight-point star; the short diagonals are what make it a compass.

### The three renderings

| Function | Use | Colour handling |
|---|---|---|
| `mark_svg(size=28, label="Myriaxon")` | inline in HTML | rays take `currentColor`; the inner disc is `var(--ink-2)` and the core `var(--signal)` |
| `mark_svg_standalone(size=28, signal=SIGNAL)` | `.svg` assets, `<img>`, favicons | bakes in `INK` / `INK_2` / `SIGNAL` — no stylesheet required |
| `app_icon_svg(size=512)` | app icon / favicon | the mark reversed out of a rounded ink tile (`rx="46"`), drawn **heavier** |
| `app_icon_data_uri()` | favicon link with no extra request | the icon as a `data:image/svg+xml,…` URI |

Two decisions worth keeping:

* **The inner disc is filled with the surface, not a colour.** Because `mark_svg()` fills it with
  `var(--ink-2)` rather than a literal hex, one markup string serves a panel, the page and a tooltip.
  This is why the earlier four-circle logogram's separate dark and light colour sets are gone.
* **The icon is a heavier drawing, not a scaled-down lockup.** At 16 px, eight hairlines and a 4 px
  dot are a smudge, so `app_icon_svg()` uses thick tapered rays and a solid core instead.
* **The favicon is a `data:` URI.** A separate `/favicon.ico` route means a second request, a second
  route to keep unauthenticated, and a 404 in the log whenever it is missing. A data URI cannot 404.

---

## 2. Palette

### Dark ground

The near-black is blue-tinted rather than neutral: against pure black a blue-black reads as depth,
while `#000` reads as a hole. The three ink steps are the only surfaces in the product — every panel
is one of them, which is what stops a dense console from turning into a patchwork.

| Token | Hex | Where it is used |
|---|---|---|
| `INK` | `#0A0C11` | the page |
| `INK_2` | `#10131A` | raised: panels, cards, dialogs, the composer |
| `INK_3` | `#171B24` | raised again: nav selection, inputs, hover targets, tags |
| `LINE` | `#232835` | every border and divider |
| `LINE_2` | `#2E3444` | borders that must read as interactive: inputs, buttons, chips |
| `TEXT` | `#EDEFF5` | primary text on ink |
| `TEXT_2` | `#A3ABBD` | secondary text on ink |
| `TEXT_3` | `#7C8599` | tertiary: captions, timestamps, units |
| `SIGNAL` | `#7CF5C8` | the one colour that means Myriaxon |
| `SIGNAL_INK` | `#062A1E` | the readable foreground **on** a signal fill |

`SIGNAL_INK` is not a darker signal — it is the text colour that sits on a signal fill, which is why
it inverts wholesale between themes.

### Light ground

The light theme is not the dark theme inverted; every hue is re-picked for contrast on a pale ground.
Signal green on white fails contrast outright, so the light accent is a deep teal-green from the same
family, and the supporting hues darken to match.

| Token | Hex | Where it is used |
|---|---|---|
| `INK_LIGHT` | `#F4F5F8` | the page |
| `INK_2_LIGHT` | `#FFFFFF` | panels, cards, dialogs, the composer |
| `INK_3_LIGHT` | `#EEF0F4` | nav selection, inputs, hover targets, tags |
| `LINE_LIGHT` | `#DDE0E7` | borders and dividers |
| `LINE_2_LIGHT` | `#C9CED9` | interactive borders |
| `TEXT_LIGHT` | `#0A0C11` | primary text |
| `TEXT_2_LIGHT` | `#3F4658` | secondary text |
| `TEXT_3_LIGHT` | `#5A6275` | tertiary text |
| `SIGNAL_LIGHT` | `#0B8F63` | the accent on a pale ground |
| `SIGNAL_INK_LIGHT` | `#FFFFFF` | text on a light signal fill |

### Supporting hues

Each is bound to one meaning, so a colour is never decorative.

| Token | Dark | Light | Meaning |
|---|---|---|---|
| `BLUE` | `#8FB4FF` | `#2F5FD0` | informational / routing |
| `AMBER` | `#FFB86B` | `#B25E00` | needs attention / scheduled |
| `VIOLET` | `#C9A2FF` | `#7A45D6` | memory and skills |
| `RED` | `#FF8A8A` | `#C62828` | failure / denied |

**The rule for `SIGNAL`:** used sparingly, and always to mark the live thing — the running state, the
active nav item, the core of the mark. A signal colour that appears everywhere stops signalling.

### In the terminal

`myriaxon/theme.py` maps the palette onto Rich styles. The mapping is meaning-preserving:

| Element | Colour |
|---|---|
| tool name, prompt, success, diff addition | `SIGNAL` |
| pending tool / confirmation question / warning | `AMBER` |
| informational, paths | `BLUE` |
| memory | `VIOLET` |
| error ground | `RED` (bold `TEXT` on a red ground) |
| diff removal, context, dim text | `TEXT_3` |
| headings, keys, user text | `TEXT` |
| results, values, secondary text | `TEXT_2` |

Syntax highlighting (`MyriaxonPygmentsStyle`) is restricted to the same palette, and colour carries
*grammar*, not decoration: keywords take the signal, names take the informational blue, strings take
the amber that already means "a value worth noticing", numbers and decorators take violet, deleted
lines take red. Its code background is `#08090B` — one step deeper than `INK`, and deliberately not
one of the three surface inks, so a code block reads as a distinct object rather than a fourth panel.

### Legacy CSS aliases

`LEGACY_ALIASES` maps the pre-rebrand variable names (`--bg`, `--panel`, `--panel-2`, `--border`,
`--muted`, `--faint`, `--accent`, `--ok`, `--warn`, `--err`, `--code-bg`, `--hover`, `--user`,
`--shadow`, `--mono`, `--ui`, `--display`, …) onto the new tokens, in the same `:root` block. They
exist so that a stray old name can never silently resolve to nothing — which is exactly the class of
bug (an undefined `--mono`) that shipped in the previous design.

---

## 3. Type

| Role | Face | Declared stack |
|---|---|---|
| Display | **Unbounded** | `"Unbounded", "Arial Black", "Segoe UI Variable Display", system-ui, sans-serif` |
| UI | **Instrument Sans** | `"Instrument Sans", "Inter", "Geist", "Segoe UI Variable Text", "Segoe UI", -apple-system, BlinkMacSystemFont, "Helvetica Neue", Arial, sans-serif` |
| Mono | **JetBrains Mono** | `"JetBrains Mono", "Geist Mono", "Cascadia Mono", ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace` |

Wordmark letter-spacing: `0.14em` (`WORDMARK_TRACKING`). Hero letter-spacing: `0.02em`
(`HERO_TRACKING`). The display face is wide by design; when it is absent and a flat system font is
substituted, the tracking is what still carries the brand.

### The offline-font decision

The faces are declared as **stacks, never fetched**. The dashboard makes zero network requests, and a
check in the suite asserts the page is self-contained; an agent harness that phones a font CDN on
first paint leaks that it is running, and cannot be used on the isolated networks this tool is built
for. So:

* if the brand faces are installed on the machine, rendering is exact;
* if they are not, the page and the terminal still render, in a stack chosen to sit as close as
  possible;
* there is no `@font-face` rule, no `<link>` to a font host, and no CDN anywhere in the product.

This is the one deliberate compromise in the identity, and it is a security and air-gap decision
rather than an aesthetic one.

> **Accurate as of this checkout:** `myriaxon/brand/__init__.py` refers to a font installer that
> drops the faces into `~/.myriaxon/fonts`. No such installer exists in this repository, so today
> installing Unbounded / Instrument Sans / JetBrains Mono is a manual, machine-level step.

---

## 4. The terminal mark and wordmark

A terminal cannot draw curves, and it cannot draw a diagonal. Both the mark and the wordmark are
therefore derived constructions rather than pictures.

### Mark: braille compass

A braille cell is a 2×4 dot grid — enough to render the compass geometry rather than approximating it
with an asterisk. The compass is defined as a 7×8 grid of lit dots (odd width centres the core on a
single dot column; 4-dot-tall braille folds into two glyph rows with no padding):

```
#.#.#.#      upward ray + both upper diagonals
..#.#..
..###..
..###..
..###..
..###..
..#.#..
#.#.#.#      downward ray + both lower diagonals
```

| Name | Value | Use |
|---|---|---|
| `MARK_GLYPH` / `MARK_GLYPH_LINES` | the braille compass, two glyph rows | the banner |
| `MARK_GLYPH_SMALL` | `✳` | dense contexts: status lines, the web header fallback |

Two implementation rules, both load-bearing:

* **Cells are joined with U+200A (hair space)**, not a normal space, so a terminal font's cell
  metrics cannot collapse the glyphs into one blob.
* **The glyph is derived from the dot table, not hand-encoded.** A hand-encoded braille literal is
  impossible to check without a working renderer — which is exactly how the previous mark ended up
  wrong.

### Wordmark: five-row block letters

`wordmark_rows("MYRIAXON")` returns five rows of block letters, one space between glyphs. Every glyph
carries a two-cell stroke, so the wordmark reads as one weight rather than a mix of bold and hairline.

| Concern | Decision |
|---|---|
| Uprights | half-block bars `▐` (U+2590) / `▌` (U+258C), **not** full blocks `█` (U+2588) |
| Why | a full block fills its cell edge to edge, so two adjacent uprights fuse into one slab and the counters between them disappear at real terminal sizes |
| Diagonals | box-drawing staircase (`▐▌▐▌` / `██`), because a terminal cell cannot hold a true diagonal |
| `O` | `╔══╗` / `║  ║` / `╚══╝` — the only rounded letterform available |

### The banner

`myriaxon/banner.py` composes the header. The two halves are exactly these, placed side by side with
three spaces between them and the mark padded to the wordmark's optical centre:

```
mark (brand.MARK_GLYPH_LINES)      wordmark (brand.wordmark_rows("MYRIAXON"))
⠁⣧⡇⠁                              ▐▌ ▐▌ ▐▌ ▐▌ ████  ████  ██  ▐▌▐▌ ╔══╗ ▐▌ ▐▌
⡀⡟⡇⡀                              ██▄██ ▐▌ ▐▌ ▐▌ ▐▌  ▐▌  ▐▌▐▌  ██  ║  ║ ██ ▐▌
                                    ██ ██  ███  ████   ▐▌  ████  ██  ║  ║ ▐██▌
                                    ▐▌ ▐▌   █   ▐▌▐▌   ▐▌  ▐▌▐▌ ▐▌▐▌ ║  ║ ▐▌ █
                                    ▐▌ ▐▌   █   ▐▌ ▐▌ ████ ▐▌▐▌ ▐▌▐▌ ╚══╝ ▐▌ ▐▌
```

Below that, inside the same panel: the identity line (`Myriaxon 1.0.0   ·   Advanced agentic
harness`), the connection line (`connected to <base_url>   model <model>`), and the tagline. The
version comes from `constants.VERSION`, so the banner never carries a stale number.

| Decision | Reason |
|---|---|
| mark to the **left** of the wordmark, not above | a terminal window is wide and short; a stacked lockup wastes the scarce dimension |
| mark padded to the wordmark's optical centre | the braille mark is two rows against the wordmark's five, so it is baselined like a logotype and its symbol |
| version stamped in `SIGNAL`, right of the name | the one live element in a static header |
| connection line (`base_url` + `model`) always shown | it is the first thing to check when output looks wrong |
| whole header in a panel whose border is the accent | the header reads as one object instead of three stacked lines |
| below the required width, the wordmark falls back to tracked-out capitals | keeps an 80-column session intact instead of wrapping into garbage |
| `print_banner_compact()` — one line, `✳ Myriaxon <version> · model · endpoint` | the full panel earns its six rows exactly once; a resumed session or a `-p` run only needs what changed |

---

## 5. Copy

| Constant | Value | Where |
|---|---|---|
| `TAGLINE` | `Ten thousand paths. One mind.` | the banner, the dashboard `<meta name="description">` |
| `TAGLINE_COMPACT` | `ten thousand paths · one mind` | tight layouts |
| `DESCRIPTOR` | `Advanced agentic harness` | the banner's identity line |
| `DESCRIPTOR_LONG` | `An advanced agentic harness for terminal and browser.` | long-form copy |

---

## 6. The three CLI themes

`myriaxon/theme.py` defines exactly three Rich themes, selected by `/theme [default|high-contrast|mono]`
(persisted to the user layer of `settings.json`) or per call through `get_console(theme_name)`.

| Key | Name | Character | What changes |
|---|---|---|---|
| `default` | Myriaxon ink | signal green on a near-black ground | the palette above; `banner` is a surface style and `banner.accent` is the mark and version stamp beside it |
| `high-contrast` | High contrast | maximum separation, no reliance on the brand hue | accent becomes bold yellow, primary text bold white, structure carried by weight and case; diff add/remove become bold green/bold red; `dim` is `grey70` |
| `mono` | Mono | no colour at all | structure carried entirely by weight, reverse video and underline, so piped output is still hierarchical; `error` is bold reverse, diff removal is strike |

Why a high-contrast theme exists at all: signal green is a *low-contrast* colour by design — it is
pale relative to its ground, which is what makes it read as a signal rather than a highlight. That is
the wrong property for a user who needs contrast, so the accessible theme drops hue entirely rather
than tinting it.

The mono theme is for terminals, log capture and CI that do not render ANSI. It is not a degraded
theme: every distinction the colour theme makes is still made, by a different channel.

**Spinner:** `SignalSpinner` cycles a braille frame and breathes through the brand's ink steps —
signal green held constant while the trail fades through the neutrals. The previous spinner cycled
three unrelated hues, which read as three different things happening rather than one thing in
progress. `ThreeColorSpinner` remains as an alias so an existing import cannot silently fail.

---

## 7. Distributable assets

The Python module is the source; the files are outputs, generated so they cannot disagree with it.

| Path | What it is |
|---|---|
| `brand/myriaxon-mark.svg` | the lockup mark — `currentColor` rays, for any surface |
| `brand/myriaxon-app-icon.svg` | the square app icon / favicon — heavier rays and a solid core, because at 16 px the lockup's hairlines disappear |
| `brand/myriaxon-lockup.svg` | the horizontal lockup: mark, wordmark, tagline on an ink ground |
| `brand/PALETTE.md` | the palette sheet: every token, both grounds, with swatches and the meaning of each colour |
| `myriaxon/brand/assets/*.svg` | the same SVGs inside the package, shipped in the wheel as package data |
| `tools/export_brand_assets.py` | regenerates the files in `brand/` from the module |

```powershell
python tools/export_brand_assets.py
```

`PALETTE.md` and each SVG carry a header saying they are generated and must not be edited by hand.
Edit the module and re-run the exporter — the failure this prevents is the previous logo living in
four places at once.

---

## 8. Rules for changing the identity

1. **Edit `myriaxon/brand/__init__.py` first, and only.** The CLI banner, the Rich themes, the
   Pygments style, the `prompt_toolkit` REPL style and the dashboard's CSS custom properties are all
   generated from or asserted against these values. A brand change is one edit, not a hunt.
2. **Do not hardcode a hex anywhere else.** `theme.py` and `completion.py` import `brand` for exactly
   this reason: the palette previously lived in three unrelated places, and a change applied to two of
   them produced an inconsistent UI.
3. **The dashboard mirrors the tokens, and the mirror is asserted.** The page is one self-contained
   HTML string with no build step, so its `:root` block repeats the values;
   `scripts/check_ui_markup.py` imports `myriaxon.brand` and checks that the served page carries the
   brand signal, the brand light signal and the ink grounds. A brand edit that lands in one place and
   not the other is the drift that check exists to catch.
4. **Keep the legacy aliases** while any pre-rebrand markup or JavaScript still references the old
   names.
5. **Give every new colour a meaning before using it.** If a colour does not stand for something the
   user must distinguish, it is decoration and belongs in the greys.
6. **Do not add a network-fetched asset** — no font CDN, no icon CDN, no external stylesheet. The
   dashboard's zero-request property is a feature, not an accident.
7. **Regenerate the assets after a token change.** `python tools/export_brand_assets.py` rewrites the
   SVGs and the palette sheet in `brand/`; a token edit that is not exported is a second, silent
   source of truth.
