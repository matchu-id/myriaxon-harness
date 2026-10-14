"""Run state and the trajectory recorder.

The recorder is the single source of truth for every behavioral metric in IQB-14. It is fed from
exactly two places, deliberately:

* `tool_registry.dispatch_tool` -- one entry per tool call, with arguments, result, duration and
  the token delta attributable to it (timing and token counters are read at call boundaries).
* the runner, at turn boundaries -- user prompt, final assistant text, usage totals, wall clock.

Nothing is inferred from stdout, and nothing is parsed back out of a log file. Earlier eval
scripts in this repo parsed `[PASS]`/`[FAIL]` lines out of captured stdout with a regex, which
meant a crash produced no results at all and a timeout could be indistinguishable from a pass.
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable


@dataclass
class TraceEvent:
    """One tool call, as actually executed."""

    index: int
    tool: str
    args: dict
    result: str
    is_error: bool
    duration_ms: float
    input_tokens_delta: int = 0
    output_tokens_delta: int = 0
    # Which attempt/turn this belongs to -- a task with N attempts and M prompts per attempt
    # produces one flat trace, and these two fields are what make it sliceable.
    attempt: int = 0
    turn: int = 0
    phase: str = "main"

    @property
    def args_json(self) -> str:
        try:
            return json.dumps(self.args, default=str, sort_keys=True)
        except TypeError:
            return str(self.args)

    @property
    def signature(self) -> str:
        """Stable identity of the call for loop/repeat detection. Deliberately excludes the
        result so a retry of the identical call is counted as a repeat."""
        return f"{self.tool}\u0000{self.args_json}"


@dataclass
class TurnRecord:
    attempt: int
    turn: int
    phase: str
    prompt: str
    final_text: str
    tool_calls: int
    duration_ms: float
    input_tokens: int
    output_tokens: int
    error: str | None = None


@dataclass
class RunState:
    """Everything measurable about one instance (task x attempt)."""

    task_id: str
    attempt: int
    events: list[TraceEvent] = field(default_factory=list)
    turns: list[TurnRecord] = field(default_factory=list)
    # Terminal status of the instance, always one of STATUSES below.
    status: str = "pending"
    detail: str = ""
    started_at: float = 0.0
    finished_at: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None
    model: str = ""
    provider: str = ""
    workspace: str = ""
    variants: dict[str, str] = field(default_factory=dict)
    extras: dict[str, Any] = field(default_factory=dict)
    # False when this RunState was rebuilt from an artifact that did not persist turn texts (an
    # older schema). Answer-based metrics (stability, robustness, similarity) MUST refuse to score
    # in that case rather than treating "no answers recorded" as "all answers identical" -- the
    # latter silently invents a perfect score, which is the worst possible failure for a benchmark.
    replies_available: bool = True

    # -- derived ----------------------------------------------------------------
    @property
    def wall_clock_s(self) -> float:
        if not self.finished_at or not self.started_at:
            return 0.0
        return self.finished_at - self.started_at

    @property
    def failed_tool_calls(self) -> list[TraceEvent]:
        return [e for e in self.events if e.is_error]

    @property
    def step_count(self) -> int:
        """Tool calls actually executed. This is what task budgets are measured in."""
        return len(self.events)

    def tool_names(self) -> list[str]:
        return [e.tool for e in self.events]

    def calls_of(self, *names: str) -> list[TraceEvent]:
        wanted = set(names)
        return [e for e in self.events if e.tool in wanted]

    def count_of(self, *names: str) -> int:
        return len(self.calls_of(*names))

    def by_phase(self, phase: str) -> list[TraceEvent]:
        return [e for e in self.events if e.phase == phase]

    def is_completed(self) -> bool:
        return self.status == "completed"

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "attempt": self.attempt,
            "status": self.status,
            "detail": self.detail,
            "model": self.model,
            "provider": self.provider,
            "wall_clock_s": round(self.wall_clock_s, 3),
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cost_usd": self.cost_usd,
            "tool_calls": len(self.events),
            "failed_tool_calls": len(self.failed_tool_calls),
            "turn_count": len(self.turns),
            # Full turn text, not just the count: the reply IS the artifact a verifier scored and a
            # human disputes. Recording only a count made it impossible to re-score a trajectory or
            # to explain a failure after the fact, which is precisely what the stored artifacts
            # exist for.
            "turns": [asdict(t) for t in self.turns],
            "variants": self.variants,
            "extras": self.extras,
            "trace": [asdict(e) for e in self.events],
        }


# Instance outcomes. The whole point of this enum is that the last three are never reported as
# agent failures -- a model cannot be blamed for the harness breaking or the network dropping.
STATUS_COMPLETED = "completed"       # agent ran to a stop condition; verifiers decide pass/fail
STATUS_BUDGET_EXCEEDED = "budget_exceeded"  # agent hit a declared step/time/cost ceiling
STATUS_TIMEOUT = "timeout"           # harness-level wall clock expired
STATUS_AGENT_ERROR = "agent_error"   # provider/API raised (auth, 5xx, rate limit exhausted)
STATUS_HARNESS_ERROR = "harness_error"  # a bug in the benchmark or verifier itself
STATUS_ENV_ERROR = "env_error"       # missing dependency / unwritable workspace / no API key
STATUSES = (
    STATUS_COMPLETED,
    STATUS_BUDGET_EXCEEDED,
    STATUS_TIMEOUT,
    STATUS_AGENT_ERROR,
    STATUS_HARNESS_ERROR,
    STATUS_ENV_ERROR,
)
# Statuses that mean "the agent did not get a fair chance" -- excluded from capability scoring and
# reported separately as harness reliability.
NON_ATTRIBUTABLE = (STATUS_HARNESS_ERROR, STATUS_ENV_ERROR)


class TraceRecorder:
    """Thread-safe sink attached to one agent's tool context.

    Attached via `tool_context.bench_trace = recorder.record`. Keeping it a plain attribute on the
    existing context (rather than threading a new parameter through three agent implementations)
    means all three providers record identically for free.
    """

    def __init__(self, state: RunState, usage_source=None, clock=time.monotonic) -> None:
        self.state = state
        self.usage_source = usage_source
        self._clock = clock
        self._lock = threading.Lock()
        self._pending: dict[int, tuple[float, int, int]] = {}
        self._counter = 0
        self.attempt = 0
        self.turn = 0
        self.phase = "main"

    def _usage(self) -> tuple[int, int]:
        if self.usage_source is None:
            return 0, 0
        totals = getattr(self.usage_source, "usage_totals", None)
        if totals is None:
            return 0, 0
        return int(getattr(totals, "input_tokens", 0) or 0), int(getattr(totals, "output_tokens", 0) or 0)

    def begin(self) -> int:
        """Mark the start of one tool call and capture that call's own token baseline.

        The baseline is per call rather than one running total because the model request that
        precedes a call is what consumes tokens: with a single shared baseline, the first recorded
        call would absorb the entire conversation's growth and every later call would look free.

        Returns the index `record()` will assign.
        """
        with self._lock:
            self._counter += 1
            inp, out = self._usage()
            self._pending[self._counter] = (self._clock(), inp, out)
            return self._counter

    def abort(self, index: int) -> None:
        """Discard a started-but-never-recorded call (a guarded call that was refused before
        dispatch, e.g. a budget rejection). Keeps durations honest instead of leaking a bogus
        timing entry into the next real call."""
        with self._lock:
            self._pending.pop(index, None)

    def record(self, name: str, tool_input: dict, is_error: bool, result: str) -> None:
        """Called by `tool_registry.dispatch_tool` for every tool call."""
        now = self._clock()
        inp, out = self._usage()
        with self._lock:
            index = self._counter
            started, base_in, base_out = self._pending.pop(index, (now, inp, out))
            event = TraceEvent(
                index=index,
                tool=name,
                args=dict(tool_input or {}),
                result=result if isinstance(result, str) else str(result),
                is_error=bool(is_error),
                duration_ms=(now - started) * 1000.0,
                input_tokens_delta=max(0, inp - base_in),
                output_tokens_delta=max(0, out - base_out),
                attempt=self.attempt,
                turn=self.turn,
                phase=self.phase,
            )
            self.state.events.append(event)

    def note_turn(self, prompt: str, final_text: str, duration_ms: float, error: str | None = None) -> None:
        inp, out = self._usage()
        with self._lock:
            self.state.turns.append(
                TurnRecord(
                    attempt=self.attempt,
                    turn=self.turn,
                    phase=self.phase,
                    prompt=prompt,
                    final_text=final_text,
                    tool_calls=sum(1 for e in self.state.events if e.attempt == self.attempt and e.turn == self.turn),
                    duration_ms=duration_ms,
                    input_tokens=inp,
                    output_tokens=out,
                    error=error,
                )
            )

    def step_count(self) -> int:
        return len(self.state.events)

    def write_jsonl(self, path: Path) -> None:
        """Append-only trajectory file. Written incrementally during the run so a crash mid-suite
        still leaves every completed tool call on disk."""
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as fh:
            for event in self.state.events:
                fh.write(json.dumps(asdict(event), default=str) + "\n")


def tool_sequence(events: Iterable[TraceEvent]) -> list[str]:
    return [e.tool for e in events]
