"""Verification for the dot execution layer's reliability guarantees.

Offline and hermetic, like ``dots_check.py``: scratch registries in temporary directories, no API
key, no network, no real state directory touched. What it proves is the set that separates "a dot
runs" from "a dot can be left alone" -- each of these is a failure with no visible symptom until
it is expensive:

* a transient provider failure is retried with backoff, a permanent one is not, and neither is
  swallowed;
* a goal that uses up its attempts becomes a question for a person rather than an infinite loop;
* the running flag is cleared even when the run raises, so nothing is left looking busy;
* two processes cannot run the same dot, a lease from a dead process expires, and a stale owner
  cannot unlock a dot somebody else has taken over;
* preflight names every static reason a run would fail, before it spends a token;
* multi-step runs are opt-in, and reflection is only invited after something was actually finished.
"""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from myriaxon import dots  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    results.append((label, bool(ok), detail))


def scratch() -> Path:
    return Path(tempfile.mkdtemp(prefix="mx-dots-rel-"))


def make(registry: dots.DotRegistry, root: Path, name: str, **kwargs) -> dots.Dot:
    kwargs.setdefault("workspace", str(root / f"ws-{name}"))
    kwargs.setdefault("goals", ["do the thing"])
    return registry.create(name, **kwargs)


def api_status_error(code: int) -> Exception:
    """An exception shaped like the provider SDKs': a type name plus ``status_code``."""
    error = type("APIStatusError", (Exception,), {})(f"HTTP {code}")
    error.status_code = code
    return error


def main() -> int:  # noqa: C901 -- a checklist reads better in one pass than as twenty helpers
    root = scratch()
    registry = dots.DotRegistry(root / "dots.json")

    # ---- classification ----------------------------------------------------------------
    check("a bare timeout is transient", dots.is_transient_error(TimeoutError("read timed out")))
    check("a dropped connection is transient", dots.is_transient_error(ConnectionResetError("reset")))
    for code in (408, 425, 429, 500, 502, 503, 504, 529):
        check(f"HTTP {code} is transient", dots.is_transient_error(api_status_error(code)))
    for code in (400, 401, 403, 404, 422):
        check(f"HTTP {code} is permanent", not dots.is_transient_error(api_status_error(code)))
    check("'Overloaded' is transient", dots.is_transient_error(RuntimeError("Overloaded")))
    check("'rate limit' is transient",
          dots.is_transient_error(RuntimeError("rate limit exceeded, retry later")))
    check("'502 Bad Gateway' is transient",
          dots.is_transient_error(RuntimeError("upstream said 502 Bad Gateway")))
    check("a bad key is permanent",
          not dots.is_transient_error(RuntimeError("invalid api key provided")))
    check("an unknown model is permanent",
          not dots.is_transient_error(RuntimeError("model not found: no-such-model")))
    check("an unclassifiable error is permanent by default",
          not dots.is_transient_error(RuntimeError("something nobody has seen before")))
    gemini = type("APIError", (Exception,), {})("quota")
    gemini.code = 429
    check("google.genai's .code is read like a status", dots.is_transient_error(gemini))
    check("backoff grows exponentially",
          [dots.retry_backoff_seconds(n) for n in (1, 2, 3)] == [2.0, 4.0, 8.0])
    check("backoff is capped", dots.retry_backoff_seconds(99) == dots.TRANSIENT_BACKOFF_CAP_SECONDS)
    check("the retry budget comes from constants", dots.max_run_attempts() >= 2)

    # ---- bounded retry -----------------------------------------------------------------
    dot = make(registry, root, "Retry")
    goal = dot.goals[0]
    delays: list[float] = []
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise TimeoutError("read timed out")
        return "done"

    outcome = dots.run_with_retry(flaky, dot=dot, goal=goal, sleep=delays.append)
    check("a transient failure is retried and recovered", outcome.ok and outcome.attempts == 3)
    check("the recovery is named as such", outcome.kind == dots.OUTCOME_RECOVERED)
    check("the backoff schedule was actually used", delays == [2.0, 4.0])
    check("the retry is visible in the log", any(a.kind == "retry" for a in dot.activity))

    delays.clear()
    outcome = dots.run_with_retry(
        lambda: (_ for _ in ()).throw(RuntimeError("invalid api key")),
        dot=dot, goal=goal, sleep=delays.append,
    )
    check("a permanent failure is not retried", outcome.attempts == 1 and not delays)
    check("the permanent failure keeps its reason", "invalid api key" in outcome.error)
    check("the reason reaches the goal", goal.last_error == outcome.error)
    check("the goal is blocked instead of retried forever", goal.status == dots.GOAL_BLOCKED)
    check("the failure is an error entry", any(a.kind == "error" for a in dot.activity))

    goal.status = dots.GOAL_ACTIVE
    delays.clear()
    outcome = dots.run_with_retry(
        lambda: (_ for _ in ()).throw(TimeoutError("timed out")),
        dot=dot, goal=goal, sleep=delays.append,
    )
    check("transient retries stop at the budget",
          outcome.attempts == dots.max_run_attempts() and len(delays) == outcome.attempts - 1)
    check("exhaustion is reported, not hidden", outcome.kind == dots.OUTCOME_EXHAUSTED)

    delays.clear()
    try:
        dots.run_with_retry(lambda: (_ for _ in ()).throw(KeyboardInterrupt()), sleep=delays.append)
        check("an interrupt propagates", False)
    except KeyboardInterrupt:
        check("an interrupt propagates", not delays)

    # ---- attempt ceiling ---------------------------------------------------------------
    stuck_dot = make(registry, root, "Stuck", proactive=True, interval_seconds=30)
    stuck = stuck_dot.goals[0]
    stuck.attempts = dots.MAX_GOAL_ATTEMPTS
    check("a spent goal is still woken once, so it can be resolved", stuck_dot.is_due())
    check("a spent goal yields no further prompt", stuck_dot.proactive_prompt() == "")
    check("a spent goal is blocked", stuck.status == dots.GOAL_BLOCKED)
    check("the block explains itself", "attempt" in stuck.notes[-1])
    check("giving up is logged as an error", any(a.kind == "error" for a in stuck_dot.activity))
    check("a blocked goal is never selected again", stuck_dot.next_goal() is None)
    check("and the dot stops being woken for it",
          not stuck_dot.is_due(now=time.time() + 10_000))

    busy = make(registry, root, "Busy", proactive=True, goals=["hopeless", "reachable"])
    busy.goals[0].attempts = dots.MAX_GOAL_ATTEMPTS
    check("a second objective is still worked", "reachable" in busy.proactive_prompt())
    check("the spent one steps aside", busy.goals[0].status == dots.GOAL_BLOCKED)

    # ---- running flag ------------------------------------------------------------------
    runner = make(registry, root, "Runner", proactive=True, interval_seconds=30)
    check("begin_run takes the run slot", runner.begin_run() is True)
    check("the dot reports itself running", runner.running and runner.is_running())
    check("a running dot is not scheduled again",
          not runner.is_due(now=time.time() + 10_000))
    runner.end_run()
    check("end_run clears the flag", not runner.running)
    check("end_run releases the lease", dots.read_run_lease(runner) is None)

    try:
        with runner.run_scope():
            raise RuntimeError("the turn blew up")
    except RuntimeError:
        pass
    check("a raising run still clears the flag", not runner.running)
    check("a raising run still releases the lease", dots.read_run_lease(runner) is None)
    check("the exception is recorded", any(a.kind == "error" for a in runner.activity))
    check("the failure keeps its reason", "blew up" in runner.last_error)

    runner.running = True
    runner.run_started_at = time.time() - dots.RUN_LEASE_STALE_SECONDS - 5
    check("a stale running flag self-heals", not runner.is_running())
    runner.running = False

    # ---- cross-process lease -----------------------------------------------------------
    owner = make(registry, root, "Lease")
    other = dots.Dot(id=owner.id, name=owner.name, workspace=owner.workspace)
    check("the first claim is granted", owner.begin_run() is True)
    check("a second claim on the same dot is refused", other.begin_run() is False)
    check("the refusal is visible, not silent",
          any("already running" in a.summary for a in other.activity))
    owner.end_run()
    check("releasing lets the next claim through", other.begin_run() is True)
    other.end_run()

    stale = dots.acquire_run_lease(owner)
    check("a live lease is not taken over", stale is not None
          and dots.acquire_run_lease(owner, now=time.time() + 5) is None)
    fresh = dots.acquire_run_lease(owner, now=time.time() + dots.RUN_LEASE_STALE_SECONDS + 5)
    check("a lease from a dead process expires", fresh is not None)
    if stale is not None:
        stale.release()
    check("a stale owner cannot unlock the new owner's dot", dots.read_run_lease(owner) is not None)
    check("a lost lease cannot be renewed", not dots.renew_run_lease(stale))
    check("a held lease can be renewed", dots.renew_run_lease(fresh))
    if fresh is not None:
        fresh.release()

    blocker = root / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    unguarded = registry.create("Unguarded", goals=["g"], workspace=str(blocker / "sub"))
    lease = dots.acquire_run_lease(unguarded)
    check("an unwritable workspace runs unguarded rather than refusing",
          lease is not None and not lease.enforced)

    # ---- preflight ---------------------------------------------------------------------
    healthy = make(registry, root, "Healthy", instructions="be careful")
    check("a healthy dot has no preflight problems", healthy.preflight() == [])

    disabled = make(registry, root, "Disabled")
    disabled.enabled = False
    check("a disabled dot is reported", any("disabled" in p for p in disabled.preflight()))

    empty = make(registry, root, "Empty", goals=[])
    problems = empty.preflight()
    check("a dot with no objectives is reported",
          any("no open objectives" in p for p in problems))
    check("a dot with nothing to go on is reported",
          any("instructions" in p for p in problems))

    blocked_dot = make(registry, root, "Blocked")
    blocked_dot.set_goal_status(blocked_dot.goals[0].id, "blocked", "needs a key")
    check("an all-blocked dot is reported", any("blocked" in p for p in blocked_dot.preflight()))

    tired = make(registry, root, "Tired")
    tired.goals[0].attempts = dots.MAX_GOAL_ATTEMPTS
    check("a spent dot is reported before it wastes a run",
          any("attempts" in p for p in tired.preflight()))

    walled = make(registry, root, "Walled")
    walled.workspace = str(blocker / "sub")
    check("an unusable workspace is reported", any("workspace" in p for p in walled.preflight()))

    half = make(registry, root, "HalfSet", provider="anthropic")
    check("a half-set model override is reported", any("model" in p for p in half.preflight()))

    # ---- multi-step runs and reflection ------------------------------------------------
    single = make(registry, root, "Single", proactive=True)
    check("one step is the default", single.max_steps_per_run == 1 and single.step_budget() == 1)
    single.proactive_prompt()
    check("continuation is refused by default", single.continuation_prompt(2) == "")

    multi = make(registry, root, "Multi", proactive=True, max_steps_per_run=3)
    check("a multi-step run is told its budget", "up to 3 steps" in multi.proactive_prompt())
    check("a continuation keeps working the objective",
          multi.goals[0].id in multi.continuation_prompt(2))
    check("the budget is a ceiling", multi.continuation_prompt(4) == "")

    thinker = make(registry, root, "Thinker", proactive=True, max_steps_per_run=3)
    thinker.begin_run()
    check("no reflection before anything is finished", thinker.reflection_prompt() == "")
    thinker.proactive_prompt()
    check("still no reflection mid-objective", thinker.reflection_prompt() == "")
    thinker.set_goal_status(thinker.goals[0].id, "done", "shipped it")
    reflection = thinker.reflection_prompt()
    check("a completed objective produces a reflection prompt", "save_skill" in reflection)
    check("reflection is offered once, not every step",
          "save_skill" in thinker.continuation_prompt(2)
          and "save_skill" not in thinker.continuation_prompt(3))
    thinker.end_run()

    failed = make(registry, root, "Failed", proactive=True, max_steps_per_run=3)
    failed.begin_run()
    failed.proactive_prompt()
    dots.run_with_retry(lambda: (_ for _ in ()).throw(RuntimeError("model not found")), dot=failed)
    check("a failed run claims no lesson", failed.reflection_prompt() == "")
    check("a failed run offers no continuation", failed.continuation_prompt(2) == "")
    failed.end_run(dots.RunOutcome(ok=False, kind=dots.OUTCOME_PERMANENT, error="model not found"))

    # ---- persistence -------------------------------------------------------------------
    persisted = make(registry, root, "Persisted", proactive=True, max_steps_per_run=3)
    persisted.goals[0].last_error = "provider timed out"
    persisted.last_error = "provider timed out"
    persisted.last_goal_id = persisted.goals[0].id
    registry.persist(persisted)
    again = dots.DotRegistry(registry.path).get("Persisted")
    check("the failure reason survives a restart", again is not None and again.last_error == "provider timed out")
    check("the per-goal reason survives a restart",
          again is not None and again.goals[0].last_error == "provider timed out")
    check("the step budget survives a restart", again is not None and again.max_steps_per_run == 3)

    running_dot = make(registry, root, "Interrupted")
    running_dot.begin_run()
    registry.persist(running_dot)
    reloaded = dots.DotRegistry(registry.path).get("Interrupted")
    check("a crashed run does not leave the dot marked running forever",
          reloaded is not None and not reloaded.running)
    check("but the lease on disk still shows it",
          reloaded is not None and reloaded.is_running())
    dots.release_run_lease(running_dot)

    old = scratch() / "dots.json"
    old.write_text(
        '{"version": 1, "dots": [{"id": "old", "name": "Older", '
        '"goals": [{"id": "g", "title": "legacy", "from_the_future": true}]}]}',
        encoding="utf-8",
    )
    legacy = dots.DotRegistry(old).get("Older")
    check("a file from before these fields loads with defaults",
          legacy is not None and legacy.max_steps_per_run == 1 and legacy.last_error == "")
    check("an unknown goal key does not lose the dot",
          legacy is not None and [g.title for g in legacy.goals] == ["legacy"])

    return report()


def report() -> int:
    for label, ok, detail in results:
        suffix = f"  — {detail}" if detail and not ok else ""
        print(f"  {'PASS' if ok else 'FAIL'}  {label}{suffix}")
    failed_checks = [r for r in results if not r[1]]
    print()
    print(f"{len(results) - len(failed_checks)}/{len(results)} reliability checks passed")
    return 1 if failed_checks else 0


if __name__ == "__main__":
    sys.exit(main())
