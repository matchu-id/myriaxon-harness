"""What will a full IQB-14 run cost and how long will it take?

Read the suite's own budgets rather than guessing, so the decision to spend is informed.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def main() -> int:
    from myriaxon.bench.tasks import load_suite

    suite = load_suite(REPO / "myriaxon" / "bench" / "tasks")
    if not suite:
        print("no tasks found")
        return 1

    print(f"{len(suite)} task(s)\n")
    print(f"  {'task':44} {'att':>4} {'steps':>6} {'secs':>6} {'usd':>7}")
    total_steps = total_usd = 0.0
    total_attempts = 0
    for task in suite:
        tid = getattr(task, "id", "?")
        attempts = int(getattr(task, "repeats", 1) or 1)
        # The ceilings live on `task.budget`, a nested dataclass -- not on the task itself. Reading
        # them from the wrong place reported every task as free, which is exactly the kind of number
        # that would make spending look safe when it is not.
        budget = getattr(task, "budget", None)
        steps = float(getattr(budget, "max_steps", 0) or 0)
        secs = float(getattr(budget, "max_seconds", 0) or 0)
        usd = float(getattr(budget, "max_cost_usd", 0) or 0)
        total_steps += steps * attempts
        total_usd += usd * attempts
        total_attempts += attempts
        print(f"  {tid:44} {attempts:>4} {steps:>6.0f} {secs:>6.0f} {usd:>7.2f}")

    print(f"\n  attempts (agent runs) : {total_attempts}")
    print(f"  step ceiling total    : {total_steps:,.0f}")
    print(f"  declared dollar ceiling: ${total_usd:,.2f}")
    print()
    print("  The dollar figure is the SUM OF CEILINGS, not a forecast: it is what the runner is")
    print("  allowed to spend before it stops, and a task that finishes in two steps costs a fraction.")
    print("  Judging adds calls: `agent` mode runs another full agent turn per graded parameter, and")
    print("  `api` mode is a single completion. Graded parameters are 2,4,6,7,8,9,10,11 -- eight of")
    print("  the fourteen -- so the judge roughly doubles the call count on those.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
