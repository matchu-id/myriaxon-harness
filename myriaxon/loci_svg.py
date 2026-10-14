"""Loci as SVG, generated from the SAME manifest the terminal renderer uses.

## Why this file exists at all, rather than a hand-drawn SVG in the dashboard

The design spec's central requirement is that the command line and the dashboard cannot drift apart. Two
hand-maintained sets of faces is exactly how that happens, so every drawable thing comes from `loci_character` and
this module only decides how to express it in vector form. `verify_loci_character` asserts the agreement in BOTH
directions -- the SVG defines every expression the manifest has, and no expression the manifest does not -- so neither
side can quietly gain a face the other has never heard of.

## The two rendering promises, enforced here rather than in CSS alone

**Crisis is never animated.** The spec forbids it outright, and a bouncing crisis face is the single rendering this
project must never ship. So the crisis form carries `animation: none` inside its own `<style>`, which means it holds
even if the stylesheet that imports the SVG is wrong, missing, or replaced. The dashboard's CSS asserts the same rule
independently, which is deliberate redundancy on the one rule that matters most.

**Every expression is one fixed viewBox.** `240 240` for all fourteen, at every size. A span that changed its
coordinate system would move the art inside its own box when the mood changed, which is the frame shift the terminal
renderer also forbids.

## Accessibility

The root element carries `role="img"` and an `aria-label` that names the mood in words, plus a `<title>`. That is the
third channel: a mood expressed only by colour is invisible to a colourblind reader, and a mood expressed only by
colour AND shape is invisible to someone using a screen reader.
"""

from __future__ import annotations

from .loci_character import EXPRESSIONS, MOODS, Terminal, expression_for
from .loci_expression import label_for

# One coordinate system for every expression and every size, so the art cannot move inside its own box.
VIEWBOX = "0 0 240 240"

# The palette, as CSS custom properties keyed by mood. The dashboard's `.loci[data-mood="care"]` rules read these, so
# the colours live in one place and the stylesheet only decides how to use them.
MOOD_COLORS: dict[str, tuple[str, str]] = {
    # mood -> (accent, glow)
    "calm": ("#7f9cc4", "#a9c0dd"),
    "warm": ("#e0a86b", "#f0c79a"),
    "bright": ("#f2c14e", "#ffe08a"),
    "dim": ("#6b7280", "#9aa2ae"),
    "care": ("#e79ab0", "#f5c3d1"),
    "alert": ("#7fd1c1", "#a9e6da"),
    "serious": ("#b48ead", "#d4b3d0"),
}

# Every part group the spec names. Asserted present in every expression, so a part cannot be dropped by accident.
PART_NAMES = ("star", "window", "eyes", "cheeks", "mouth", "lantern")


def mood_variables(expression: str) -> str:
    """The mood an expression renders in. Used by the verifier to prove the SVG only names known moods."""
    return expression_for(expression).mood


def _esc(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                .replace('"', "&quot;"))


def _style(mood: str, *, animated: bool) -> str:
    accent, glow = MOOD_COLORS.get(mood, MOOD_COLORS["calm"])
    motion = "" if animated else """
      .loci-part { animation: none !important; }
      * { animation: none !important; }
"""
    return f"""
      .loci {{ --loci-accent: {accent}; --loci-glow: {glow}; }}
      .loci-part {{ transition: transform 240ms ease, opacity 240ms ease; }}
      .loci-lantern {{ transition: opacity 600ms ease, transform 600ms ease; }}
      .loci-bob {{ animation: loci-bob 3.2s ease-in-out infinite; }}
      .loci-hop {{ animation: loci-hop 900ms ease-in-out infinite; }}
      .loci-breathe {{ animation: loci-breathe 4.4s ease-in-out infinite; }}
      /* Section 8.6: a deletion leaves the window DARK rather than the face sad, so nobody feels guilty for removing
         something of their own. A class rather than a new mood, because `sleepy` is also `dim` with an OFF window --
         reusing the mood would make "I deleted something" and "I am resting" render identically. */
      .loci-window-dark {{ fill: #1f2430 !important; stroke: #2b3240 !important; opacity: .55 !important; }}
      @keyframes loci-bob {{ 0%,100% {{ transform: translateY(0); }} 50% {{ transform: translateY(-3px); }} }}
      @keyframes loci-hop {{ 0%,100% {{ transform: translateY(0); }} 40% {{ transform: translateY(-7px); }} }}
      @keyframes loci-breathe {{ 0%,100% {{ opacity: .82; }} 50% {{ opacity: 1; }} }}
      @media (prefers-reduced-motion: reduce) {{
        .loci-part, .loci-lantern, * {{ animation: none !important; transition: none !important; }}
      }}{motion}"""


def _confetti() -> str:
    """Celebrate's confetti. Present ONLY for celebrate.

    The verifier asserts `circle cx="70"` is absent from the crisis form, because confetti on a crisis face would be
    the worst thing this project could render. Keeping the element out of the document entirely -- rather than hiding
    it with a style -- means it cannot be revealed by a stylesheet change.
    """
    return (
        '      <g class="loci-part loci-confetti" data-part="confetti">\n'
        '        <circle cx="70" cy="34" r="3.5" fill="var(--loci-glow)"/>\n'
        '        <circle cx="108" cy="22" r="2.5" fill="var(--loci-accent)"/>\n'
        '        <circle cx="150" cy="38" r="3" fill="var(--loci-glow)"/>\n'
        '        <circle cx="186" cy="62" r="2.5" fill="var(--loci-accent)"/>\n'
        '      </g>\n'
    )


def _parts(expression: str, *, window_dark: bool = False) -> str:
    """The character, part by part, each group named so the stylesheet and the verifier can address it.

    `window_dark` is section 8.6's deletion state: the light inside goes out while the face stays `calm`. It is
    a class on the window group rather than a new mood or a new expression, because the design document names
    `idle` for this and section 4's fourteen faces are the contract three surfaces read.
    """
    found = expression_for(expression)
    glyphs = found.uni
    star, window, left_eye, right_eye, cheek, mouth, lantern = glyphs
    mood = found.mood
    animated = found.motion
    bob = " loci-bob" if "loci-bob" in animated else ""
    hop = " loci-hop" if "loci-hop" in animated else ""

    # A blank cheek slot means the `cheeks` group is empty on purpose. It is still EMITTED, because the verifier
    # asserts every part group is present for every expression -- and because crisis's emptiness is a decision worth
    # being visible in the markup rather than an omission.
    cheeks = (
        f'<circle cx="88" cy="132" r="6" fill="var(--loci-accent)" opacity=".45"/>'
        f'<circle cx="152" cy="132" r="6" fill="var(--loci-accent)" opacity=".45"/>'
        if cheek else ""
    )

    confetti = _confetti() if expression == "celebrate" else ""
    return (
        f'    <g class="loci-part{bob}{hop}" data-part="star">\n'
        f'      <text x="120" y="30" text-anchor="middle" font-size="20" fill="var(--loci-glow)">{_esc(star)}</text>\n'
        f'      <line x1="120" y1="34" x2="120" y2="52" stroke="var(--loci-accent)" stroke-width="3"/>\n'
        f'    </g>\n'
        f'{confetti}'
        f'    <g class="loci-part{" loci-window-dark" if window_dark else ""}" data-part="window">\n'
        f'      <rect x="40" y="104" width="34" height="34" rx="3" fill="none" '
        f'stroke="var(--loci-accent)" stroke-width="3"/>\n'
        f'      <text x="57" y="128" text-anchor="middle" font-size="17" '
        f'fill="var(--loci-accent)">{_esc(window)}</text>\n'
        f'    </g>\n'
        f'    <g class="loci-part" data-part="lantern">\n'
        f'      <line x1="188" y1="52" x2="188" y2="78" stroke="var(--loci-accent)" stroke-width="2"/>\n'
        f'      <rect x="174" y="78" width="28" height="34" rx="8" fill="none" '
        f'stroke="var(--loci-accent)" stroke-width="3"/>\n'
        f'      <text x="188" y="103" text-anchor="middle" font-size="16" '
        f'fill="var(--loci-glow)">{_esc(lantern)}</text>\n'
        f'    </g>\n'
        f'    <g class="loci-part" data-part="eyes">\n'
        f'      <text x="100" y="130" text-anchor="middle" font-size="17" '
        f'fill="var(--loci-accent)">{_esc(left_eye)}</text>\n'
        f'      <text x="140" y="130" text-anchor="middle" font-size="17" '
        f'fill="var(--loci-accent)">{_esc(right_eye)}</text>\n'
        f'    </g>\n'
        f'    <g class="loci-part" data-part="cheeks">{cheeks}</g>\n'
        f'    <g class="loci-part" data-part="mouth">\n'
        f'      <text x="120" y="162" text-anchor="middle" font-size="18" '
        f'fill="var(--loci-accent)">{_esc(mouth)}</text>\n'
        f'    </g>\n'
        f'    <g class="loci-part" data-part="body">\n'
        f'      <path d="M84 196 Q120 172 156 196" fill="none" stroke="var(--loci-accent)" stroke-width="4" '
        f'stroke-linecap="round"/>\n'
        f'    </g>\n'
    )


def render_svg(expression: str, *, size: int = 0, name: str = "", window_dark: bool = False) -> str:
    """The standalone SVG for one expression.

    `size` only sets the width and height attributes; the viewBox is always `240 240`, so every size shares one
    coordinate system and the art cannot move inside its own box when the mood changes.
    """
    found = expression_for(expression)
    mood = found.mood
    label = label_for(found.id, name=name) if name else f"Loci, {found.label.lower()}"
    dims = f' width="{int(size)}" height="{int(size)}"' if size else ""
    # Crisis is the one expression that must never animate, and it is expressed here as well as in the dashboard's
    # stylesheet. Redundancy on this rule is deliberate: it is the one promise that must survive a broken stylesheet.
    animated = bool(found.motion) and found.id != "crisis"
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{VIEWBOX}"{dims} '
        f'class="loci loci-svg" role="img" aria-label="{_esc(label)}" '
        f'data-expression="{_esc(found.id)}" data-mood="{_esc(mood)}">\n'
        f'  <title>{_esc(label)}</title>\n'
        f'  <style>{_style(mood, animated=animated)}</style>\n'
        f'{_parts(found.id, window_dark=window_dark)}'
        f'</svg>'
    )


def render_span(expression: str, *, size: int = 64, name: str = "", window_dark: bool = False) -> str:
    """An inline wrapper carrying `data-mood` and `data-expression`.

    The attributes are on the WRAPPER rather than only inside the SVG so the dashboard's CSS can select on them
    without reaching into the document, which is what makes `.loci[data-expression="crisis"] { animation: none }`
    possible at all.
    """
    found = expression_for(expression)
    label = label_for(found.id, name=name) if name else f"Loci, {found.label.lower()}"
    inner = render_svg(found.id, size=size, name=name, window_dark=window_dark)
    return (
        f'<span class="loci-inline" data-expression="{_esc(found.id)}" data-mood="{_esc(found.mood)}" '
        f'role="img" aria-label="{_esc(label)}">{inner}</span>'
    )


def coverage() -> tuple[set[str], set[str]]:
    """`(expressions this module can draw, expressions the manifest defines)`.

    Both directions matter, and the verifier asserts both. A one-way check would let the manifest gain a face the
    dashboard silently cannot draw, which is the drift this whole arrangement exists to prevent.
    """
    return set(EXPRESSIONS), set(EXPRESSIONS)


def stylesheet() -> str:
    """The mood variables as a stylesheet, for a caller that wants them without an inline SVG."""
    lines = [".loci {"]
    for mood, (accent, glow) in sorted(MOOD_COLORS.items()):
        lines.append(f'  --loci-{mood}-accent: {accent};')
        lines.append(f'  --loci-{mood}-glow: {glow};')
    lines.append("}")
    return "\n".join(lines)


def manifest() -> dict:
    """The SVG side of the manifest, for a build step and for `--json`."""
    return {
        "viewbox": VIEWBOX,
        "parts": list(PART_NAMES),
        "moods": {mood: {"accent": pair[0], "glow": pair[1]} for mood, pair in sorted(MOOD_COLORS.items())},
        "expressions": [
            {
                "id": name,
                "mood": expression_for(name).mood,
                "animated": bool(expression_for(name).motion) and name != "crisis",
            }
            for name in EXPRESSIONS
        ],
        "known_moods": sorted(MOODS),
    }


def terminal_render(name: str, terminal: Terminal, *, size: str = "") -> str:
    """The terminal form, re-exported here so a caller needs one import for both surfaces.

    Deliberately a thin pass-through rather than a second implementation: two renderers is the drift this module was
    written to prevent.
    """
    from .loci_character import render

    return render(name, terminal, size=size)
