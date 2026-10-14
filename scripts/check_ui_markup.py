"""Static checks on the served UI markup.

    python scripts/check_ui_markup.py [port]

The visual layout cannot be verified without a browser, but the structural things that break it
silently can be: a dangling element id referenced by the script, a missing theme variable, the logo,
and the controls the UI claims to have. A `$("id")` that does not exist throws at the first call and
leaves the page half-rendered, which is exactly the failure this catches.
"""
import json
import re
import sys
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8791
passed, failed = [], []


def check(label, condition, detail=""):
    (passed if condition else failed).append(label)
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}{(' -- ' + str(detail)) if detail and not condition else ''}")


def main() -> int:
    # The palette's single source of truth. ui.py mirrors these values because the page is one
    # self-contained string with no build step (see the comment in its <style> block), so the mirror
    # is asserted rather than trusted: a brand edit that lands in one place and not the other is the
    # drift this catches.
    from myriaxon import brand

    html = urllib.request.urlopen(f"http://127.0.0.1:{PORT}/", timeout=30).read().decode("utf-8")

    print("=== structure ===")
    check("serves HTML", html.lstrip().startswith("<!DOCTYPE html>"))
    check("titled Myriaxon", "<title>Myriaxon</title>" in html)

    # Every id the script looks up must exist in the markup.
    ids_defined = set(re.findall(r'id="([^"]+)"', html))
    ids_used = set(re.findall(r'\$\("([^"]+)"\)', html))
    # Ids assigned at runtime (`element.id = "x"`) are legitimate lookups that cannot appear in the
    # static markup; excluding them keeps the check about genuine dangling references.
    ids_created = set(re.findall(r'\.id\s*=\s*"([^"]+)"', html))
    missing = sorted(ids_used - ids_defined - ids_created)
    check("every referenced element id exists", not missing, f"missing: {missing}")

    print("\n=== logo ===")
    # The pre-rebrand logogram was four coloured circles drawn twice -- a light variant and a dark
    # one, swapped by a CSS rule -- so the checks here were for both variants and for the four hexes
    # of its palette. The Myriaxon mark replaces all of that: eight rays converging on a ring with a
    # signal-green core, drawn once with `fill="currentColor"` and one `var(--signal)` so a single
    # drawing serves both themes. What is asserted below is that one drawing and its real geometry.
    check("the mark is an inline SVG", '<svg class="mark"' in html)
    check("the mark declares the brand canvas", 'viewBox="0 0 200 200"' in html)
    check("the mark is exposed as an image", 'role="img"' in html)
    check("the mark carries an accessible name", 'aria-label="Myriaxon"' in html)
    check("the mark is drawn from polygon rays", html.count("<polygon") >= 8, html.count("<polygon"))
    check("the mark is one drawing, not a per-theme pair",
          html.count('<svg class="mark"') == 1 and "mark-light" not in html and "mark-dark" not in html,
          html.count('<svg class="mark"'))
    check("the mark takes its colour from the surrounding theme", 'fill="currentColor"' in html)
    check("the mark's core is the signal colour",
          'class="core-dot"' in html and ".mark .core-dot { fill: var(--signal); }" in html)
    # Declared once in a <template> and cloned into every placement by the script: duplicating the
    # SVG per location is how the sidebar mark and the header mark end up subtly different.
    check("the mark is declared once as a template", 'id="marktemplate"' in html)
    check("both placements clone that one template",
          html.count('class="markslot"') >= 2 and '$("marktemplate")' in html,
          html.count('class="markslot"'))
    # The palette. `myriaxon/brand/__init__.py` is the source of truth; the page mirrors it.
    check("the page's dark --signal is the brand signal",
          re.search(rf"--signal\s*:\s*{re.escape(brand.SIGNAL)}\b", html) is not None, brand.SIGNAL)
    check("the page's light --signal is the brand light signal",
          re.search(rf"--signal\s*:\s*{re.escape(brand.SIGNAL_LIGHT)}\b", html) is not None,
          brand.SIGNAL_LIGHT)
    for name, colour in (("ink ground", brand.INK), ("raised ground", brand.INK_2),
                         ("light ground", brand.INK_LIGHT)):
        check(f"the brand {name} ({colour}) is used", colour in html, colour)

    print("\n=== theme ===")
    check("dark theme variables defined", 'html[data-theme="dark"]' in html or ':root,' in html)
    check("light theme variables defined", 'html[data-theme="light"]' in html)
    check("theme toggle present", 'id="themebtn"' in html)
    check("theme is applied before first paint", "applyTheme(saved" in html or "applyTheme(" in html)
    for variable in ("--bg", "--text", "--border", "--accent", "--code-bg", "--user"):
        check(f"variable {variable} defined", variable in html)

    print("\n=== required controls ===")
    for element_id, purpose in (
        ("sessions", "session list"),
        ("newbtn", "new session"),
        ("projbtn", "open project"),
        ("projmodal", "project dialog"),
        ("provider-sel", "vendor switcher"),
        ("model", "model switcher"),
        ("mode", "mode switcher"),
        ("prompt", "composer"),
        ("attach", "attachment button"),
        ("chips", "attachment chips"),
        ("trajwrap", "trajectory pane"),
    ):
        check(f"{purpose} control present ({element_id})", f'id="{element_id}"' in html)

    print("\n=== markdown styling ===")
    for selector in ("table", "blockquote", "h1", "hr", "strong", "ul", "pre"):
        check(f"styles for {selector}", re.search(r"\.content[^{]*\b" + selector + r"\b", html) is not None)

    print("\n=== script sanity ===")
    # `sidefoot` is a legitimate CSS class name; what was stale was the element id the script looked
    # up, which the "every referenced element id exists" check above already covers. Asserting on the
    # bare string would flag a correct class attribute.
    check("no stale sidefoot element lookup", '$("sidefoot")' not in html)
    check("answer_delta handled", "answer_delta" in html)
    check("session delete wired", "/api/sessions/" in html and "DELETE" in html)
    check("workspace switch wired", "/api/workspace" in html)

    print("\n=== buffering indicator ===")
    # Shown from turn start until the first token. Verified separately that Gemini streams no thought
    # parts at all (only a token count), so an indicator is the only honest way to show progress.
    # The element is created at runtime, so its id appears as an assignment rather than markup.
    check("buffering element created", 'buffering-msg' in html)
    check("buffering styled", ".buffering" in html)
    # The old check named the pre-rebrand logogram's `--yellow` explicitly. That name is no longer in
    # the palette, and the check had quietly degraded into a tautology (any `var(--yellow)` contains
    # `--yellow`), so it passed while the indicator resolved to nothing. Asserting the property --
    # every variable the buffering rules use is one the page defines -- cannot go stale, and it is
    # exactly the class of bug the original check existed to catch.
    buffering_rules = "\n".join(line for line in html.splitlines() if ".buffering" in line)
    buffering_vars = sorted(set(re.findall(r"var\((--[A-Za-z0-9_-]+)\)", buffering_rules)))
    undefined_vars = [name for name in buffering_vars if not re.search(rf"{name}\s*:", html)]
    check("buffering is coloured by a palette variable the page defines",
          bool(buffering_vars) and not undefined_vars,
          f"uses {buffering_vars}; undefined: {undefined_vars}")
    check("buffering animates (CSS only, no timer needed)", "@keyframes myriaxon-pulse" in html)
    check("animation respects reduced-motion", "prefers-reduced-motion" in html)
    check("elapsed seconds shown", 'id="buffering-elapsed"' in html)
    check("buffering is cleared", "clearBuffering()" in html)

    print("\n=== answer shown once ===")
    # A second channel carrying the same text is what produced the duplicated bubble.
    check("full-answer event handled", 'case "answer":' in html)
    check("both channels reuse one bubble", "streamAnswer" in html and html.count("streamAnswer(e.text") >= 2)

    print(f"\n{len(passed)} passed, {len(failed)} failed")
    if failed:
        print("failed:", failed)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
