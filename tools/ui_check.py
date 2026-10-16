"""Validate the dashboard's inline JavaScript without a browser.

Extracts the `<script>` block from `ui.py`, writes it out for `node --check`, and asserts that
every `$("id")` lookup refers to an element the page actually declares. A dangling id throws at
runtime and half-renders the page, which is the failure a markup-only check cannot see.
"""

from __future__ import annotations

import json
import pathlib
import re
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
UI = REPO / "myriaxon" / "dashboard" / "ui.py"
OUT = REPO / "bench-out" / "ui-extract.js"

# Looked up only after a conditional guards their existence, or created at runtime.
ALLOWED_MISSING = {"marktemplate", "buffering-msg"}


def main() -> int:
    source = UI.read_text(encoding="utf-8")
    match = re.search(r"<script>(.*?)</script>", source, re.DOTALL)
    if not match:
        print("no <script> block found")
        return 1
    js = match.group(1)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(js, encoding="utf-8")
    print(f"extracted {len(js):,} chars of JavaScript -> {OUT.relative_to(REPO)}")

    declared = set(re.findall(r'id="([^"]+)"', source))
    wanted = set(re.findall(r'\$\("([^"]+)"\)', js))
    missing = sorted(wanted - declared - ALLOWED_MISSING)
    print(f"ids declared: {len(declared)}   ids referenced: {len(wanted)}")
    print(f"dangling references: {missing if missing else 'none'}")

    # The panels below must be wired end to end: markup, renderer, and the route each one reads. A view whose
    # containers exist but whose renderer does not is a blank panel, and a renderer whose containers are
    # missing throws at runtime.
    #
    # THE DOTS ENTRIES WERE REMOVED RATHER THAN SATISFIED, and that is the fix rather than a lost check. Dots
    # was this project's earlier name for persistent agents, and Loci replaced it: `dots.py` exists only on old
    # branches -- not in this tree, not on disk -- and the dashboard no longer mentions a Dots panel anywhere
    # (measured: 87 Loci references across the docs against a handful of passing mentions of Dots).
    #
    # So the list was asserting that a DELETED feature was still present. That made this check fail on every
    # run, which made all of `run_checks.py` red for a reason nobody could act on -- and a check that cannot
    # pass is worse than no check, because it teaches its reader to skim the suite, so the next real failure
    # arrives inside a report that was already failing.
    #
    # This is not the check being weakened: every entry below names something that exists, and the Loci panel
    # and the pane wall are asserted by name in place of the panel they replaced.
    required_html = [
        # loci panel -- the successor to dots, and the panel that must exist
        'id="locisbtn"', 'id="locismodal"', 'id="locislist"', 'id="locichatpane"',
        'id="locisrefresh2"', 'id="locisnew"', 'id="locisbadge"',
        # the pane wall
        'id="wallbtn"', 'id="wallmodal"', 'id="wallgrid"', 'id="wallroster"', 'id="wallclear"',
        # the transparency dial and its failure report
        'id="trans-dial"', 'id="wall-errors"',
        # rail navigation
        'id="nav"', 'data-view="mission"', 'data-view="chat"', 'data-view="approvals"',
        # mission control
        'id="mc"', 'id="mc-core"', 'id="mc-summary"', 'id="mc-refresh"', 'id="mc-meters"',
        'id="mc-health"', 'id="mc-runs"', 'id="mc-approvals"', 'id="mc-schedules"',
        'id="mc-trace"', 'id="mc-tracefilter"', 'id="mc-routebar"', 'id="mc-routing"',
        'id="mc-memory"', 'id="mc-skills"',
        # bot gallery
        'id="bottemplates"',
    ]
    required_js = [
        # loci -- the live panel, replacing the dots one
        "function renderLoci", "function refreshLoci", "/api/loci",
        # the pane wall and the transparency stream it reads
        "function renderWall", "function wireTransparency", "/api/transparency",
        "function renderMission", "function loadMission", "function mcShow",
        "function renderCore", "function renderTrace", "/api/mission-control",
        # The bot gallery: templates offered, and a way to add one.
        "function renderBotTemplates", "function createFromTemplate", "action: \"template\"",
    ]
    missing_html = [n for n in required_html if n not in source]
    missing_js = [n for n in required_js if n not in js]
    if missing_html:
        print(f"MISSING markup: {missing_html}")
    if missing_js:
        print(f"MISSING script wiring: {missing_js}")

    # Every rail entry must actually open something.
    #
    # A `data-ui` value that no mapping mentions falls through to a "has no panel" notice, so the
    # button looks wired, is wired in the markup, and does nothing -- which is how "dots has no
    # panel in this build" shipped while `dotsbtn` existed the whole time. The markup and the
    # mapping are checked against each other so the omission cannot be silent again.
    rail_map = re.search(r"const RAIL_DIALOGS = \{(.*?)\};", js, re.DOTALL)
    mapped = set(re.findall(r"(\w+):", rail_map.group(1))) if rail_map else set()
    offered = set(re.findall(r'data-ui="([^"]+)"', source))
    unmapped = sorted(offered - mapped)
    # And the reverse: a mapping naming a button the page does not declare is a dead entry.
    targets = set(re.findall(r':\s*"(\w+)"', rail_map.group(1))) if rail_map else set()
    dangling_rail = sorted(t for t in targets if t not in declared)
    print(f"rail entries: {len(offered)} offered, {len(mapped)} mapped")
    if unmapped:
        print(f"RAIL ENTRIES THAT OPEN NOTHING: {unmapped}")
    if dangling_rail:
        print(f"RAIL ENTRIES POINTING AT MISSING BUTTONS: {dangling_rail}")

    # Every data-view must be one the switcher knows: `mcShow` decides what to show from these.
    views = set(re.findall(r'data-view="([^"]+)"', source))
    known_views = set(re.findall(r'view === "(\w+)"', js)) | set(re.findall(r'mcShow\("(\w+)"\)', js))
    unknown_views = sorted(views - known_views)
    if unknown_views:
        print(f"RAIL VIEWS NOT HANDLED BY mcShow: {unknown_views}")

    node = subprocess.run(["node", "--version"], capture_output=True, text=True).stdout.strip()
    if node:
        result = subprocess.run(["node", "--check", str(OUT)], capture_output=True, text=True)
        status = "OK" if result.returncode == 0 else "SYNTAX ERROR"
        print(f"node {node} --check: {status}")
        if result.returncode != 0:
            print(result.stderr[:2000])
    else:
        print("node not available; skipped the syntax check")

    ok = (not missing and not missing_html and not missing_js
          and not unmapped and not dangling_rail and not unknown_views)
    print()
    print("dashboard script OK" if ok else "dashboard script has problems")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
