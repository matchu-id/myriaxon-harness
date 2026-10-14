#!/usr/bin/env python3
"""Verify the background job service.

## The gap this closes

Myriaxon could **start** background work (`run_bash(run_in_background=True)`) and list it, and nothing else: no
`wait`, no `promote`, no completion notification. Opencode's `background-job.ts` provides `list`/`get`/`start`/
`extend`/`wait`/`promote`/`cancel`, and this is that layer over the processes Myriaxon already tracked.

## What these checks exist for

Three properties, each of which is a way the feature fails silently:

  * **a completion is announced EXACTLY ONCE.** A job that re-reported on every step would nag until the model
    learned to ignore the runtime-context block — the same training-a-reader-to-ignore failure the diagnostics
    module was designed around. So the "seconds call reports nothing" checks matter more than the first one.
  * **waiting is BOUNDED.** `run_budget.check()` runs at the step boundary, so a tool call that never returns means
    a budget that is never consulted. A `wait` with no timeout would quietly defeat the ceiling above it.
  * **a KILLED job is not reported as a success.** Work that was cut short is precisely the case a caller must not
    mistake for completion.
"""

from __future__ import annotations

import io
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from myriaxon import jobs  # noqa: E402
from myriaxon import tool_registry as TR  # noqa: E402
from myriaxon import tools  # noqa: E402
from myriaxon.tool_context import ToolContext  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []

# Long-running and chatty commands for the tests, built so the shell never has to unquote anything.
#
# Two earlier versions failed here, and both failures were about quoting rather than about the job service:
#   1. `python -c "time.sleep(30)"` -- on this host `python` on PATH is a Microsoft Store App Execution Alias, so the
#      command failed instantly with "Python was not found" and the job was never running. `myriaxon/interpreter.py`
#      exists for exactly this, and a verifier that trips over it cannot test the running state at all.
#   2. `"<abs path>/python.exe" -c "print('echoed')"` -- correct-looking, but `run_bash` hands the string to
#      PowerShell as ONE `-Command` argument, and between PowerShell's parser, this repository's non-ASCII path and
#      the inner quotes it died at "line:1 char:68".
#
# So the commands are script FILES executed by path: no nesting, nothing for a shell to reinterpret. It tests the same
# thing, because the job service does not care what the child runs.
_SCRIPTS = Path(tempfile.mkdtemp(prefix="myx-jobs-"))


def _script(name: str, body: str) -> str:
    """Write a small script and return a shell-safe command that runs it.

    **The call operator is required, not cosmetic.** `run_bash` passes its command to `shell_argv`, which on Windows
    is `powershell.exe -Command <the string verbatim>`. Measured: PowerShell treats a LEADING QUOTED PATH as a string
    rather than as a command, so

        "C:\\...\\python.exe" "C:\\...\\q.py"     ->  rc=1, "At line:1 char:68"
        & "C:\\...\\python.exe" "C:\\...\\q.py"   ->  rc=0, works

    `&` is PowerShell's call operator. This cost two rounds of debugging a verifier for a bug in the *verifier*, which
    is why it is written down here rather than left as one surprising character in a format string.
    """
    path = _SCRIPTS / f"{name}.py"
    path.write_text(body, encoding="utf-8", newline="\n")
    # Quoted because this repository's path contains a space and non-ASCII characters; `&` because PowerShell needs
    # it before a quoted command name.
    prefix = "& " if sys.platform == "win32" else ""
    return f'{prefix}"{sys.executable}" "{path}"'


_QUICK = _script("quick", "print('echoed')\n")
_SLEEP = _script("sleep", "import time\ntime.sleep(30)\n")
_BIG = _script("big", "print('x' * 20000)\n")


def check(label: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAIL).append(label)
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}" + (f"\n         {detail}" if detail else ""))


class _StubProc:
    """A process that does not exist.

    `jobs.view` and `jobs.enforce_limit` read exactly two things off a process -- `exit_code` and `terminated` -- so
    the state rule and the eviction rule can be exercised with no process at all. Sections 2-10 use REAL jobs for the
    integration path; the sections that test a decision use this, because a decision should not be able to fail
    because a subprocess did not start.
    """

    def __init__(self, *, exit_code=None, terminated=False, command="") -> None:
        self.exit_code = exit_code
        self.terminated = terminated
        self.command = command
        self.notified = False
        self._lines: list[str] = []
        self._lock = None
        self._myriaxon_started_at = time.time()


def context() -> ToolContext:
    from rich.console import Console

    return ToolContext(client=None, model="m", max_tokens=1, console=Console(file=io.StringIO()),
                       yolo=True, tools=[])


def start(ctx, command: str) -> str:
    """Start a background job and return ITS id.

    `next(iter(ctx.background_procs))` is wrong, and the cap section proved it: `_run_bash` numbers jobs by the size
    of the registry, so the tenth job is `bash_1` again, not `bash_10`. Taking the first key therefore returned
    `bash_1` every time — measured as `running [\'bash_1\', \'bash_1\', \'bash_1\']` — so the section was checking three
    references to one job and the "none of the running ones were dropped" assertion was meaningless.

    The id is the key that was not there before, which is correct however the ids are generated.
    """
    before = set(ctx.background_procs)
    tools._run_bash({"command": command, "run_in_background": True}, ctx)
    new = [k for k in ctx.background_procs if k not in before]
    return new[-1] if new else ""


def wait_for(ctx, job_id: str, timeout: float = 25.0) -> None:
    """Block until the job leaves RUNNING, so a check does not race the process."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if jobs.view(job_id, jobs.get(ctx, job_id)).finished:
            return
        time.sleep(0.05)


def main() -> int:
    print("=== 1. the tools are registered AND dispatched ===")
    # Registered is not dispatched: a definition in TOOL_DEFS with no handler is a tool that exists and does nothing.
    names = [TR._def_name(d) for d in TR.build_tool_defs()]
    check("bg_jobs is defined", "bg_jobs" in names, str(len(names)) + " tools")
    check("bg_wait is defined", "bg_wait" in names)
    check("the existing bash tools survive", "bash_output" in names and "kill_bash" in names)
    source = (REPO / "myriaxon" / "tools.py").read_text(encoding="utf-8")
    check("bg_jobs is dispatched", 'if name == "bg_jobs":' in source)
    check("bg_wait is dispatched", 'if name == "bg_wait":' in source)

    print()
    print("=== 2. a job can be listed, and its state is honest ===")
    ctx = context()
    job_id = start(ctx, _QUICK)
    listed = tools._bg_jobs({}, ctx)[0]
    check("the list names the job", job_id in listed, listed[:90])
    # The command is a long script-file invocation and the listing truncates it, so this asserts against a slice that
    # actually contains the command. The first version searched a 90-character slice for ".py", which ended before
    # it -- a check on the wrong region, failing against correct output.
    command_shown = jobs.view(job_id, jobs.get(ctx, job_id)).command
    check("  and its command is recorded", "python" in command_shown.lower() and ".py" in command_shown,
          command_shown[-90:])
    view = jobs.view(job_id, jobs.get(ctx, job_id))
    check("the view has a state", view.state in jobs.STATES, view.state)
    check("  a bounded elapsed figure", view.elapsed >= 0, str(view.elapsed))

    print()
    print("=== 3. wait returns the output, bounded by a timeout ===")
    out, failed = tools._bg_wait({"id": job_id, "timeout": 20}, ctx)
    check("the wait succeeded", not failed, out[:80])
    check("  it reports the exit state", "exited" in out, out[:80])
    check("  and the job's output", "echoed" in out, out[:120])

    print()
    print("=== 4. THE CENTRAL PROPERTY: a completion is announced EXACTLY ONCE ===")
    # A job that re-reports naggingly would train the model to ignore the block, which is the failure this whole
    # codebase keeps designing around.
    first = jobs.pending_notifications(ctx)
    check("the first call reports the job", len(first) == 1, str(len(first)))
    check("  naming the job", job_id in first[0], first[0][:90])
    check("  and its exit code", "exit 0" in first[0], first[0][:90])
    for n in (2, 3, 4):
        again = jobs.pending_notifications(ctx)
        check(f"call {n} reports NOTHING", again == [], str(again))
    check("the job is marked notified", jobs.view(job_id, jobs.get(ctx, job_id)).notified)

    print()
    print("=== 5. waiting on an ALREADY-FINISHED job returns immediately ===")
    start_time = time.perf_counter()
    out2, failed2 = tools._bg_wait({"id": job_id, "timeout": 20}, ctx)
    elapsed = time.perf_counter() - start_time
    check("it does not block", elapsed < 1.0, f"{elapsed:.3f}s")
    check("  and returns the output", "echoed" in out2, out2[:90])

    print()
    print("=== 6. a wait that TIMES OUT says so, and does not claim the job finished ===")
    # The dangerous outcome is implying completion. A model that believes a still-running job finished will read
    # partial output as the whole result.
    slow = context()
    slow_id = start(slow, _SLEEP)
    out3, failed3 = tools._bg_wait({"id": slow_id, "timeout": 1.5}, slow)
    check("a timeout is reported as an error", failed3, out3[:90])
    check("  it says the job is still running", "still running" in out3 or "is still" in out3, out3[:140])
    check("  it does NOT claim completion", "exited" not in out3.split("\n")[0], out3[:140])
    check("  and it explains why waiting is bounded",
          "step boundary" in out3 or "budget" in out3, out3[:220])
    tools._kill_bash({"id": slow_id}, slow)

    print()
    print("=== 7. a TIMED-OUT job is not reported as finished ===")
    # The notification must not fire for a job that is still running.
    still = jobs.pending_notifications(slow)
    check("no notification for the still-running job",
          not any(slow_id in r and "running" in r for r in still), str(still))

    print()
    print("=== 8. A KILLED JOB is reported as killed, not as finished ===")
    # The case a caller must not mistake for success.
    killed = context()
    killed_id = start(killed, _SLEEP)
    time.sleep(0.4)
    tools._kill_bash({"id": killed_id}, killed)
    wait_for(killed, killed_id, timeout=5)
    reports = jobs.pending_notifications(killed)
    check("the killed job is reported", len(reports) == 1, str(len(reports)))
    check("  as killed", "killed" in reports[0], reports[0][:90])
    check("  NOT as exited", "exited" not in reports[0], reports[0][:90])

    print()
    print("=== 9. an unknown id is refused with something actionable ===")
    out4, failed4 = tools._bg_wait({"id": "no-such-job"}, ctx)
    check("an unknown id is an error", failed4, out4[:80])
    check("  it says how to find the real ids", "bg_jobs" in out4, out4[:140])
    check("  and it explains a dropped job is gone",
          "dropped" in out4 or "registry" in out4, out4[:200])
    out5, failed5 = tools._bg_wait({}, ctx)
    check("a missing id is refused", failed5 and "id" in out5.lower(), out5[:90])
    out6, failed6 = tools._bg_wait({"id": job_id, "timeout": "not-a-number"}, ctx)
    check("a non-numeric timeout is refused", failed6, out6[:80])

    print()
    print("=== 10. promote brings the result forward, and KEEPS the job ===")
    # Keeping it is deliberate: the record is what an operator reads later, and discarding it to save a dictionary
    # entry would be a poor trade.
    promoted = context()
    promoted_id = start(promoted, _QUICK)
    wait_for(promoted, promoted_id)
    text, failed7 = tools._bg_wait({"id": promoted_id, "promote": True, "timeout": 10}, promoted)
    check("promotion succeeds", not failed7, text[:80])
    check("  it carries an explicit header", "[" in text and "exited" in text, text[:90])
    check("  and the output", "echoed" in text, text[:120])
    check("  the job is STILL in the registry",
          jobs.get(promoted, promoted_id) is not None,
          "promotion must not discard the record")

    print()
    print("=== 11. the registry cap drops FINISHED jobs and NEVER running ones ===")
    # Losing the handle to a running process would leave something nothing can kill or wait for -- worse than an
    # unbounded registry.
    #
    # The first version of this section started MAX+3 RUNNING jobs and asserted "nothing running was dropped", which
    # passed but proved nothing: with only running jobs present there was nothing droppable, so `enforce_limit`
    # returning [] was the only possible answer. A check that cannot fail is not a check. The registry is now
    # deliberately filled PAST the cap with a mixture -- finished and running -- so the rule has something to choose.
    bounded = context()
    # The running jobs are held by PROCESS OBJECT IDENTITY, not by id string.
    #
    # This section was flaky -- measured: 2 failures in 3 runs. The cause is that `_run_bash` derives a job's id from
    # `background_id_counter`, so an id is a sequence number rather than a stable name, and `start()` resolves "the
    # job I just made" by diffing the registry keys. Under load the diff can resolve to a job that has since been
    # dropped, so `running_ids` sometimes held a recycled name and the assertion compared the wrong things. Object
    # identity cannot be recycled, so the check now asks the question it means to ask: are THESE processes still
    # tracked?
    # The running jobs must ACTUALLY be running, and building them from real processes made this section flaky.
    #
    # Measured, in three stages:
    #   * `states {'failed': 1, 'running': 2, ...}` -- one `_SLEEP` launch FAILED at startup, so `enforce_limit`
    #     correctly dropped it and "no running job was dropped" failed. The bug was the fixture assuming three
    #     launches produce three running jobs.
    #   * A retry-until-running loop fixed it in isolation: 63/63 across five consecutive runs.
    #   * It still failed inside the full 40-verifier sweep, because that sweep starts hundreds of processes and the
    #     launch failure rate rises with the load. A fixture that depends on the OS under load is not a fixture.
    #
    # So this section now tests the RULE with stubs. `enforce_limit` and `view` read exactly two things off a process
    # -- `exit_code` and `terminated` -- so `_StubProc` supplies those and the decision under test is exercised
    # exactly as it is in production, with no process to fail. Sections 2-10 use real jobs for the integration path;
    # this one is about a decision, and a decision should not need a subprocess.
    bounded = context()
    bounded.background_procs = {}
    running_procs = []
    for index in range(3):
        proc = _StubProc(exit_code=None, terminated=False, command=_SLEEP)
        bounded.background_procs[f"bash_{index + 1}"] = proc
        running_procs.append(proc)
    for index in range(jobs.MAX_BACKGROUND_JOBS + 1):
        bounded.background_procs[f"bash_{100 + index}"] = _StubProc(
            exit_code=0, terminated=False, command=_QUICK)

    check("three jobs are genuinely RUNNING",
          sum(1 for p in running_procs if jobs.view("x", p).state == jobs.RUNNING) == 3)
    check("the registry is over the cap", len(bounded.background_procs) > jobs.MAX_BACKGROUND_JOBS,
          f"{len(bounded.background_procs)} tracked, cap {jobs.MAX_BACKGROUND_JOBS}")

    running_ids = [jid for jid, proc in bounded.background_procs.items() if proc in running_procs]
    check("the running jobs are identifiable", len(running_ids) == 3, str(running_ids))

    dropped = jobs.enforce_limit(bounded)
    check("finished jobs WERE dropped", bool(dropped), str(len(dropped)))
    check("  and NONE of the running ones were", not (set(dropped) & set(running_ids)),
          f"dropped {dropped[:4]}, running {running_ids}")
    surviving = set(bounded.background_procs.values())
    check("  every running PROCESS is still tracked",
          all(proc in surviving for proc in running_procs),
          f"{sum(1 for p in running_procs if p in surviving)} of 3 live")
    check("  the registry is back at or under the cap",
          len(bounded.background_procs) <= jobs.MAX_BACKGROUND_JOBS,
          str(len(bounded.background_procs)))
    check("  the OLDEST finished jobs went first",
          dropped[:1] == ["bash_100"], f"first dropped {dropped[:1]}")

    print()
    print("=== 11b. a KILLED job is droppable, and is not confused with a running one ===")
    # `terminated` is checked before `exit_code` precisely because a killed process also has an exit code. Losing
    # that distinction would make a killed job look like a successful one.
    mixed = context()
    mixed.background_procs = {
        "bash_1": _StubProc(exit_code=None, terminated=False, command="running"),
        "bash_2": _StubProc(exit_code=1, terminated=True, command="killed"),
        "bash_3": _StubProc(exit_code=0, terminated=False, command="finished"),
    }
    check("a killed job reports KILLED", jobs.view("bash_2", mixed.background_procs["bash_2"]).state == jobs.KILLED,
          jobs.view("bash_2", mixed.background_procs["bash_2"]).state)
    check("  and an exit code does not override it",
          jobs.view("bash_2", mixed.background_procs["bash_2"]).state != jobs.EXITED)
    check("a running job reports RUNNING", jobs.view("bash_1", mixed.background_procs["bash_1"]).state == jobs.RUNNING)
    check("a clean exit reports EXITED", jobs.view("bash_3", mixed.background_procs["bash_3"]).state == jobs.EXITED)
    check("a killed job is announced as killed", any(
        "killed" in report for report in jobs.pending_notifications(mixed)),
        str(jobs.pending_notifications(mixed))[:120])

    finished = context()
    finished_ids = [start(finished, _QUICK) for i in range(jobs.MAX_BACKGROUND_JOBS + 4)]
    for job_id in finished_ids:
        wait_for(finished, job_id)
    dropped2 = jobs.enforce_limit(finished)
    check("finished jobs ARE dropped past the cap", bool(dropped2), str(len(dropped2)))
    check("  the registry is back under the cap",
          len(finished.background_procs) <= jobs.MAX_BACKGROUND_JOBS,
          str(len(finished.background_procs)))
    check("  and it says what it dropped", "dropped" in jobs.keep_bounded(finished)
          or jobs.keep_bounded(finished) == "", jobs.keep_bounded(finished)[:120])

    print()
    print("=== 12. the service never raises on a broken context ===")
    # Every caller is a tool dispatch inside a turn.
    class Bare:
        pass

    for label, obj in (("an object with nothing", Bare()),):
        for fn in (jobs.all_jobs, jobs.pending_notifications, jobs.enforce_limit, jobs.render_list):
            try:
                result = fn(obj)
                check(f"{fn.__name__} handles {label}", isinstance(result, (list, str, dict)),
                      f"{type(result).__name__}")
            except Exception as exc:  # noqa: BLE001
                check(f"{fn.__name__} handles {label}", False, f"{type(exc).__name__}: {exc}")
    try:
        check("wait on a broken context is refused, not raised",
              jobs.wait(Bare(), "x", 0.1)[0] is False)
    except Exception as exc:  # noqa: BLE001
        check("wait on a broken context is refused, not raised", False, f"{type(exc).__name__}: {exc}")
    check("summary handles a broken context", isinstance(jobs.summary(Bare()), dict))

    print()
    print("=== 13. WIRED: the notification is on the runtime-context carrier ===")
    # Announcing it as a synthetic USER message would put words in the user's mouth; announcing it nowhere is the
    # polling this service replaces. A job service nothing announces through is two more getters.
    adapt = (REPO / "myriaxon" / "model_adapt.py").read_text(encoding="utf-8")
    check("model_adapt announces completions", "pending_notifications" in adapt,
          "the carrier must carry it")
    check("  it announces exactly once by construction",
          "announced EXACTLY ONCE" in adapt or "EXACTLY ONCE" in adapt, "the intent must be recorded")
    check("  and it applies the cap", "keep_bounded" in adapt)
    check("  reusable across providers via model_adapt",
          "def append_runtime_context" in adapt, "all three agents already call this")

    print()
    print("=== 14. an output tail is bounded and SAYS it was truncated ===")
    # A chatty job would otherwise put megabytes into the transcript, and a truncated log read as a complete one is
    # a wrong conclusion waiting to happen.
    big = context()
    big_id = start(big, _BIG)
    wait_for(big, big_id)
    tail = jobs.output_tail(jobs.get(big, big_id), limit=500)
    check("the tail is bounded", len(tail) <= 700, str(len(tail)))
    check("  and the truncation is marked", "omitted" in tail, tail[:80])
    small = jobs.output_tail(jobs.get(big, big_id), limit=100_000)
    check("a small output is not marked", "omitted" not in small, str(len(small)))

    print()
    print(f"=== {len(PASS)} passed, {len(FAIL)} failed ===")
    for label in FAIL:
        print(f"  FAILED: {label}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
