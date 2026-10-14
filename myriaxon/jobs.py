"""Background jobs: wait for one, promote it, and learn when it finishes without polling.

## The gap

Myriaxon could **start** background work — `run_bash(run_in_background=True)` puts a `_BackgroundProcess` in
`ctx.background_procs`, and `/shells` lists them — and that was all. There was no `wait`, no `promote`, and no
completion notification, so the only way to find out whether a long command had finished was to poll it by hand.

Opencode's `packages/core/src/background-job.ts` provides exactly that missing layer: `list`, `get`, `start`,
`extend`, `wait`, `promote`, `cancel`. This is that layer over the processes Myriaxon already tracks.

## Why a wrapper rather than a rewrite

`_BackgroundProcess` already does the hard part correctly and for a non-obvious reason: a daemon thread drains
`proc.stdout` continuously, because **a pipe that is never read fills and blocks the child**. Rewriting the process
handling would risk reintroducing that. So this module adds the service semantics — waiting, promoting, and a
one-shot completion notification — on top of the existing object.

## The three decisions that matter

**Notification is once, exactly once.** A job that reported its completion on every subsequent check would nag the
model forever, and the model would learn to ignore the runtime-context block — the same training-a-reader-to-ignore
failure the diagnostics module was designed around. `notified` is set when the report is produced, and the report is
never produced twice.

**Waiting is bounded.** `wait()` takes a timeout and returns `(finished, output)`. Blocking forever inside a tool
call would defeat the run budget above it: `run_budget.check()` runs at the step boundary, so a tool that never
returns means a budget that is never consulted.

**Promotion is not killing.** `promote` brings a job's result into the foreground — it waits for completion and
returns everything the job produced. Opencode's `promote` also moves a job out of its background scope; here the job
stays in the registry, because removing it would lose the record an operator may still want. `cancel` is the
separate operation that stops it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from .constants import (
    BACKGROUND_JOB_KEEP,
    BACKGROUND_JOB_OUTPUT_TAIL_CHARS,
    DEFAULT_BACKGROUND_WAIT_SECONDS,
    MAX_BACKGROUND_JOBS,
)

# Job states, closed so a caller branches on them rather than on prose.
RUNNING = "running"
EXITED = "exited"
FAILED = "failed"
KILLED = "killed"
STATES = (RUNNING, EXITED, FAILED, KILLED)


@dataclass
class JobView:
    """A job as a caller sees it. A snapshot: the underlying process keeps moving."""

    job_id: str
    command: str
    state: str
    exit_code: int | None
    elapsed: float
    # Whether this job's completion has already been reported, so a caller can tell "finished quietly" from
    # "finished and you were told".
    notified: bool = False

    @property
    def finished(self) -> bool:
        return self.state != RUNNING

    def render(self) -> str:
        code = "" if self.exit_code is None else f" (exit {self.exit_code})"
        return f"{self.job_id}: {self.state}{code} after {self.elapsed:.0f}s -- {self.command[:70]}"


def _state_of(process, terminated: bool) -> str:
    """Derive the state from the process object.

    `terminated` is checked first: a killed process also has an exit code, and reporting it as `exited` would hide
    the fact that its work was cut short — which is exactly the case a caller needs to distinguish.
    """
    if terminated:
        return KILLED
    code = getattr(process, "exit_code", None)
    if code is None:
        return RUNNING
    return EXITED if code == 0 else FAILED


def _started_at(process) -> float:
    """When the job started, as best the process object can say.

    Read from the process where possible rather than recorded here, because `_run_bash` constructs the object and
    this module must not require a change to it just to answer "how long has this been running".
    """
    started = getattr(process, "_myriaxon_started_at", None)
    if isinstance(started, (int, float)) and started:
        return float(started)
    return 0.0


def view(job_id: str, process) -> JobView:
    """One job as a snapshot."""
    started = _started_at(process)
    return JobView(
        job_id=job_id,
        command=str(getattr(process, "command", "")),
        state=_state_of(process, bool(getattr(process, "terminated", False))),
        exit_code=getattr(process, "exit_code", None),
        elapsed=(time.time() - started) if started else 0.0,
        notified=bool(getattr(process, "notified", False)),
    )


def all_jobs(ctx) -> list[JobView]:
    """Every tracked job, oldest first.

    Sorted by the counter in the id rather than by dict order, because a dict's order is insertion order only by
    accident of implementation and a listing that reshuffles between calls is hard to read.
    """
    procs = getattr(ctx, "background_procs", None)
    if not isinstance(procs, dict):
        return []

    def order(item: tuple[str, object]) -> tuple[int, str]:
        name = item[0]
        digits = "".join(ch for ch in name if ch.isdigit())
        return (int(digits) if digits else 0, name)

    return [view(name, proc) for name, proc in sorted(procs.items(), key=order)]


def get(ctx, job_id: str):
    """The raw process for an id, or None."""
    procs = getattr(ctx, "background_procs", None)
    if not isinstance(procs, dict):
        return None
    return procs.get(job_id)


def wait(ctx, job_id: str, timeout: float = DEFAULT_BACKGROUND_WAIT_SECONDS) -> tuple[bool, str]:
    """Wait for a job to finish, up to `timeout` seconds. Returns `(finished, output_tail)`.

    Polls rather than joining the drain thread, because the drain thread belongs to `_BackgroundProcess` and joining
    it from a tool call would block a thread that is not this caller's to join. A 50 ms interval is well below the
    granularity of anything a model asks about, and the loop always exits on the timeout.
    """
    process = get(ctx, job_id)
    if process is None:
        return False, f"no background process with id {job_id!r}"

    deadline = time.time() + max(0.0, timeout)
    while time.time() < deadline:
        if _state_of(process, bool(getattr(process, "terminated", False))) != RUNNING:
            return True, output_tail(process)
        time.sleep(0.05)
    # Timed out. Report the state honestly rather than implying it finished.
    still = _state_of(process, bool(getattr(process, "terminated", False)))
    return False, (
        f"{job_id} is still {still} after {timeout:.0f}s. It was NOT waited for any longer: blocking a tool call "
        "indefinitely would defeat the run budget, which is only checked at the step boundary. Poll it with "
        "bash_output, or wait again with a longer timeout."
    )


def promote(ctx, job_id: str, timeout: float = DEFAULT_BACKGROUND_WAIT_SECONDS) -> tuple[bool, str]:
    """Bring a job's result into the foreground.

    Waits for completion and returns everything it produced, so the caller has the result inline rather than having
    to poll. The job is deliberately **kept** in the registry afterwards, unlike Opencode's promotion which moves it
    out of its background scope: the record is what an operator reads later, and discarding it to save a dictionary
    entry would be a poor trade.
    """
    process = get(ctx, job_id)
    if process is None:
        return False, f"no background process with id {job_id!r}"
    finished, output = wait(ctx, job_id, timeout)
    if not finished:
        return False, output
    view_ = view(job_id, process)
    header = f"[{view_.job_id} {view_.state}" + (
        f" exit {view_.exit_code}]" if view_.exit_code is not None else "]"
    )
    return True, f"{header}\n{output}"


def output_tail(process, limit: int = BACKGROUND_JOB_OUTPUT_TAIL_CHARS) -> str:
    """Everything the job has produced, bounded. **Non-destructive.**

    ## The bug this signature exists to prevent

    The first version called `process.poll_new_output()`, which ADVANCES a read offset — it is the incremental
    accessor that `bash_output` uses to return only the lines added since the last check. Measured consequence: the
    first `output_tail` returned the job's output and the second returned `''`, so a `wait` after a `promote` (or
    two notifications about one job) silently lost the result. The verifier caught it because it read the same job
    twice.

    So this reads the accumulator directly rather than through the incremental accessor. The lock is the process's
    own, because the drain thread appends to that list from another thread.

    Bounded because a chatty job — a build, a server log — would otherwise put megabytes into the transcript, and the
    tail is what a caller needs. The marker says how much was dropped, so a truncated log cannot be read as a
    complete one.
    """
    lines = getattr(process, "_lines", None)
    if not isinstance(lines, list):
        # An object that is not a `_BackgroundProcess`. Fall back to the incremental accessor and accept that it
        # consumes, rather than raising inside a tool dispatch.
        try:
            return process.poll_new_output()
        except Exception:  # noqa: BLE001
            return ""
    lock = getattr(process, "_lock", None)
    if lock is not None:
        with lock:
            text = "".join(lines)
    else:
        text = "".join(lines)
    if len(text) <= limit:
        return text
    dropped = len(text) - limit
    return f"... [{dropped:,} earlier characters omitted]\n" + text[-limit:]


def pending_notifications(ctx) -> list[str]:
    """Completion reports for jobs that finished and have not been reported yet. Marks them reported.

    **Exactly once**, and that is the point. A job that re-reported on every subsequent check would nag the model
    until it learned to ignore the runtime-context block — the same training-a-reader-to-ignore failure the
    diagnostics module was designed around.

    A **killed** job is reported too, and reported as killed rather than as finished: work that was cut short is
    precisely the case a caller must not mistake for success.
    """
    reports: list[str] = []
    procs = getattr(ctx, "background_procs", None)
    if not isinstance(procs, dict):
        return reports

    for job_id, process in procs.items():
        if getattr(process, "notified", False):
            continue
        state = _state_of(process, bool(getattr(process, "terminated", False)))
        if state == RUNNING:
            continue
        try:
            process.notified = True
        except Exception:  # noqa: BLE001
            # A process object that refuses the flag would re-report forever, which is worse than not reporting, so
            # skip it rather than risk a nagging loop.
            continue
        code = getattr(process, "exit_code", None)
        tail = output_tail(process, 400).strip()
        line = f"{job_id} {state}" + (f" (exit {code})" if code is not None else "")
        line += f" -- {str(getattr(process, 'command', ''))[:70]}"
        if tail:
            line += f"\n  last output: {tail.splitlines()[-1][:160]}"
        reports.append(line)
    return reports


def enforce_limit(ctx) -> list[str]:
    """Drop the OLDEST finished jobs when the registry is over its cap. Returns what was dropped.

    Only finished jobs are considered. Evicting a *running* process would lose the handle needed to kill or wait for
    it, leaving an orphan the harness can no longer see — worse than an unbounded registry.

    ## The bug the ordering fixes

    The first version popped from `all_jobs(ctx)`, which sorts by job id, and then relied on the cap to stop it. That
    drops whichever job happens to sort first, which after a few kills and restarts is not necessarily a finished
    one — measured as "and NONE of the running ones were" failing, with a running job in the dropped list. Ordering
    by the numeric id (oldest first) and taking only finished jobs makes the intent explicit: the oldest finished
    work goes, and a running job is never a candidate.
    """
    procs = getattr(ctx, "background_procs", None)
    if not isinstance(procs, dict) or len(procs) <= MAX_BACKGROUND_JOBS:
        return []

    def numeric_id(job_view: JobView) -> tuple[int, str]:
        digits = "".join(ch for ch in job_view.job_id if ch.isdigit())
        return (int(digits) if digits else 0, job_view.job_id)

    candidates = sorted((v for v in all_jobs(ctx) if v.finished), key=numeric_id)
    dropped: list[str] = []
    for job_view in candidates:
        if len(procs) <= MAX_BACKGROUND_JOBS:
            break
        # Belt and braces: `candidates` is already finished-only, and this re-checks against the LIVE process so a
        # job that restarted between the listing and the pop cannot be evicted while running.
        live = procs.get(job_view.job_id)
        if live is None:
            continue
        if _state_of(live, bool(getattr(live, "terminated", False))) == RUNNING:
            continue
        procs.pop(job_view.job_id, None)
        dropped.append(job_view.job_id)
    return dropped


def keep_bounded(ctx) -> str:
    """Apply the cap and describe the outcome, for a tool result or a status line."""
    dropped = enforce_limit(ctx)
    if not dropped:
        return ""
    return (
        f"dropped {len(dropped)} finished job(s) from the registry to stay under {MAX_BACKGROUND_JOBS}: "
        f"{', '.join(dropped)}. Their output is gone; the registry holds only recent jobs. Running jobs are never "
        "dropped, because losing the handle would leave a process nothing can kill."
    )


def render_list(ctx) -> str:
    """A readable listing, for a tool result.

    **A read, with no side effect.** The first version called `keep_bounded` here, so *looking* at the job list
    silently dropped jobs — and the verifier caught it as "every running job is still tracked" failing after a
    listing. A view that mutates is worse than no view: an operator who lists the jobs to decide what to wait for
    would change what is there to wait for. The cap is applied by the caller that owns the decision, in
    `pending_notifications`' path, not by a renderer.
    """
    jobs = all_jobs(ctx)
    if not jobs:
        return (
            "No background jobs. Start one with run_bash(run_in_background=true); its id can then be waited for or "
            "promoted."
        )
    lines = [f"{len(jobs)} background job(s):"]
    for job in jobs:
        lines.append(f"  {job.render()}")
    running = sum(1 for j in jobs if not j.finished)
    if running:
        lines.append(f"  ({running} still running)")
    return "\n".join(lines)


def summary(ctx) -> dict:
    """Counts by state, for `session_status` and `doctor`."""
    jobs = all_jobs(ctx)
    counts = {state: 0 for state in STATES}
    for job in jobs:
        counts[job.state] = counts.get(job.state, 0) + 1
    counts["total"] = len(jobs)
    return counts
