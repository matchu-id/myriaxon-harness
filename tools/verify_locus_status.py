#!/usr/bin/env python3
"""Verify that `loci --status` tells the truth about a monitor.

## Why this file exists

`format_locus` used to print `wakes every 900s` and nothing else. A watcher whose condition command did not exist was
**permanently and silently quiet**, and its status line was identical to a healthy watcher's -- the defect concealed
itself. So these checks are mostly about what the status line must REVEAL, and one of them asserts the loud failure
state specifically.

The theme: an operator reading `myriaxon loci` should be able to tell a monitor that is working from one that is
blind, without reading the source or the activity log.
"""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from myriaxon import conditions as C  # noqa: E402
from myriaxon import loci  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAIL).append(label)
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}" + (f"\n         {detail}" if detail else ""))


def fresh() -> loci.LocusRegistry:
    tmp = Path(tempfile.mkdtemp())
    loci.LOCI_FILE = tmp / "loci.json"
    loci.reset_registry()
    return loci.get_registry()


def render(locus, **kw) -> str:
    return loci.format_locus(locus, **kw)


def main() -> int:
    print("=== 1. a healthy watcher reports WHAT it watches ===")
    # Without the description, "wakes every 900s" is all you get, which says nothing about the condition.
    reg = fresh()
    tmp = Path(tempfile.mkdtemp())
    health = tmp / "health.ok"
    health.write_text("ok")
    locus = reg.create("ops", goals=[], proactive=True, interval_seconds=1)
    locus.condition = {"kind": C.KIND_FILE_MISSING, "description": "the health file is gone",
                     "path": str(health), "min_seconds_between_runs": 60}
    reg.persist(locus)

    text = render(reg.get("ops"))
    check("the condition description is shown", "the health file is gone" in text, text)
    check("the kind is shown", "file_missing" in text, text)
    check("what it checks is shown", "health.ok" in text, text)
    check("the floor is shown", "60s" in text, text)

    print()
    print("=== 2. the verdict is shown, with its age ===")
    # "quiet" alone is ambiguous: quiet since when? A watcher quiet for three days means something different from
    # one that checked a minute ago.
    #
    # The first version of this check drove `is_due(now=time.time() + 10)` to make the watch due, which stamps
    # `condition_checked_at` TEN SECONDS IN THE FUTURE -- and `_relative_age` then correctly renders "just now",
    # because a future timestamp is what a clock adjustment produces. So the check set the clock forward and then
    # complained the age was not in the past. The timestamp is set directly here instead.
    live = reg.get("ops")
    live.condition_checked_at = time.time() - 120
    live.last_condition_state = "quiet"
    reg.persist(live)
    # Rendered from the SAME object that was mutated. `persist` reloads from disk internally and rebinds the
    # registry's entry, so a second `reg.get("ops")` could hand back a different instance than the one just
    # configured -- which is what made the first version of this check assert against stale state.
    text = render(live)
    check("the last verdict is shown", "last check:" in text, text)
    check("it names the verdict", "quiet" in text or "holds" in text, text)
    check("and its age", "ago" in text, text)
    check("a check 120s old reads as minutes", "min ago" in text, text)

    print()
    print("=== 3. THE LOUD CASE: a check that cannot run is unmissable ===")
    # This is the whole point. A failed check must not render as an ordinary word among many.
    live = reg.get("ops")
    live.last_condition_state = "ERROR: the program 'systemctl' was not found on PATH"
    text = render(live)
    check("a failed check is marked loudly", "!!" in text, text)
    check("it says the check FAILED TO RUN", "FAILED TO RUN" in text, text)
    check("it repeats the reason", "not found on PATH" in text, text)
    check("it spells out the consequence",
          "not the same as a check that passed" in text, text)
    check("the ERROR state does NOT render as a plain 'quiet'",
          "last check: ERROR" not in text, "the loud form replaces the quiet one")

    print()
    print("=== 4. a never-evaluated condition says so ===")
    # "quiet" would be a lie: it has not been checked.
    fresh_dot = reg.create("fresh", goals=[], proactive=True, interval_seconds=900)
    fresh_dot.condition = {"kind": C.KIND_ALWAYS, "description": "always on"}
    text = render(fresh_dot)
    check("it says not evaluated yet", "not evaluated yet" in text, text)
    check("it does NOT claim a verdict", "last check: quiet" not in text, text)

    print()
    print("=== 5. the watchlist is shown, and bounded ===")
    # Bounded because a 40-item watchlist would make the status line unreadable, and the count is the useful part.
    listed = reg.create("listed", goals=[], proactive=True)
    listed.scratch = "\n".join(f"- item {i}" for i in range(9))
    text = render(listed)
    check("the item count is shown", "9 item(s)" in text, text)
    check("the first items are shown", "item 0" in text, text)
    check("the overflow is summarised", "and 3 more" in text, text)
    check("not all nine are printed", text.count("- item ") == 6, str(text.count("- item ")))

    print()
    print("=== 6. the heartbeat verdict and the remembered note are shown ===")
    # The note is internal context carried to the next turn; an operator should be able to see it rather than
    # wonder why the agent mentioned something nobody said.
    noted = reg.create("noted", goals=[], proactive=True)
    noted.scratch = "- watch the queue"
    noted.record_heartbeat("remembered", note="disk at 80%, watching")
    text = render(noted)
    check("the last verdict is shown", "last check verdict: remembered" in text, text)
    check("the note is shown", "disk at 80%" in text, text)

    print()
    print("=== 7. `once` and the `changed` kind are visible ===")
    once = reg.create("oneshot", goals=[], proactive=True)
    once.condition = {"kind": C.KIND_CHANGED, "description": "CI status changed", "observe": "echo green",
                      "once": True, "min_seconds_between_runs": 0}
    text = render(once)
    check("the changed kind is named", "changed" in text, text)
    check("the observe command is shown", "echo green" in text, text)
    check("once is stated", "fires once" in text, text)

    print()
    print("=== 8. a locus WITHOUT a condition is not cluttered ===")
    # The new block must be additive: a plain locus's status must not gain empty headings.
    plain = reg.create("plain", goals=["do a thing"], proactive=True)
    text = render(plain)
    check("no condition section", "wakes on a condition" not in text, text)
    check("no watchlist section", "watchlist:" not in text, text)
    check("the ordinary lines survive", "open objective(s)" in text and "do a thing" in text, text)

    print()
    print("=== 9. a locus with a condition but no goals is still rendered usefully ===")
    # The maintenance case: nothing to build, so "no open objectives" must not be the only thing on screen.
    text = render(reg.get("ops"))
    check("it says there are no objectives", "no open objectives" in text)
    check("but the condition is still the headline", "the health file is gone" in text)

    print()
    print("=== 10. the relative age is coarse, and never negative ===")
    # Coarse on purpose: the difference between 61 and 63 seconds is not part of the answer. A future timestamp
    # must not render as a negative age, which is what a clock adjustment would otherwise produce.
    now = time.time()
    for delta, expect in ((5, "s ago"), (600, "min ago"), (7200, "h ago"), (200000, "d ago")):
        got = loci._relative_age(now - delta)
        check(f"{delta}s ago renders in {expect}", expect in got, got)
    check("zero renders as never", loci._relative_age(0) == "never", loci._relative_age(0))
    check("a future timestamp does not go negative", loci._relative_age(now + 500) == "just now",
          loci._relative_age(now + 500))

    print()
    print("=== 11. the verdict helper is a DRY RUN ===")
    # `doctor` and the status line both ask "would this fire?". Asking must not consume the change.
    reg2 = fresh()
    watcher = reg2.create("probe", goals=[], proactive=True, interval_seconds=1)
    watcher.condition = {"kind": C.KIND_CHANGED, "description": "CI", "observe": "echo one",
                         "min_seconds_between_runs": 0}
    reg2.persist(watcher)
    first = reg2.get("probe").condition_verdict()
    second = reg2.get("probe").condition_verdict()
    check("it returns a verdict without a model call", isinstance(first, str) and bool(first), first)
    check("asking twice gives the same answer", first == second, f"{first!r} then {second!r}")
    check("and it stores nothing",
          not reg2.get("probe").trigger_state or reg2.get("probe").condition_verdict() == first,
          str(reg2.get("probe").trigger_state))

    bad = reg2.create("invalid", goals=[], proactive=True)
    bad.condition = {"kind": "nonsense", "description": "x"}
    check("an invalid condition is reported as an error",
          reg2.get("invalid").condition_verdict().startswith("ERROR"), reg2.get("invalid").condition_verdict())

    nodot = reg2.create("none", goals=["g"], proactive=True)
    check("a locus with no condition says so",
          "no condition" in reg2.get("none").condition_verdict(),
          reg2.get("none").condition_verdict())

    print()
    print("=== 12. an ERROR verdict survives a save and reload ===")
    # The loud state is what an operator reads after a restart, so it must not be lost in serialisation.
    reg.get("ops").last_condition_state = "ERROR: the check did not finish within 30s"
    reg.persist(reg.get("ops"))
    reloaded = reg.get("ops")
    check("the verdict persisted", "ERROR" in reloaded.last_condition_state,
          reloaded.last_condition_state)
    check("and still renders loudly", "!!" in render(reloaded))
    check("the trigger state round-trips",
          isinstance(reloaded.trigger_state, dict), str(reloaded.trigger_state))

    print()
    print(f"=== {len(PASS)} passed, {len(FAIL)} failed ===")
    for label in FAIL:
        print(f"  FAILED: {label}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
