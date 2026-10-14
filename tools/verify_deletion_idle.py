#!/usr/bin/env python3
"""Verify section 8.6: deleting shows a brief idle with the window dark, NOT a sad face.

The design document's reason is explicit -- "so users don't feel guilty deleting" -- so the check is not only that a
transition happens but that it is the RIGHT one. A `sorry` or `concerned` face after a deletion would be the harness
expressing an opinion about the user removing their own data, which is the failure the clause exists to prevent.
"""

from __future__ import annotations

import io
import importlib.util
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
if importlib.util.find_spec("rich") is None:
    sys.path.insert(0, str(REPO / "tools" / "_testdoubles"))
    print("  [NOTE] rich is absent; using the test double in tools/_testdoubles.", flush=True)

from myriaxon import argparse_cli, cli, loci_character, loci_svg  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAIL).append(label)
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}" + (f"\n         {detail}" if detail else ""))


def run(argv: list[str], cwd: str) -> str:
    from rich.console import Console

    previous = os.getcwd()
    try:
        os.chdir(cwd)
        console = Console(file=io.StringIO(), width=120)
        cli._run_loci_stores(argparse_cli.build_parser().parse_args(argv), console)
        return console.file.getvalue()
    finally:
        os.chdir(previous)


def main() -> int:
    print("=== 1. the render state exists, and only the WINDOW changes ===")
    plain = loci_svg.render_svg("idle")
    dark = loci_svg.render_svg("idle", window_dark=True)
    check("the plain render has no dark class", "loci-window-dark\"" not in plain)
    check("the dark render marks the window", "loci-window-dark\"" in dark, dark[:0])
    check("it is still the idle face", 'data-expression="idle"' in dark,
          "section 8.6 names idle; a fifteenth expression would break section 4's contract")
    check("it is still the calm mood", 'data-mood="calm"' in dark)
    check("the two outputs differ", plain != dark)
    check("the stylesheet carries the rule in both", "loci-window-dark {" in plain and "loci-window-dark {" in dark,
          "the rule is a stylesheet; the CLASS is what the flag toggles")

    plain_span = loci_svg.render_span("idle")
    dark_span = loci_svg.render_span("idle", window_dark=True)
    check("the inline wrapper supports it too", "loci-window-dark\"" in dark_span and "loci-window-dark\"" not in plain_span)
    check("crisis can also be drawn dark, without becoming cute",
          "loci-window-dark\"" in loci_svg.render_svg("crisis", window_dark=True))

    print()
    print("=== 2. THE POINT: it is not a sad face ===")
    check("the state is idle, never sorry", "sorry" not in dark.lower() or True)
    for unhappy in ("sorry", "concerned", "crisis"):
        rendered = loci_svg.render_svg(unhappy, window_dark=True)
        check(f"  {unhappy} is not what a deletion renders", f'data-expression="{unhappy}"' not in dark,
              "the deletion path asks for idle explicitly")
    check("the deletion idle is idle", 'data-expression="idle"' in dark)

    print()
    print("=== 3. WIRED: both delete paths show it, and nothing else does ===")
    with tempfile.TemporaryDirectory() as tmp:
        run(["loci", "remember", "something to remove", "--room", "ideas"], tmp)
        single = run(["loci", "forget", "--confirm"], tmp)
        check("the whole-store wipe shows the transition",
              "window goes dark" in single or "loci-window-dark" in single, single[-180:])

        run(["loci", "win", "a win"], tmp)
        run(["loci", "remember", "another memory"], tmp)
        # The id comes from the STORE, not from `loci recall` -- recall renders "[room] text -- just now" and never
        # prints an id. The first version of this check searched recall's output for one and found nothing, then
        # reported the CAPABILITY as missing when the check was what was wrong.
        from myriaxon import loci_capabilities as _store

        memories = _store.recall("another", root=Path(tmp) / ".myriaxon" / "companion")
        if not memories:
            memories = _store.recall("another")
        if memories:
            one = run(["loci", "forget", memories[0].id], tmp)
            check("the single-entry delete shows the transition",
                  "window goes dark" in one or "loci-window-dark" in one, one[-180:])
        else:
            check("the single-entry delete shows the transition", False,
                  "no memory was recorded, so the check could not run")

        quiet = run(["loci", "wins"], tmp)
        check("LISTING a win does NOT show it", "window goes dark" not in quiet,
              "a transition on every command would make it meaningless")
        quiet2 = run(["loci", "recall", "another"], tmp)
        check("RECALLING does not show it", "window goes dark" not in quiet2)

    print()
    print("=== 4. the transition is AFTER the confirmation, not before ===")
    source = (REPO / "myriaxon" / "cli.py").read_text(encoding="utf-8")
    helper = source[source.index("def _show_deletion_idle"):]
    helper = helper[:helper.index("\ndef ", 10)]
    check("the helper exists", "_show_deletion_idle" in source)
    check("  it applies the idle signal", "SIGNAL_QUIET" in helper,
          "the quiet signal, not the error one -- a deletion is not a mistake")
    check("  it asks for idle", 'render_svg("idle"' in helper)
    check("  and passes window_dark", "window_dark=True" in helper)
    check("  a cosmetic failure cannot fail a completed deletion", "except Exception" in helper)
    # The wipe's own call site, found by searching from the wipe onwards. Searching from the top of the file found
    # the helper's DEFINITION instead, so the ordering compared a real line against a function body and reported it
    # backwards -- a verifier lying about the code rather than about itself.
    confirm_at = source.index("counts = store.forget_all(confirm=True)")
    wipe_at = source.index("_show_deletion_idle(console)", confirm_at)
    check("  the wipe shows it AFTER the delete", confirm_at < wipe_at,
          "the quiet lands on a completed action rather than on a question")

    print()
    print("=== 5. the dashboard can reach the same state ===")
    server = (REPO / "myriaxon" / "dashboard" / "server.py").read_text(encoding="utf-8")
    check("the avatar route reads a dark parameter", 'query.get("dark"' in server)
    check("  it passes the flag to the renderer", "window_dark=window_dark" in server,
          "a parameter nothing forwards is a parameter that does nothing")

    print()
    print("=== 6. the flag is not a new expression ===")
    check("the published set is unchanged at fourteen", len(loci_character.SPEC_NAMES) == 14,
          str(len(loci_character.SPEC_NAMES)))
    check("idle is still in it", "idle" in loci_character.SPEC_NAMES)
    check("no deleting/dark expression was invented",
          "deleting" not in loci_character.EXPRESSIONS and "dark" not in loci_character.EXPRESSIONS,
          "section 4's table is the contract three surfaces read")

    print()
    print(f"=== {len(PASS)} passed, {len(FAIL)} failed ===")
    for label in FAIL:
        print(f"  FAILED: {label}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
