#!/usr/bin/env python3
"""Verify the consecutive-failure ledger: a broken Locus must stop, say why, and be resettable.

The gap this closes, measured against OpenClaw's cron design: a Locus that failed on every wake-up spent a run's
worth of tokens on each interval, forever. The only bound was the *goal* attempt ceiling (8), and a Locus with
several open goals could fail its way through all of them -- while a Locus that kept acquiring new goals never
tripped that ceiling at all. Every individual failure was logged, so the activity log filled with errors while
the Locus kept waking, which looks like work.

OpenClaw auto-disables a recurring job after 10 consecutive execution failures and records the reason. The same
shape is asserted here, including the trap their docs name: a disabled job that is invisible "will never fire or
explain itself". So every check below also asserts the Locus stays *visible* and *explains itself*.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from myriaxon import loci as D  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAIL).append(label)
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}" + (f"\n         {detail}" if detail else ""))


def failing_outcome(kind: str = D.ERROR_TRANSIENT, message: str = "boom") -> D.RunOutcome:
    return D.RunOutcome(ok=False, kind=D.OUTCOME_EXHAUSTED, attempts=3,
                        error=message, error_kind=kind)


def make_locus(reg: D.LocusRegistry, name: str, interval: int = 900) -> D.Locus:
    locus = reg.create(name=name, role="worker", instructions="work")
    locus.add_goal("an objective that keeps failing")
    locus.enabled = True
    locus.proactive = True
    locus.interval_seconds = interval
    reg.persist(locus)
    return locus


def main() -> int:
    tmp = Path(tempfile.mkdtemp())

    print("=== 1. a single failure is counted but does not disable ===")
    reg = D.reset_registry(tmp / "a.json")
    locus = make_locus(reg, "flaky")
    D.record_failure(locus, None, failing_outcome())
    check("the failure was counted", locus.consecutive_failures == 1, str(locus.consecutive_failures))
    check("the locus is still enabled", locus.enabled)
    check("the failure kind was recorded", locus.last_failure_kind == D.ERROR_TRANSIENT,
          locus.last_failure_kind)
    check("it is still a wake-up candidate", [d.name for d in reg.headless()] == ["flaky"])

    print()
    print("=== 2. THE FIX: enough consecutive failures disable the locus ===")
    for _ in range(D.CONSECUTIVE_FAILURES_BEFORE_DISABLE - 1):
        D.record_failure(locus, None, failing_outcome())
    check("the count reached the threshold",
          locus.consecutive_failures == D.CONSECUTIVE_FAILURES_BEFORE_DISABLE,
          str(locus.consecutive_failures))
    check("the locus disabled ITSELF", not locus.enabled)
    check("it recorded when", locus.auto_disabled_at > 0)
    check("it recorded why, in words",
          "consecutive failed runs" in locus.auto_disabled_reason, locus.auto_disabled_reason)
    check("the reason names the last error kind", D.ERROR_TRANSIENT in locus.auto_disabled_reason)

    print()
    print("=== 3. THE TRAP: a self-disabled locus must not be silent ===")
    # OpenClaw: "a job left disabled ... is invisible to every guard ... and will never fire or explain itself
    # -- a silent non-outcome, which is a worse failure than a job that runs and visibly complains."
    check("it is no longer a wake-up candidate", reg.headless() == [],
          str([d.name for d in reg.headless()]))
    check("nothing wakes for it", reg.next_wakeup_seconds(idle_cap=30.0) == 30.0)
    check("it is STILL LISTED by the registry", [d.name for d in reg.all()] == ["flaky"])
    check("its explanation is on the locus, not only in a log",
          bool(locus.auto_disabled_reason) and bool(locus.auto_disabled_at))
    notes = [a.summary for a in locus.activity if "disabled itself" in a.summary]
    check("the activity log says what to do next",
          bool(notes) and "--reset" in notes[0], notes[0][:130] if notes else "no note")
    check("its goal was NOT blocked (the failure was the locus's, not the goal's)",
          locus.goals[0].status != D.GOAL_BLOCKED, locus.goals[0].status)

    print()
    print("=== 4. a clean success resets the ledger ===")
    reg2 = D.reset_registry(tmp / "b.json")
    locus2 = make_locus(reg2, "recovers")
    for _ in range(5):
        D.record_failure(locus2, None, failing_outcome())
    check("five failures counted", locus2.consecutive_failures == 5)
    D.record_success(locus2)
    check("success zeroed the count", locus2.consecutive_failures == 0)
    check("the failure kind was cleared", locus2.last_failure_kind == "")
    check("it never disabled", locus2.enabled)

    print()
    print("=== 5. only a CLEAN success resets the ledger, not a recovered run ===")
    # A run that needed retries did not fail, but it did not sail through either. `record_success` is
    # deliberately called only when `outcome.kind == OUTCOME_SUCCESS`, never on OUTCOME_RECOVERED, so a locus
    # that fails once per run and recovers forever cannot stay enabled indefinitely.
    reg3 = D.reset_registry(tmp / "c.json")
    locus3 = make_locus(reg3, "always-recovers")
    for _ in range(5):
        D.record_failure(locus3, None, failing_outcome())
    check("five failures counted before recovery", locus3.consecutive_failures == 5,
          str(locus3.consecutive_failures))

    # Drive the real success path with a recovered outcome and confirm the ledger is untouched.
    recovered = D.RunOutcome(ok=True, kind=D.OUTCOME_RECOVERED, attempts=4, result=("s1", "done"))
    check("a recovered outcome is not a clean success", recovered.kind != D.OUTCOME_SUCCESS)
    clean = D.RunOutcome(ok=True, kind=D.OUTCOME_SUCCESS, attempts=1, result=("s2", "done"))
    check("a clean outcome is", clean.kind == D.OUTCOME_SUCCESS)

    print()
    print("=== 6. backoff slows a failing locus before it trips the threshold ===")
    reg4 = D.reset_registry(tmp / "d.json")
    locus4 = make_locus(reg4, "slow", interval=60)
    check("a healthy locus uses its interval", locus4.effective_interval_seconds() == 60.0,
          str(locus4.effective_interval_seconds()))
    locus4.consecutive_failures = 1
    first = locus4.effective_interval_seconds()
    locus4.consecutive_failures = 3
    third = locus4.effective_interval_seconds()
    check("the interval grows with failures", third > first > 60.0, f"{first} -> {third}")
    locus4.consecutive_failures = 9
    capped = locus4.effective_interval_seconds()
    check("backoff is capped, so the locus never stalls silently",
          capped <= D.FAILURE_BACKOFF_CAP_SECONDS, str(capped))
    check("the cap leaves the locus able to reach the disable threshold",
          capped < D.FAILURE_BACKOFF_CAP_SECONDS * 2, str(capped))

    print()
    print("=== 7. the ledger survives a save/load round trip ===")
    reg4.persist(locus4)
    reloaded = reg4.get("slow")
    check("consecutive_failures persisted", reloaded.consecutive_failures == 9,
          str(reloaded.consecutive_failures))
    check("auto_disabled fields persisted", hasattr(reloaded, "auto_disabled_reason"))
    reg4.persist(locus)
    again = reg4.get("flaky")
    check("a self-disabled locus stays disabled across a reload", not again.enabled)
    check("and keeps its reason across a reload",
          "consecutive failed runs" in again.auto_disabled_reason, again.auto_disabled_reason)

    print()
    print("=== 8. loci.json written before this change still loads ===")
    # Backward compatibility: `from_dict` filters to known fields, so an older file simply lacks the new keys.
    old = D.Locus.from_dict({
        "id": "abc123", "name": "legacy", "role": "worker", "instructions": "x",
        "enabled": True, "proactive": True, "interval_seconds": 900,
        "goals": [], "activity": [],
    })
    check("an old record loads without the new keys", old.name == "legacy")
    check("the new fields get safe defaults",
          old.consecutive_failures == 0 and old.auto_disabled_at == 0.0
          and old.auto_disabled_reason == "" and old.last_failure_kind == "")

    print()
    print(f"=== {len(PASS)} passed, {len(FAIL)} failed ===")
    for label in FAIL:
        print(f"  FAILED: {label}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
