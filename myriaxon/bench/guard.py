"""Per-instance budgets and sampling determinism.

Two things a benchmark needs that a human-facing agent does not:

1. **A ceiling.** Otherwise an instance can loop until the account is empty, and no result is
   comparable because nobody knows what budget it got. Budgets are enforced *between* requests and
   at every tool call, so overshoot is bounded by a single request's `max_tokens`, never unbounded.
2. **Determinism where the provider allows it.** Temperature and seed are recorded in the run
   manifest even when the provider ignores them, because "we asked for 0" and "we got 0" are
   different claims and a report must not conflate them.

Enforcement is monkeypatch-based rather than an edit inside each provider's loop: the same guard
then covers all three agent implementations (`agent.py`, `openai_agent.py`, `gemini_agent.py`),
including nested sub-agents, without a per-provider divergence. The patch is installed and removed
per instance, and is a no-op if a provider renames its stream method (the runner then reports
`budget_enforced: false` in the manifest rather than silently pretending it was enforced).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from .prices import estimate_cost_usd


# Retry policy for transient provider failures. A benchmark that loses a whole task block because
# a 429 or a 503 happened to land on one attempt is measuring the network, not the agent -- and the
# failure is invisible in the score because it is (correctly) excluded as non-attributable.
RETRYABLE_MARKERS = (
    "429",
    "rate limit",
    "too many requests",
    "500",
    "502",
    "503",
    "504",
    "overloaded",
    "timed out",
    "timeout",
    "connection",
    "temporarily unavailable",
    "upstream",
)
MAX_ATTEMPT_RETRIES = 2
RETRY_BACKOFF_SECONDS = 8.0


def is_retryable(exc: BaseException) -> bool:
    """True for failures where retrying the *same* instance is the correct response.

    Deliberately excludes credential and credit problems: those are deterministic, and retrying
    them just burns wall-clock and makes the run's failure mode harder to see.
    """
    text = f"{type(exc).__name__} {exc}".lower()
    if any(marker in text for marker in ("invalid api key", "authentication", "unauthorized", "401", "403", "402", "insufficient credit", "quota")):
        return False
    return any(marker in text for marker in RETRYABLE_MARKERS)


class BudgetExceeded(Exception):
    """Raised inside the agent loop when a declared ceiling is hit.

    Caught by the runner, which records status `budget_exceeded` -- deliberately *not* folded into
    `fail`, because "ran out of the budget we gave it" and "could not do the task" are different
    findings, and a benchmark that merges them cannot tell you whether to raise the budget or
    change the model.
    """

    def __init__(self, reason: str, *, steps: int = 0, seconds: float = 0.0, cost_usd: float | None = None):
        super().__init__(reason)
        self.reason = reason
        self.steps = steps
        self.seconds = seconds
        self.cost_usd = cost_usd


@dataclass
class Budget:
    """Declared ceilings for one instance. `None` means unbounded (recorded as such)."""

    max_steps: int | None = 60          # tool calls, across the whole agent tree
    max_seconds: float | None = 900.0   # wall clock for the whole instance
    max_cost_usd: float | None = 0.50
    max_tokens_per_request: int | None = None
    max_subagent_depth: int = 1         # 0 disables `task`/`parallel_tasks` entirely

    def to_dict(self) -> dict:
        return {
            "max_steps": self.max_steps,
            "max_seconds": self.max_seconds,
            "max_cost_usd": self.max_cost_usd,
            "max_tokens_per_request": self.max_tokens_per_request,
            "max_subagent_depth": self.max_subagent_depth,
        }


@dataclass
class BudgetGuard:
    """Tracks consumption and decides when to abort.

    `check()` is called from inside the agent's request path -- the last point at which new spend
    can still be prevented. `_stream_response` runs once per model request, so checking there
    bounds overshoot to one request rather than one whole turn.
    """

    budget: Budget
    model: str = ""
    started_at: float = field(default_factory=time.monotonic)
    steps: int = 0
    depth: int = 0
    spend_usd: float | None = None
    enforced: bool = True
    # Reads the live step count. Wired to the trace recorder so the ceiling reflects calls that
    # actually executed, rather than a counter the agent loop would have had to remember to
    # increment -- and so nested sub-agent calls are counted too.
    step_source: object | None = None
    _agent = None
    _orig = None
    _last_input = 0
    _last_output = 0

    # -- consumption -------------------------------------------------------------
    def note_step(self) -> None:
        self.steps += 1

    def current_steps(self) -> int:
        if callable(self.step_source):
            try:
                return int(self.step_source())
            except Exception:  # noqa: BLE001 -- never let instrumentation break enforcement
                return self.steps
        return self.steps

    def elapsed(self) -> float:
        return time.monotonic() - self.started_at

    def current_cost(self) -> float | None:
        if self._agent is None:
            return self.spend_usd
        totals = getattr(self._agent, "usage_totals", None)
        if totals is None:
            return None
        return estimate_cost_usd(
            self.model, int(getattr(totals, "input_tokens", 0) or 0), int(getattr(totals, "output_tokens", 0) or 0)
        )

    def reason_if_exceeded(self) -> str | None:
        cost = self.current_cost()
        self.spend_usd = cost
        steps = self.current_steps()
        if self.budget.max_steps is not None and steps >= self.budget.max_steps:
            return f"step_budget_exhausted({steps}/{self.budget.max_steps})"
        if self.budget.max_seconds is not None and self.elapsed() >= self.budget.max_seconds:
            return f"time_budget_exhausted({self.elapsed():.0f}s/{self.budget.max_seconds:.0f}s)"
        if cost is not None and self.budget.max_cost_usd is not None and cost >= self.budget.max_cost_usd:
            return f"cost_budget_exhausted(${cost:.4f}/${self.budget.max_cost_usd:.4f})"
        return None

    def check(self) -> None:
        reason = self.reason_if_exceeded()
        if reason is not None:
            raise BudgetExceeded(
                reason,
                steps=self.current_steps(),
                seconds=self.elapsed(),
                cost_usd=self.spend_usd,
            )

    def check_after_tool_call(self) -> None:
        """Enforce the step ceiling at the tool-call boundary.

        The per-request check alone is not enough: an agent that calls five tools per model request
        would overshoot a 25-step budget by five steps between checks, and -- worse -- an agent in a
        tool-only loop that never issues another request would never be checked at all. Raising
        here unwinds through the agent's own turn-level rollback, which restores the message list to
        a structurally valid state before the runner records `budget_exceeded`.
        """
        if self.budget.max_steps is None:
            return
        self.check()

    # -- installation ------------------------------------------------------------
    def install(self, agent) -> bool:
        """Wrap the agent's per-request method so the budget is checked before every request.

        Returns True if a known stream method was found and wrapped. The runner records this in the
        result JSON: `budget_enforced: false` is an honest, visible degradation, and never a silent
        one.
        """
        self._agent = agent
        for method_name in ("_stream_response", "_stream_response_with_retry"):
            if hasattr(agent, method_name):
                self._orig = getattr(agent, method_name)
                setattr(agent, method_name, self._wrapper)
                return True
        return False

    def _wrapper(self, *args, **kwargs):
        self.check()
        return self._orig(*args, **kwargs)

    def uninstall(self) -> None:
        pass  # the agent instance is discarded at the end of every instance; nothing global is patched
