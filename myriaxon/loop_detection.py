"""Loop detection: notice when the agent is repeating itself without making progress.

## The gap this closes

`tool_registry._check_tool_loop` refuses a call whose `(name, arguments)` has been seen
`MAX_REPEATED_TOOL_CALLS` times in a row. That catches the crudest case and misses three important ones:

* **the same call producing a different result each time.** A poll that returns new data every second is not a
  loop, and a call that keeps failing identically *is* one — argument-identity cannot tell them apart.
* **different arguments chasing the same dead end.** `read_file("a.py")`, `read_file("b.py")`,
  `read_file("a.py")` has no three-in-a-row repeat and is still stuck.
* **a loop that survives compaction.** Compaction rewrites the transcript, which resets any counter kept over
  messages, and the agent resumes exactly the loop it was in.

OpenClaw's `src/agents/tool-loop-detection.ts` handles all three, and the design has two ideas worth copying
exactly:

**Score the `(tool, args, result)` triple, not the call.** Their `hashToolCall` hashes stable-stringified params,
and the outcome digest normalises volatile fields out — durations, PIDs, session ids, cwd, timestamps, retry
counters — while keeping the payload text, the status, and the real error. Their reason, quoted: *"so a new
failure cause resets the streak."* Without normalisation every retry looks identical because the elapsed time
changed; with it, two failures are only "the same" if they failed the same way.

**Arm a targeted guard at the moment of the fix.** Their post-compaction guard snapshots the pre-compaction call
tail as a baseline and aborts on three identical triples afterwards, with the reasoning that *"repeated args alone
can be legitimate polling; identical results after compaction prove the compression did not change the loop."*
And it is **enabled unless explicitly disabled**, because *"the guard exists to escape compaction loops that would
otherwise burn unbounded tokens, so a no-config user still gets the protection."*

## What is dropped

Their history is keyed by run id so heartbeat cycles do not inherit stale counts, and there are six detector
kinds including `ping_pong` (A→B→A→B) and a global circuit breaker. Four kinds are implemented here — repeat,
argument churn, no-progress poll, and the post-compaction guard — because those are the ones that fire on real
work. `ping_pong` and the breaker are noted as absent rather than half-built.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any

# Tiers, from their `tool-loop-detection.ts:47-50` and `tool-loop-thresholds.ts:1`.
HISTORY_SIZE = 30
WARNING_THRESHOLD = 10
CRITICAL_THRESHOLD = 20
GLOBAL_BREAKER_THRESHOLD = 30
UNKNOWN_TOOL_THRESHOLD = 10

# How many times the SAME command may fail the SAME way before the turn is stopped.
#
# Set to 2, and the number is the point. Every threshold above counts REPEATS, which is the right question
# for a poll that keeps returning the same data: ten identical reads of an unchanged file is wasteful but
# harmless, and a lower bar would interrupt legitimate waiting. A REPEATED FAILURE is a different event --
# the call did not do what it was for, and doing it again cannot change that unless something changed in
# between, which a second identical attempt is precisely the way to find out.
#
# Measured, and this is why the constant exists at all: a user asked a simple question, the agent ran
# `Get-Content pyproject.toml`, that call BLOCKED FOR 120 SECONDS and failed, and the agent then retried
# the same read. The loop detector reported nothing, because it needs ten repeats before it speaks:
#
#     call 1: verdict = None
#     call 2: verdict = None
#     ...
#     call 10: Detection(kind='repeat', severity='warning')
#
# Ten identical *results* is a warning. Two identical *failures* is two minutes of a user's life and the
# same mistake twice. The thresholds above were ported from another harness whose tool set and timeouts
# differ; this one is set from what this harness actually does.
FAILURE_BREAKER_THRESHOLD = 2

# The post-compaction guard's windows.
POST_COMPACTION_WINDOW = 3
POST_COMPACTION_BASELINE = 16

# Outcomes that always look identical whatever else changes, so a repeat is meaningful immediately.
_TERMINAL_FAILURE_MARKERS = (
    "traceback (most recent call last)",
    "permission denied",
    "no such file or directory",
    "command not found",
    "connection refused",
    "authentication failed",
    "invalid api key",
    # A timeout is terminal in the sense that matters here: the next attempt starts from the same state and
    # will block for the same duration. Added after a measured failure -- a single `Get-Content` blocked for
    # the full 120-second default before giving up, which is long enough that a retry costs the user
    # another two minutes. Without this marker the two attempts digest differently (the elapsed time is in
    # the text) and the repeated-failure breaker cannot see them as the same failure.
    "timed out after",
    "did not finish",
)

# Fields whose value changes between otherwise identical calls, and which therefore must not contribute to the
# outcome digest. Their list is longer; these are the ones Myriaxon's tools actually emit.
_VOLATILE_KEYS = frozenset({
    "duration", "duration_ms", "elapsed", "elapsed_ms", "took_ms", "runtime_ms",
    "pid", "ppid", "session_id", "sessionid", "cwd", "timestamp", "ts", "time", "at",
    "attempt", "attempts", "retry", "retries", "started_at", "finished_at", "ended_at",
    "request_id", "requestid", "trace_id", "traceparent", "nonce", "id",
})

# Numbers that are almost always incidental: a duration, a byte count, an elapsed clock.
_NUMERIC_NOISE = re.compile(r"\b\d+(?:\.\d+)?\s*(?:ms|s|sec|secs|seconds|bytes|B|KB|MB)\b", re.IGNORECASE)
_ISO_TIME = re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?\b")
_HEX_ID = re.compile(r"\b[0-9a-f]{16,}\b", re.IGNORECASE)


def normalise_outcome(result: Any) -> str:
    """A stable digest of what a call *did*, with incidental detail stripped.

    Two identical failures must hash the same, and two genuinely different failures must not. Stripping the
    volatile fields is what makes both true -- without it, every retry differs by its elapsed time and the
    detector never fires.
    """
    text = result if isinstance(result, str) else json.dumps(result, sort_keys=True, default=str, ensure_ascii=False)
    text = _ISO_TIME.sub("<time>", text)
    text = _NUMERIC_NOISE.sub("<n>", text)
    text = _HEX_ID.sub("<id>", text)

    # Parse a JSON object and drop volatile keys before hashing, so their values cannot affect the digest.
    stripped = text.strip()
    if stripped.startswith("{"):
        try:
            parsed = json.loads(stripped)
            if isinstance(parsed, dict):
                parsed = {k: v for k, v in parsed.items() if k.lower() not in _VOLATILE_KEYS}
                text = json.dumps(parsed, sort_keys=True, ensure_ascii=False)
        except json.JSONDecodeError:
            pass

    lowered = text.lower()
    for marker in _TERMINAL_FAILURE_MARKERS:
        if marker in lowered:
            # A terminal failure marker REPLACES the text rather than prefixing it, and the difference is
            # load-bearing.
            #
            # It used to be `text = f"<fail:{marker}> " + text`, which keeps the original wording in the
            # digest. Two timeouts therefore hashed differently -- one said "timed out after 120s and was
            # killed", the next added a sentence of guidance -- so the repeated-failure breaker, whose whole
            # job is to spot the SAME failure twice, could not see them as the same and never fired.
            # Measured: attempt 1 -> None, attempt 2 -> None, with the breaker in place and working.
            #
            # Replacing the text with the marker alone is correct because the marker IS the cause: for a
            # given tool and given arguments, "permission denied" and "no such file or directory" are
            # different failures (different digests) and "permission denied" twice is one failure twice
            # (same digest). The surrounding prose never carried information about the cause -- it carried
            # information about what the harness said next, which is exactly what must not affect identity.
            return hashlib.sha256(f"<fail:{marker}>".encode("utf-8")).hexdigest()[:16]

    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def call_fingerprint(tool_name: str, arguments: Any) -> str:
    """The identity of a call: name plus canonical arguments.

    `exec` titles and other presentation fields are excluded by hashing the arguments as given rather than any
    rendered form -- their reasoning: *"Execution titles describe presentation, not a different command."*
    """
    canonical = json.dumps(arguments, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(f"{tool_name}\x1f{canonical}".encode("utf-8")).hexdigest()[:16]


def triple_fingerprint(tool_name: str, arguments: Any, result: Any) -> str:
    """The identity of a call *and its outcome*. This is what the detectors count."""
    return f"{call_fingerprint(tool_name, arguments)}:{normalise_outcome(result)}"


@dataclass
class CallRecord:
    tool: str
    call: str
    outcome: str
    triple: str
    at: float
    failed: bool = False


@dataclass
class Detection:
    """One verdict from the detectors."""

    kind: str
    severity: str          # "warning" | "critical"
    tool: str
    count: int
    message: str
    detail: str = ""

    def render(self) -> str:
        return f"[{self.severity}] {self.kind}: {self.message}"


class LoopDetector:
    """Tracks recent calls for one run and reports repeats.

    One instance per run: their history is keyed by run id *"so scheduled heartbeat cycles and fresh runs do not
    inherit stale loop counts from earlier runs."* A shared detector would carry a count across unrelated work
    and fire on a coincidence.
    """

    def __init__(self, enabled: bool = True, run_id: str = "") -> None:
        self.enabled = enabled
        self.run_id = run_id
        self.history: list[CallRecord] = []
        self._lock = threading.RLock()
        # Post-compaction guard state.
        self._baseline: list[str] = []
        self._armed = False
        self.warnings_issued = 0
        self.criticals_issued = 0

    # ---- recording

    def record(
        self,
        tool_name: str,
        arguments: Any,
        result: Any,
        *,
        failed: bool = False,
        at: float | None = None,
    ) -> Detection | None:
        """Record one completed call and return a detection when something looks stuck.

        Returns `None` for the ordinary case, which is nearly every call.
        """
        if not self.enabled:
            return None
        stamp = time.time() if at is None else at
        record = CallRecord(
            tool=tool_name,
            call=call_fingerprint(tool_name, arguments),
            outcome=normalise_outcome(result),
            triple=triple_fingerprint(tool_name, arguments, result),
            at=stamp,
            failed=failed,
        )
        with self._lock:
            self.history.append(record)
            if len(self.history) > HISTORY_SIZE:
                self.history = self.history[-HISTORY_SIZE:]
            return self._evaluate(record)

    def _check_repeated_failure(self, latest: CallRecord) -> Detection | None:
        """Stop the turn when the same call fails the same way twice.

        The severity is `critical` on purpose. Every other verdict here is advice -- "consider whether the
        approach is working" -- which is right for a loop that is merely wasteful. A repeated failure is
        not wasteful, it is broken, and the harness should say so in a way that ends the turn rather than
        in a way the model can read past and try a third time.

        Only IDENTICAL failures count. A second attempt that fails differently is progress: the error
        changed, so the model learned something, and interrupting that would be its own mistake.
        """
        if not latest.failed:
            return None
        same_failure = [
            r for r in self.history
            if r.failed and r.call == latest.call and r.outcome == latest.outcome
        ]
        if len(same_failure) < FAILURE_BREAKER_THRESHOLD:
            return None
        self.criticals_issued += 1
        return Detection(
            kind="repeated-failure",
            severity="critical",
            tool=latest.tool,
            count=len(same_failure),
            message=(
                f"{latest.tool} has now failed the same way {len(same_failure)} times. Retrying an "
                "identical call that already failed will fail again -- the command is wrong, the path is "
                "wrong, or the tool is unavailable, and none of those change by asking twice. Stop and "
                "report what failed, or take a different approach."
            ),
            detail=latest.call,
        )

    def _evaluate(self, latest: CallRecord) -> Detection | None:
        # Checked FIRST, ahead of the post-compaction guard and every repeat counter, because it answers a
        # different and more urgent question. Every other check asks "is this going nowhere"; this one asks
        # "has this already failed twice the same way", and the answer to that should stop the turn before
        # any other verdict gets a chance to describe it more gently.
        detection = self._check_repeated_failure(latest)
        if detection is not None:
            return detection

        # Order matters. The post-compaction guard is checked first: it is the narrowest and the one whose
        # evidence is strongest, so its verdict should win over a coincidental count.
        detection = self._check_post_compaction(latest)
        if detection is not None:
            return detection

        # 1. Same call, same outcome. The unambiguous loop.
        identical = [r for r in self.history if r.triple == latest.triple]
        if len(identical) >= CRITICAL_THRESHOLD:
            self.criticals_issued += 1
            return Detection(
                kind="repeat",
                severity="critical",
                tool=latest.tool,
                count=len(identical),
                message=(
                    f"{latest.tool} has produced an identical result {len(identical)} times in this run. "
                    "That is not progress; stop and change approach."
                ),
                detail=latest.call,
            )
        if len(identical) >= WARNING_THRESHOLD:
            self.warnings_issued += 1
            return Detection(
                kind="repeat",
                severity="warning",
                tool=latest.tool,
                count=len(identical),
                message=(
                    f"{latest.tool} has produced an identical result {len(identical)} times. "
                    "Consider whether the approach is working."
                ),
                detail=latest.call,
            )

        # 2. Same call, *different* outcome, many times: a poll that is returning new data each time is fine.
        #    A poll returning the same shape with nothing changing is not, and that is what the identical check
        #    above already covers. This branch catches the case where the outcome varies only in volatile fields
        #    that survived normalisation, so the counts differ.
        same_call = [r for r in self.history if r.call == latest.call]
        distinct_outcomes = {r.outcome for r in same_call}
        if len(same_call) >= CRITICAL_THRESHOLD and len(distinct_outcomes) <= 2:
            self.criticals_issued += 1
            return Detection(
                kind="no-progress-poll",
                severity="critical",
                tool=latest.tool,
                count=len(same_call),
                message=(
                    f"{latest.tool} has been called {len(same_call)} times with the same arguments and only "
                    f"{len(distinct_outcomes)} distinct outcome(s). This looks like a poll that is not changing."
                ),
            )

        # 3. Argument churn: the same tool called repeatedly with *different* arguments and the same outcome
        #    each time, which is a search that keeps failing for the same reason.
        same_tool = [r for r in self.history if r.tool == latest.tool]
        distinct_calls = {r.call for r in same_tool}
        if len(same_tool) >= WARNING_THRESHOLD and len(distinct_calls) >= WARNING_THRESHOLD:
            outcomes = {r.outcome for r in same_tool}
            if len(outcomes) <= 2 and latest.failed:
                self.warnings_issued += 1
                return Detection(
                    kind="argument-churn",
                    severity="warning",
                    tool=latest.tool,
                    count=len(same_tool),
                    message=(
                        f"{latest.tool} has failed {len(same_tool)} times with {len(distinct_calls)} different "
                        "argument sets and the same outcome. The arguments are not the problem."
                    ),
                )

        # 4. An unknown tool retried: a name that does not resolve will not start resolving.
        if latest.failed and "unknown tool" in str(latest.outcome):
            unknown = [r for r in self.history if r.tool == latest.tool and r.failed]
            if len(unknown) >= UNKNOWN_TOOL_THRESHOLD:
                return Detection(
                    kind="unknown-tool-repeat",
                    severity="critical",
                    tool=latest.tool,
                    count=len(unknown),
                    message=f"{latest.tool!r} has failed as unknown {len(unknown)} times.",
                )

        return None

    # ---- the post-compaction guard

    def arm_after_compaction(self) -> list[str]:
        """Snapshot the call tail as a baseline, and start watching. Returns the baseline.

        Called immediately after a compaction. Their comment for why this is a separate guard rather than part of
        the main detector: compaction rewrites the transcript, so any loop that was in progress resumes with its
        history apparently erased. Snapshotting the tail gives the guard something to compare against that the
        compaction cannot touch.
        """
        with self._lock:
            self._baseline = [r.triple for r in self.history[-POST_COMPACTION_BASELINE:]]
            self._armed = True
            return list(self._baseline)

    def _check_post_compaction(self, latest: CallRecord) -> Detection | None:
        """Abort when the same triple appears POST_COMPACTION_WINDOW times after a compaction.

        Three rather than ten: after a compaction there is no benign explanation for an identical repeat, and the
        cost of being wrong is a wasted run rather than a lost one.
        """
        if not self._armed:
            return None
        recent = [r.triple for r in self.history[-POST_COMPACTION_WINDOW:]]
        if len(recent) < POST_COMPACTION_WINDOW:
            return None
        if len(set(recent)) != 1:
            return None
        triple = recent[0]
        # Only a triple the baseline also contains proves the compaction did not change the loop. A *new* triple
        # repeated three times after a compaction is a fresh loop, which detector 1 will catch at its own tier.
        if triple not in self._baseline:
            return None
        self._armed = False
        self.criticals_issued += 1
        return Detection(
            kind="compaction-loop-persisted",
            severity="critical",
            tool=latest.tool,
            count=POST_COMPACTION_WINDOW,
            message=(
                "the same call and result have now repeated after a compaction that included them, so "
                "compressing the history did not break the loop. Stopping instead of spending more tokens."
            ),
            detail=latest.call,
        )


# --------------------------------------------------------------------------- module-level, per run

_detector: LoopDetector | None = None
_lock = threading.RLock()


def set_detector(detector: LoopDetector | None) -> None:
    global _detector
    with _lock:
        _detector = detector


def get_detector() -> LoopDetector | None:
    with _lock:
        return _detector


def record(tool_name: str, arguments: Any, result: Any, *, failed: bool = False) -> Detection | None:
    """Record against the active detector, if there is one. Safe to call unconditionally."""
    detector = get_detector()
    if detector is None:
        return None
    return detector.record(tool_name, arguments, result, failed=failed)


def arm_after_compaction() -> list[str]:
    """Arm the post-compaction guard on the active detector. Called by each agent after it compacts."""
    detector = get_detector()
    if detector is None:
        return []
    return detector.arm_after_compaction()
