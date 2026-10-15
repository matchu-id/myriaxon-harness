#!/usr/bin/env python3
"""Verify the Level 4 and Level 5 capabilities are REACHABLE, not merely implemented.

A thin wrapper around `bench_l4_l5.py` so the bench runs in CI. The two files are deliberately separate: the
bench is a tool a person runs and reads, with evidence printed under every line, and this is the CI-facing
verifier that has to print a summary `run_checks.py` can parse. Keeping one implementation means the number in
CI and the number on a developer's screen cannot disagree.

WHY THIS IS A VERIFIER AND NOT JUST TESTS

The pytest suite already covers each module. What it cannot cover is the question this asks: can the capability
be reached from where it is supposed to run? Both of this project's worst defects were green in the suite and
broken in production --

  * the swarm board passed its tests while being unreachable from a confined Locus, because the board lives
    outside the workspace and `_resolve` refused it;
  * per-member isolation passed while the run loop granted every peer's workspace, because the test granted a
    narrower set than production.

Neither was a missing check. Both were a check that existed on one path and not another doing the same thing.
So every check here drives the real entry point: `dispatch_tool`, the run loop's own grant list, the condition
evaluator, the live transparency dial.
"""

from __future__ import annotations

import io
import contextlib
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tools"))

import bench_l4_l5 as bench  # noqa: E402


def main() -> int:
    # The bench prints its own evidence, which is what makes a failure actionable. Captured and re-emitted only
    # when something fails, so a green run stays short enough to read.
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = bench.main()

    output = buffer.getvalue()
    passed = sum(1 for _l, _n, ok, _e in bench.RESULTS if ok)
    failed = [r for r in bench.RESULTS if not r[2]]

    if failed:
        # On failure the whole bench output is shown, because the evidence under a passing line is what tells a
        # reader whether the failing line is measuring the right thing.
        print(output)
    else:
        # On success, one line per level so the count is visible without the detail.
        for level in ("4", "5", "explain"):
            group = [r for r in bench.RESULTS if r[0] == level]
            if not group:
                continue
            good = sum(1 for _l, _n, ok, _e in group if ok)
            label = {"4": "Level 4 (coordinates with other agents)",
                     "5": "Level 5 (own sub-goals, context, risk, uncertainty)",
                     "explain": "Explainability (the eight-layer dial)"}[level]
            print(f"  [{('PASS' if good == len(group) else 'FAIL')}] {label}: {good}/{len(group)}")

    print()
    print(f"=== {passed} passed, {len(failed)} failed ===")
    for _level, name, _ok, evidence in failed:
        print(f"  FAILED: {name}")
        if evidence:
            print(f"          {str(evidence).splitlines()[0][:140]}")
    return 1 if failed or code else 0


if __name__ == "__main__":
    raise SystemExit(main())
