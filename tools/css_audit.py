"""Audit the dashboard's stylesheet against the markup and script that use it.

Three failure modes, all of which shipped in this project and none of which any browser console
reports:

1. **A class used in markup with no rule.** It renders as an unstyled element, which is how the
   messaging dialog's checkbox labels ended up floating under their own rows -- ``.chk`` was
   referenced in three places and defined nowhere.
2. **A ``var()`` referencing a variable the page never declares.** An undefined custom property is
   not an error: it resolves to nothing and the element silently loses that property. The previous
   design shipped its code panels in the wrong font this way.
3. **A rule for a class nothing uses.** Dead CSS is not a bug, but it is usually a half-finished
   rename, and it hides the real name when someone greps for it.

Run:  python tools/css_audit.py
Exit code is non-zero when there is anything in category 1 or 2.
"""

from __future__ import annotations

import pathlib
import re
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
UI = REPO / "myriaxon" / "dashboard" / "ui.py"

# Classes produced by script at runtime rather than written in the markup, so their absence from a
# static scan is expected. Named explicitly rather than pattern-matched, so a genuine typo cannot
# hide behind a prefix.
DYNAMIC_CLASSES = {
    "open", "on", "active", "streaming", "hidden", "due", "ok", "err", "warn",
    "pending", "active", "blocked", "done", "user", "assistant", "new", "meta",
    "add", "del", "hunk", "ctx", "current", "busy", "error", "warning", "success",
    "output", "notice", "msg", "dim", "muted", "faint", "mono", "cl",
}

# Elements that legitimately carry no styling of their own; they are hooks for the script or for
# behaviour defined elsewhere.
UNSTYLED_BY_DESIGN = {"marktemplate", "markslot"}


def main() -> int:
    source = UI.read_text(encoding="utf-8")
    if "<style>" not in source or "</style>" not in source:
        print("no <style> block found")
        return 1
    style = source.split("<style>", 1)[1].split("</style>", 1)[0]
    script = source.split("<script>", 1)[1].split("</script>", 1)[0] if "<script>" in source else ""

    # --- classes used -------------------------------------------------------------------
    used: set[str] = set()
    for match in re.finditer(r'class="([^"]+)"', source):
        used.update(match.group(1).split())
    # classList.add/remove/toggle("x") and className = "x" in the script.
    for match in re.finditer(r'classList\.(?:add|remove|toggle|contains)\("([\w-]+)"', script):
        used.add(match.group(1))
    for match in re.finditer(r'className\s*=\s*"([^"]+)"', script):
        used.update(match.group(1).split())
    # Template-literal classNames are scanned for *literal* class names only. Interpolated class
    # names (`class="dottag ${dot.proactive ? "on" : ""}"`) are not statically knowable, so their
    # expressions are stripped before scanning -- otherwise a naive regex reports classes called
    # "${dot.proactive" and the real findings get lost in the noise.
    for match in re.finditer(r'class="([^"]*)"', script):
        text = re.sub(r"\$\{[^{}]*\}", " ", match.group(1))
        used.update(text.split())

    styled = set(re.findall(r"\.(-?[a-zA-Z_][\w-]*)", style))

    # A CSS class name cannot contain `$`, `{`, `}` or `?`, so anything that does is an artefact of
    # scanning a template literal rather than a real class. Filtered rather than chased: the scan
    # is a lint, and a lint whose output needs interpreting gets ignored.
    valid = re.compile(r"^-?[a-zA-Z_][\w-]*$")
    unstyled = sorted(
        c for c in used - styled
        if valid.match(c) and c not in DYNAMIC_CLASSES and c not in UNSTYLED_BY_DESIGN
    )

    # --- css variables ------------------------------------------------------------------
    referenced = set(re.findall(r"var\((--[a-z0-9-]+)", style))
    referenced |= set(re.findall(r"var\((--[a-z0-9-]+)", source))
    declared = set(re.findall(r"(--[a-z0-9-]+)\s*:", style))
    undefined = sorted(referenced - declared)

    # --- dead rules ---------------------------------------------------------------------
    dead = sorted(
        c for c in styled - used
        if not c.startswith("webkit") and c not in {"mark", "core-ring", "core-dot", "headmark",
                                                    "wordmark", "markslot", "datalist"}
    )

    print(f"classes used     : {len(used)}")
    print(f"classes styled   : {len(styled)}")
    print(f"css vars used    : {len(referenced)}   declared: {len(declared)}")
    print()

    if unstyled:
        print("FAIL  classes used in markup/script with no rule:")
        for name in unstyled:
            print(f"        .{name}")
    else:
        print("ok    every class in use has a rule")

    if undefined:
        print("FAIL  var() referencing an undeclared variable:")
        for name in undefined:
            print(f"        {name}")
    else:
        print("ok    every var() reference resolves")

    if dead:
        print(f"note  {len(dead)} rule(s) whose class nothing uses (possibly a half-finished rename):")
        for name in dead[:20]:
            print(f"        .{name}")
    print()

    failures = len(unstyled) + len(undefined)
    print("stylesheet audit clean" if not failures else f"stylesheet audit: {failures} problem(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
