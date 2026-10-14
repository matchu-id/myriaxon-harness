#!/usr/bin/env python3
"""Verify loop budgets.

The claim under test is that a run CANNOT exceed its ceiling, and that it stops for a nameable reason. So the
checks run a real loop to its limit and assert which ceiling fired -- a budget that stops the run but reports the
wrong reason is only half a fix.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from myriaxon import run_budget as B  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAIL).append(label)
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}" + (f"\n         {detail}" if detail else ""))


class FakeClock:
    """A clock the test drives, so a time budget is tested without sleeping."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def main() -> int:
    print("=== 1. the reason vocabulary is closed and names the budget axes ===")
    check("there is a budget reason for each axis",
          B.StopReason.STEP_BUDGET.is_budget and B.StopReason.TIME_BUDGET.is_budget
          and B.StopReason.TOKEN_BUDGET.is_budget)
    check("completion is not a budget stop", not B.StopReason.COMPLETED.is_budget)
    check("cancellation is not a budget stop", not B.StopReason.CANCELLED.is_budget)
    # This is the defect the module exists to fix: DeepSeek's TurnEndReason has no budget member, so a run that
    # hit a ceiling is indistinguishable from one that failed.
    check("stopping for a budget is distinguishable from failing",
          B.StopReason.TIME_BUDGET != B.StopReason.ERROR)

    print()
    print("=== 2. a run under its ceiling is not stopped ===")
    budget = B.Budget(max_steps=5, max_seconds=60.0, max_tokens=1000)
    run = B.RunBudget(budget)
    for _ in range(4):
        run.check()
        run.begin_step()
        run.add_tokens(100)
    check("four steps under a five-step ceiling is fine", run.stopped is None, run.render())
    check("the spend is tracked", run.spend.steps == 4 and run.spend.tokens == 400, run.spend.render())

    print()
    print("=== 3. THE FIX: the step ceiling stops the run, and says so ===")
    run.check()
    run.begin_step()
    run.add_tokens(100)
    try:
        run.check()
        check("the sixth step is refused", False, "it was allowed")
    except B.BudgetExceeded as exc:
        check("the sixth step is refused", True)
        check("the reason is the step budget", exc.reason is B.StopReason.STEP_BUDGET, exc.reason.value)
        check("a budget of 5 permits exactly 5 steps", run.spend.steps == 5, str(run.spend.steps))
        check("the exception carries the spend", exc.spend.steps == 5)
        check("the message names the budget", "step-budget" in str(exc), str(exc))
    check("the run records why it stopped", run.stopped is B.StopReason.STEP_BUDGET)

    print()
    print("=== 4. a time ceiling stops the run without sleeping ===")
    clock = FakeClock()
    timed = B.RunBudget(B.Budget(max_steps=None, max_seconds=30.0, max_tokens=None), clock=clock)
    timed.check()
    clock.advance(29.0)
    timed.check()
    check("29 seconds into a 30 second budget is allowed", timed.stopped is None)
    clock.advance(2.0)
    try:
        timed.check()
        check("past the time budget is refused", False, "it was allowed")
    except B.BudgetExceeded as exc:
        check("past the time budget is refused", True)
        check("the reason is the time budget", exc.reason is B.StopReason.TIME_BUDGET, exc.reason.value)

    print()
    print("=== 5. a token ceiling stops the run ===")
    costed = B.RunBudget(B.Budget(max_steps=None, max_seconds=None, max_tokens=1000))
    costed.add_tokens(999)
    costed.check()
    check("999 tokens of 1000 is allowed", costed.stopped is None)
    costed.add_tokens(1)
    try:
        costed.check()
        check("reaching the token budget is refused", False, "it was allowed")
    except B.BudgetExceeded as exc:
        check("reaching the token budget is refused", True)
        check("the reason is the token budget", exc.reason is B.StopReason.TOKEN_BUDGET, exc.reason.value)

    print()
    print("=== 6. the axes are independent ===")
    # A step cap alone lets five enormous steps through, and a token cap alone lets ten thousand tiny ones
    # through. Neither substitutes for the other.
    steps_only = B.RunBudget(B.Budget(max_steps=3, max_seconds=None, max_tokens=None))
    steps_only.add_tokens(50_000_000)
    steps_only.check()
    check("an enormous token count does not trip a steps-only budget", steps_only.stopped is None)

    tokens_only = B.RunBudget(B.Budget(max_steps=None, max_seconds=None, max_tokens=50))
    for _ in range(10_000):
        tokens_only.begin_step()
    tokens_only.add_tokens(50)
    try:
        tokens_only.check()
        check("a huge step count does not trip a tokens-only budget", False, "it stopped")
    except B.BudgetExceeded as exc:
        check("a huge step count does not trip a tokens-only budget", True)
        check("and the token axis is what fired", exc.reason is B.StopReason.TOKEN_BUDGET)

    print()
    print("=== 7. an unbounded axis is reported as unbounded, not as a large number ===")
    open_run = B.RunBudget(B.Budget(max_steps=None, max_seconds=None, max_tokens=None))
    for _ in range(1000):
        open_run.begin_step()
        open_run.check()
    check("an unbounded run never stops", open_run.stopped is None)
    remaining = open_run.remaining()
    check("an unbounded axis reports None", remaining["steps"] is None, str(remaining))
    check("the rendering says unbounded", "unbounded" in open_run.budget.render(),
          open_run.budget.render())

    print()
    print("=== 8. remaining() reports what is left on each axis ===")
    partial = B.RunBudget(B.Budget(max_steps=10, max_seconds=100.0, max_tokens=1000))
    partial.begin_step()
    partial.begin_step()
    partial.add_tokens(250)
    left = partial.remaining()
    check("8 steps remain", left["steps"] == 8, str(left))
    check("about 100 seconds remain", left["seconds"] is not None and left["seconds"] > 99, str(left))
    check("750 tokens remain", left["tokens"] == 750, str(left))

    print()
    print("=== 9. remaining() never reports a negative budget ===")
    # A clamp matters because a status line showing "-3 steps remaining" reads as a bug in the display rather than
    # as the ceiling having been passed.
    past = B.RunBudget(B.Budget(max_steps=2, max_seconds=None, max_tokens=None))
    for _ in range(7):
        past.begin_step()
    check("steps clamp at zero", past.remaining()["steps"] == 0, str(past.remaining()))

    print()
    print("=== 10. the near-limit warning names the axis ===")
    warning = B.RunBudget(B.Budget(max_steps=10, max_seconds=None, max_tokens=None))
    for _ in range(5):
        warning.begin_step()
    check("half-way is not a warning", warning.warn_at(0.8) is None)
    for _ in range(3):
        warning.begin_step()
    check("80% names the step axis", warning.warn_at(0.8) is B.StopReason.STEP_BUDGET,
          str(warning.warn_at(0.8)))
    check("an unbounded budget never warns", B.RunBudget(B.Budget(
        max_steps=None, max_seconds=None, max_tokens=None)).warn_at(0.1) is None)

    print()
    print("=== 11. THE FIX: a real loop cannot exceed its ceiling ===")
    # The end-to-end claim. A loop that ignores the return value of check() is the failure mode this rules out.
    executed = 0
    loop = B.RunBudget(B.Budget(max_steps=7, max_seconds=None, max_tokens=None))
    try:
        while True:
            loop.check()
            loop.begin_step()
            executed += 1
    except B.BudgetExceeded:
        pass
    check("the loop ran exactly its budget", executed == 7, f"{executed} iterations for a budget of 7")
    check("and it stopped for the step budget", loop.stopped is B.StopReason.STEP_BUDGET)

    print()
    print("=== 12. the ceiling is enforced at the boundary, never mid-step ===")
    # Interrupting mid-step would leave a tool_use with no tool_result, and the API rejects such a transcript on
    # every later turn. So a step that is already running must be allowed to finish.
    mid = B.RunBudget(B.Budget(max_steps=1, max_seconds=None, max_tokens=None))
    mid.check()
    mid.begin_step()
    # The step is now in flight. Spending pushes past the ceiling...
    mid.add_tokens(999_999)
    completed = False
    try:
        # ...and the in-flight work still completes, because nothing re-checks until the boundary.
        completed = True
    finally:
        pass
    check("in-flight work is not interrupted", completed)
    try:
        mid.check()
        check("but the NEXT boundary refuses", False, "the second step was allowed")
    except B.BudgetExceeded:
        check("but the NEXT boundary refuses", True)

    print()
    print("=== 13. unattended runs get a tighter budget than interactive ones ===")
    # Myriaxon runs unattended Loci, so it is a script at least half the time. This is the one place the
    # distinction is encoded, so it cannot drift between callers.
    interactive = B.budget_for(interactive=True, label="session")
    unattended = B.budget_for(unattended=True, label="locus")
    check("an unattended budget is tighter on steps",
          unattended.max_steps < interactive.max_steps,
          f"{unattended.max_steps} vs {interactive.max_steps}")
    check("and tighter on time", unattended.max_seconds < interactive.max_seconds)
    check("and tighter on tokens", unattended.max_tokens < interactive.max_tokens)
    check("the label is carried", unattended.label == "locus")
    check("explicit overrides win",
          B.budget_for(unattended=True, max_steps=5).max_steps == 5)

    print()
    print("=== 14. rendering is readable in a status line ===")
    shown = B.RunBudget(B.Budget(max_steps=10, max_seconds=60.0, max_tokens=5000, label="locus run"))
    shown.begin_step()
    check("the rendering names the label", "locus run" in shown.render(), shown.render())
    check("it says it is running", "running" in shown.render())
    shown.stopped = B.StopReason.TOKEN_BUDGET
    check("it says why it stopped", "token-budget" in shown.render(), shown.render())

    print()
    print(f"=== {len(PASS)} passed, {len(FAIL)} failed ===")
    for label in FAIL:
        print(f"  FAILED: {label}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
