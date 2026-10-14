"""Per-tool cooperative deadlines, scoped so a timeout is never misattributed.

## The gap this closes

Myriaxon has a `timeout` argument on `run_bash` and a slot-acquire timeout for sub-agents. A tool that has no
such argument can hang forever, and nothing in the harness notices: `dispatch_tool` has no deadline, so a wedged
MCP server or a pathological regex parks the turn until the user interrupts.

OpenClaw's `guard/timeout-policy` closes this, and its most careful idea is not the timeout itself:

**The result is replaced only when the wrapper's OWN timer fired.** Their implementation arms a scoped deadline,
swaps it onto the execution signal, and afterwards checks `timeoutOf(deadline.signal, TOOL_TIMEOUT)`. If the outer
signal aborted for a different reason -- a user cancel, an upstream deadline that fired first -- the wrapper
leaves the result alone, because attributing that to a tool timeout would be a lie about cause. A timeout reported
for the wrong reason sends the reader looking in the wrong place.

Their second rule: **the caller's signal is restored afterwards.** The deadline is scoped to the dispatch, and
leaving it installed would apply one tool's budget to the next call.

Third: **a tool with no declared budget is untouched -- a passthrough.** Inventing a default for every tool would
be a behaviour change disguised as a safety feature.

## Cooperative, not preemptive, and why that is stated rather than hidden

Python cannot kill a thread. `signal.alarm` exists but only works on the main thread and only on POSIX. So a tool
that ignores its deadline keeps running: the harness stops *waiting* for it, records a timeout, and moves on. The
worker thread is left as a daemon, and the tool's side effects (a half-written file, a spawned process) still
happen. That is worth saying plainly in the docstring and in the result, because a caller who believes the work
was cancelled will draw the wrong conclusion about the workspace.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable

# The marker a timed-out result carries. A closed value, so a caller can branch on `is_timeout` rather than
# matching prose.
TIMEOUT_PREFIX = "[tool timed out:"

# Their default for a tool that declares nothing is *no deadline*, and that is deliberate: a default would change
# the behaviour of every existing tool. A caller who wants a floor sets `default_seconds`.
DEFAULT_NONE = None

# Used for the shared executor. Bounded so a wedged tool cannot spawn unboundedly many abandoned workers.
MAX_ABANDONED_WORKERS = 8


@dataclass
class Deadline:
    """One scoped deadline.

    `fired` distinguishes "this budget expired" from "something else aborted", which is the whole reason the
    class exists rather than a bare `future.result(timeout=...)`.
    """

    seconds: float
    tool: str = ""
    started_at: float = field(default_factory=time.monotonic)
    fired: bool = False

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started_at

    @property
    def remaining(self) -> float:
        return max(0.0, self.seconds - self.elapsed)

    def render(self) -> str:
        state = "fired" if self.fired else f"{self.remaining:.1f}s left"
        return f"{self.tool or 'tool'} deadline {self.seconds:.1f}s [{state}]"


class ToolTimeout(TimeoutError):
    """Raised internally when a tool's own deadline expires. Never escapes `run_with_deadline`."""

    def __init__(self, deadline: Deadline) -> None:
        self.deadline = deadline
        super().__init__(f"{deadline.tool or 'tool'} exceeded {deadline.seconds:.1f}s")


def format_seconds(seconds: float) -> str:
    """A budget rendered so it never reads as zero.

    `f"{0.3:.0f}s"` is "0s", which reads as "no budget at all" -- the opposite of what happened. Sub-second
    budgets keep a decimal; anything longer is rounded, because a reader does not need "90.0s" for a 90-second
    limit.
    """
    if seconds < 1:
        return f"{seconds:.2f}s"
    if seconds < 10:
        return f"{seconds:.1f}s"
    return f"{seconds:.0f}s"


def timeout_result(deadline: Deadline, partial: str = "") -> str:
    """The result text a timed-out call produces.

    Names the budget, says the work may still be running, and says what to do next. The "may still be running"
    part is not hedging -- it is the honest consequence of cooperative cancellation, and a caller told the tool
    was cancelled would wrongly assume the workspace is untouched.
    """
    note = (
        f"{TIMEOUT_PREFIX} {deadline.tool or 'this tool'} did not finish within "
        f"{format_seconds(deadline.seconds)}. The call was abandoned, not cancelled: the tool may still be "
        "running and its side effects may still land. Narrow the work (a smaller range, a more specific "
        "pattern), raise the timeout, or check the workspace before assuming nothing happened.]"
    )
    if partial:
        note += f"\n\nPartial output before the deadline:\n{partial}"
    return note


def is_timeout(text: str) -> bool:
    return isinstance(text, str) and text.startswith(TIMEOUT_PREFIX)


# A shared pool for abandoned work. Daemon threads so a wedged tool cannot keep the interpreter alive at exit.
_POOL: ThreadPoolExecutor | None = None
_POOL_LOCK = threading.Lock()
_ABANDONED = 0


def _pool() -> ThreadPoolExecutor:
    global _POOL
    with _POOL_LOCK:
        if _POOL is None:
            _POOL = ThreadPoolExecutor(max_workers=MAX_ABANDONED_WORKERS, thread_name_prefix="tool-deadline")
        return _POOL


def abandoned_count() -> int:
    """How many calls have been abandoned to their deadline. Read by a status line and by tests."""
    return _ABANDONED


@dataclass
class DeadlineOutcome:
    """What `run_with_deadline` produced."""

    result: Any = None
    timed_out: bool = False
    error: BaseException | None = None
    deadline: Deadline | None = None

    @property
    def ok(self) -> bool:
        return not self.timed_out and self.error is None


def run_with_deadline(
    action: Callable[[], Any],
    *,
    seconds: float | None,
    tool: str = "",
    outer_aborted: Callable[[], bool] | None = None,
) -> DeadlineOutcome:
    """Run `action`, abandoning it after `seconds`. Returns an outcome rather than raising.

    `seconds=None` runs inline -- a passthrough, not a default. That matters: inventing a budget for a tool that
    declared none would silently change the behaviour of every existing tool.

    `outer_aborted` is the misattribution guard. When the wait ends and this callable reports that an outside
    cause already aborted -- a user cancel, an upstream deadline -- the outcome is NOT marked as a tool timeout,
    because reporting a timeout for a cancellation sends the reader looking in the wrong place.
    """
    global _ABANDONED
    if action is None:
        return DeadlineOutcome(result=None)

    if not seconds or seconds <= 0:
        try:
            return DeadlineOutcome(result=action())
        except BaseException as exc:  # noqa: BLE001
            return DeadlineOutcome(error=exc)

    deadline = Deadline(seconds=float(seconds), tool=tool)
    future = _pool().submit(action)
    try:
        result = future.result(timeout=deadline.seconds)
        return DeadlineOutcome(result=result, deadline=deadline)
    except TimeoutError:
        # `concurrent.futures` raises `TimeoutError` (builtin, aliased) from `.result(timeout=...)`.
        pass
    except BaseException as exc:  # noqa: BLE001
        # A genuine failure inside the tool is reported as itself, never as a timeout.
        return DeadlineOutcome(error=exc, deadline=deadline)

    # The wait ended. Was it OUR deadline, or did something outside abort first?
    if outer_aborted is not None:
        try:
            if outer_aborted():
                return DeadlineOutcome(
                    timed_out=False, error=None, deadline=deadline, result=None
                )
        except Exception:
            # An unreadable abort flag falls through to the timeout verdict: the deadline did elapse, and
            # claiming an outside cause we could not confirm would be the misattribution this guards against.
            pass

    deadline.fired = True
    _ABANDONED += 1
    return DeadlineOutcome(result=timeout_result(deadline), timed_out=True, deadline=deadline)


def declared_timeout(tool_def: dict, tool_name: str = "") -> float | None:
    """A tool's declared budget, from its own schema or a per-tool table.

    Read from the declaration rather than centralised so a tool's budget lives next to the tool, and so adding a
    tool does not mean editing a list somewhere else. Both shapes are accepted because the providers use both.
    """
    if not isinstance(tool_def, dict):
        return None
    for candidate in (tool_def, tool_def.get("function"), (tool_def.get("input_schema") or {})):
        if not isinstance(candidate, dict):
            continue
        for key in ("timeout_s", "timeout_seconds", "timeoutMs", "timeout_ms"):
            value = candidate.get(key)
            if isinstance(value, (int, float)) and value > 0:
                # Milliseconds are converted, because `timeoutMs` is the name their tool schema uses and a
                # silent unit mismatch would turn a 30-second budget into 30 milliseconds.
                return float(value) / 1000.0 if "ms" in key.lower() or key.endswith("Ms") else float(value)
    return None


# Tools that must never be given an automatic deadline, with the reason. A deadline on these would break the
# feature rather than the tool: each one legitimately runs until a condition the harness cannot predict.
NO_DEADLINE = {
    "ask_user_question": "waits for a person, which has no useful budget",
    "task": "a sub-agent's duration is its own; the harness bounds it separately",
    "parallel_tasks": "same as task, multiplied",
    "send_to_subagent": "same as task",
    "schedule_prompt": "schedules future work, does not wait for it",
}
