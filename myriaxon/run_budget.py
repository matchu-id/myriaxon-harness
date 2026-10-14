"""Loop budgets: put a ceiling on a run, and make the ceiling the reason it stopped.

## The gap this closes

Both reference harnesses leave the agent loop unbounded, and both say so. Claude Code's `ralph-wiggum` loop
defaults to `--max-iterations 0` — unlimited — with a warning printed at setup, and its only real bound is the
model's own claim that it finished. DeepSeek's loop is `while (await this.turn())` over an unbounded
`while (true)` step loop, with no step cap, no wall-clock budget and no currency budget; their `TurnEndReason`
has no "budget exhausted" member. Bounds exist in both, but every one is *targeted*: a per-tool timeout, a retry
policy, a compaction threshold, a workflow agent cap.

Myriaxon is in the same position. `MAX_PAUSE_TURN_RESTARTS` exists, compaction thresholds exist, and a Locus run has
a wall-clock limit — but a session turn itself has no ceiling. A model that keeps calling tools is bounded only
by the user watching it.

**This is the gap where copying the reference is the wrong move, and DeepSeek's scan says so directly**: "The
absence of a global step/time/token bound — right for an interactive GUI, wrong for a script." Myriaxon runs
unattended Loci, so it is a script at least half the time.

## The design

A `Budget` is set per turn, checked at the step boundary — never mid-step, because interrupting a tool call leaves
a `tool_use` with no `tool_result` and the API rejects that transcript forever (a failure this harness already
documents at the rollback in `agent.py`).

Three ceilings, each answering a different runaway:

* **steps** — a model calling tools in a circle. Caught fastest and cheapest.
* **wall clock** — a hung or pathologically slow tool, or a model that is making progress but far too slowly.
* **tokens** — the cost ceiling. A step cap alone lets five enormous steps through; a token cap alone lets ten
  thousand tiny ones through.

The reason is a **first-class value**, not a string. Their `TurnEndReason` is a closed union and its lack of a
budget member is precisely the defect; making the reason an enum here means a caller can branch on "stopped for
budget" without matching on prose, and a test can assert that the budget — not the model — ended the run.

## What is deliberately NOT copied

`ralph-wiggum`'s completion promise: an exact `<promise>` string the *model* emits to end the loop. Their own
README warns it is unreliable and says to rely on `--max-iterations` instead. A loop whose exit condition is
self-reported is a loop with no exit condition, so this module has no equivalent — a run ends on a ceiling or on
the model producing a turn with no tool calls, and never on an assertion of success.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum


class StopReason(str, Enum):
    """Why a run ended. A closed set, so a caller can branch on it without matching prose.

    An enum rather than a string because the defect being fixed is exactly that these harnesses have no
    budget-exhausted member in their end-reason union.
    """

    COMPLETED = "completed"          # the model produced a turn with no tool calls
    STEP_BUDGET = "step-budget"
    TIME_BUDGET = "time-budget"
    TOKEN_BUDGET = "token-budget"
    CANCELLED = "cancelled"
    ERROR = "error"

    @property
    def is_budget(self) -> bool:
        return self in (StopReason.STEP_BUDGET, StopReason.TIME_BUDGET, StopReason.TOKEN_BUDGET)


# Defaults chosen so an ordinary interactive session never reaches them, and an unattended run cannot exceed
# them. DeepSeek's outer attempt budget is `min(160, max(32, 24 + profiles*8))`, which is the same order.
DEFAULT_MAX_STEPS = 120
DEFAULT_MAX_SECONDS = 3600.0
DEFAULT_MAX_TOKENS = 2_000_000


@dataclass
class Budget:
    """Ceilings for one run. `None` on a field means that axis is not bounded."""

    max_steps: int | None = DEFAULT_MAX_STEPS
    max_seconds: float | None = DEFAULT_MAX_SECONDS
    max_tokens: int | None = DEFAULT_MAX_TOKENS
    label: str = "turn"

    def render(self) -> str:
        parts = []
        if self.max_steps is not None:
            parts.append(f"{self.max_steps} steps")
        if self.max_seconds is not None:
            parts.append(f"{self.max_seconds:.0f}s")
        if self.max_tokens is not None:
            parts.append(f"{self.max_tokens:,} tokens")
        return f"{self.label}: " + (", ".join(parts) if parts else "unbounded")


@dataclass
class Spend:
    """What a run has used so far."""

    steps: int = 0
    started_at: float = field(default_factory=time.monotonic)
    tokens: int = 0

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started_at

    def render(self) -> str:
        return f"{self.steps} step(s), {self.elapsed:.0f}s, {self.tokens:,} tokens"


class BudgetExceeded(RuntimeError):
    """Raised at a step boundary when a ceiling is reached.

    Carries the reason so the caller records *why* the run stopped rather than inferring it from an exception
    type, and so a status line can say "stopped: step budget" instead of "failed".
    """

    def __init__(self, reason: StopReason, spend: Spend, budget: Budget) -> None:
        self.reason = reason
        self.spend = spend
        self.budget = budget
        super().__init__(f"{budget.label} stopped: {reason.value} ({spend.render()} of {budget.render()})")


class RunBudget:
    """Tracks one run against its `Budget`.

    The check is `check()` at the **step boundary** and nothing else. That placement is deliberate and is the
    reason this is safe to enforce at all: interrupting mid-step would leave a `tool_use` block with no matching
    `tool_result`, and the Anthropic API rejects such a transcript on every subsequent turn. A budget that
    corrupts a session is worse than no budget.
    """

    def __init__(self, budget: Budget | None = None, *, clock=time.monotonic) -> None:
        self.budget = budget or Budget()
        self.spend = Spend(started_at=clock())
        self._clock = clock
        self.stopped: StopReason | None = None
        self.checks = 0

    # ---- accounting

    def begin_step(self) -> None:
        """Record that a step is starting. Does not check; checking happens before, at the boundary."""
        self.spend.steps += 1

    def add_tokens(self, count: int) -> None:
        """Record tokens spent. Called after a response, so the ceiling is enforced at the next boundary."""
        if count and count > 0:
            self.spend.tokens += int(count)

    def _elapsed(self) -> float:
        return self._clock() - self.spend.started_at

    # ---- enforcement

    def check(self) -> None:
        """Raise `BudgetExceeded` if any ceiling is reached. Call at the step boundary, before starting work.

        Steps are checked as `>=` rather than `>` so the limit is the number of steps actually allowed: a budget of
        3 permits steps 1, 2 and 3, and refuses the fourth.
        """
        self.checks += 1
        budget = self.budget

        if budget.max_steps is not None and self.spend.steps >= budget.max_steps:
            self.stopped = StopReason.STEP_BUDGET
            raise BudgetExceeded(StopReason.STEP_BUDGET, self.spend, budget)
        if budget.max_seconds is not None and self._elapsed() >= budget.max_seconds:
            self.stopped = StopReason.TIME_BUDGET
            raise BudgetExceeded(StopReason.TIME_BUDGET, self.spend, budget)
        if budget.max_tokens is not None and self.spend.tokens >= budget.max_tokens:
            self.stopped = StopReason.TOKEN_BUDGET
            raise BudgetExceeded(StopReason.TOKEN_BUDGET, self.spend, budget)

    # ---- reporting

    @property
    def elapsed(self) -> float:
        return self._elapsed()

    def remaining(self) -> dict[str, float | None]:
        """What is left on each axis, for a status line.

        Returns `None` for an unbounded axis rather than a large number: a caller displaying the value should be
        able to say "unbounded" rather than invent a figure.
        """
        budget = self.budget
        return {
            "steps": None if budget.max_steps is None else max(0, budget.max_steps - self.spend.steps),
            "seconds": None if budget.max_seconds is None else max(0.0, budget.max_seconds - self._elapsed()),
            "tokens": None if budget.max_tokens is None else max(0, budget.max_tokens - self.spend.tokens),
        }

    def warn_at(self, fraction: float = 0.8) -> StopReason | None:
        """The axis closest to its ceiling, if any is past `fraction`. For a one-time warning.

        Returning the axis rather than a bool lets the warning name what is nearly exhausted, which is the
        difference between "running low" and "3 steps left".
        """
        budget = self.budget
        candidates: list[tuple[float, StopReason]] = []
        if budget.max_steps:
            candidates.append((self.spend.steps / budget.max_steps, StopReason.STEP_BUDGET))
        if budget.max_seconds:
            candidates.append((self._elapsed() / budget.max_seconds, StopReason.TIME_BUDGET))
        if budget.max_tokens:
            candidates.append((self.spend.tokens / budget.max_tokens, StopReason.TOKEN_BUDGET))
        if not candidates:
            return None
        ratio, reason = max(candidates)
        return reason if ratio >= fraction else None

    def render(self) -> str:
        state = f"stopped: {self.stopped.value}" if self.stopped else "running"
        return f"{self.budget.label} [{state}] {self.spend.render()}"


def budget_for(
    *,
    interactive: bool = True,
    unattended: bool = False,
    label: str = "turn",
    **overrides,
) -> Budget:
    """A budget appropriate to how the run is being driven.

    An interactive session is bounded generously, because a person is watching and can interrupt — the ceiling is
    a backstop against a runaway, not a working limit. An unattended run (a Locus, a scheduled job) is bounded much
    more tightly, because nobody is there to notice and the cost is real. This is the one place the distinction
    is encoded, so it cannot drift between callers.
    """
    if unattended:
        defaults = dict(max_steps=60, max_seconds=900.0, max_tokens=400_000)
    elif interactive:
        defaults = dict(max_steps=DEFAULT_MAX_STEPS, max_seconds=DEFAULT_MAX_SECONDS,
                        max_tokens=DEFAULT_MAX_TOKENS)
    else:
        defaults = dict(max_steps=200, max_seconds=7200.0, max_tokens=4_000_000)
    defaults.update({key: value for key, value in overrides.items() if value is not None})
    return Budget(label=label, **defaults)
