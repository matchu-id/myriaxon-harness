"""Reliability of the locus execution layer: retry, ceilings, leases, preflight, multi-step runs.

Offline and hermetic, like ``test_loci.py``: every test builds its own registry and workspaces in a
temporary directory, so nothing here touches ``~/.myriaxon``, needs an API key, or sleeps for real.

The properties asserted are the ones that separate "a locus runs" from "a locus can be left alone".
Each one is a failure that has no visible symptom until it is expensive:

* a transient provider failure is retried, a permanent one is not, and neither is swallowed;
* a goal that hits its attempt ceiling becomes a question for a person, not an infinite loop;
* the running flag is cleared even when the run raises, so nothing is left looking busy;
* two processes cannot run the same locus, and a lease left by a dead process expires;
* preflight names every static reason a run would fail, before it spends a token;
* multi-step runs are opt-in and reflection is only invited after real work.
"""

from __future__ import annotations

import json
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from myriaxon import loci


def api_status_error(code: int) -> Exception:
    """An exception shaped like the provider SDKs' own: a class name plus ``status_code``.

    Built dynamically rather than imported, because the real anthropic/openai/gemini error classes
    are exactly what this module must classify without importing three SDKs to do it.
    """
    error = type("APIStatusError", (Exception,), {})(f"HTTP {code}")
    error.status_code = code
    return error


class ReliabilityTestCase(unittest.TestCase):
    """Base: a private registry, a private workspace per locus, no real state touched."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory(prefix="mx-loci-rel-")
        self.root = Path(self._tmp.name)
        self.path = self.root / "loci.json"
        self.registry = loci.LocusRegistry(self.path)
        self._saved_get = loci.get_registry
        loci.get_registry = lambda: self.registry

    def tearDown(self) -> None:
        loci.get_registry = self._saved_get
        self._tmp.cleanup()

    def make(self, name: str = "Rel", goal: str = "do the thing", **kwargs) -> loci.Locus:
        # A workspace per locus: the run lease lives in the workspace, so two loci sharing one would
        # share a lock and the exclusivity tests would pass for the wrong reason.
        kwargs.setdefault("workspace", str(self.root / f"ws-{name}"))
        return self.registry.create(name, goals=[goal] if goal else [], **kwargs)

    def reopened(self) -> loci.LocusRegistry:
        return loci.LocusRegistry(self.path)


class TestErrorClassification(ReliabilityTestCase):
    def test_timeouts_and_dropped_connections_are_transient(self) -> None:
        for exc in (
            TimeoutError("read timed out"),
            ConnectionResetError("connection reset by peer"),
            ConnectionAbortedError("aborted"),
            RuntimeError("peer closed connection without sending complete message body"),
        ):
            with self.subTest(exc=repr(exc)):
                self.assertTrue(loci.is_transient_error(exc))

    def test_rate_limits_and_server_errors_are_transient(self) -> None:
        for code in (408, 425, 429, 500, 502, 503, 504, 529):
            with self.subTest(code=code):
                self.assertTrue(loci.is_transient_error(api_status_error(code)))

    def test_client_errors_are_permanent(self) -> None:
        for code in (400, 401, 403, 404, 405, 422):
            with self.subTest(code=code):
                self.assertFalse(loci.is_transient_error(api_status_error(code)))

    def test_auth_and_model_failures_are_permanent_without_a_status(self) -> None:
        for message in (
            "invalid api key provided",
            "model not found: claude-nope",
            "the requested model does not exist",
        ):
            with self.subTest(message=message):
                self.assertFalse(loci.is_transient_error(RuntimeError(message)))

    def test_an_exception_type_alone_is_enough(self) -> None:
        """The SDKs wrap their errors, so a type name is often the only evidence there is."""
        auth = type("AuthenticationError", (Exception,), {})("nope")
        rate = type("RateLimitError", (Exception,), {})("slow down")
        self.assertEqual(loci.classify_error(auth), loci.ERROR_PERMANENT)
        self.assertEqual(loci.classify_error(rate), loci.ERROR_TRANSIENT)

    def test_the_message_is_consulted_when_nothing_else_says_anything(self) -> None:
        self.assertTrue(loci.is_transient_error(RuntimeError("Overloaded")))
        self.assertTrue(loci.is_transient_error(RuntimeError("rate_limit_error")))
        self.assertTrue(loci.is_transient_error(RuntimeError("upstream returned 503")))
        self.assertFalse(loci.is_transient_error(RuntimeError("invalid_request_error: bad tool schema")))

    def test_geminis_code_attribute_is_read_like_a_status(self) -> None:
        """google.genai's errors carry .code, not .status_code -- see cli._gemini_api_errors."""
        exc = type("APIError", (Exception,), {})("quota")
        exc.code = 429
        self.assertTrue(loci.is_transient_error(exc))
        exc.code = 403
        self.assertFalse(loci.is_transient_error(exc))

    def test_an_unclassifiable_error_is_permanent(self) -> None:
        """Retrying a programming error three times just delays the report the user needs."""
        self.assertFalse(loci.is_transient_error(RuntimeError("something nobody has seen before")))

    def test_backoff_grows_exponentially_and_is_capped(self) -> None:
        self.assertEqual(
            [loci.retry_backoff_seconds(n) for n in (1, 2, 3)], [2.0, 4.0, 8.0]
        )
        self.assertEqual(loci.retry_backoff_seconds(50), loci.TRANSIENT_BACKOFF_CAP_SECONDS)
        self.assertEqual(loci.retry_backoff_seconds(0), loci.retry_backoff_seconds(1))

    def test_the_retry_budget_comes_from_the_shared_constants(self) -> None:
        from myriaxon.constants import TRANSIENT_ERROR_RETRIES

        self.assertEqual(loci.max_run_attempts(), TRANSIENT_ERROR_RETRIES + 1)


class TestRetryExecution(ReliabilityTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.locus = self.make("Retry")
        self.goal = self.locus.goals[0]

    def test_a_transient_failure_is_retried_and_recovers(self) -> None:
        delays: list[float] = []
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] < 3:
                raise TimeoutError("read timed out")
            return "finished"

        outcome = loci.run_with_retry(
            flaky, locus=self.locus, goal=self.goal, sleep=delays.append
        )
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.kind, loci.OUTCOME_RECOVERED)
        self.assertEqual(outcome.attempts, 3)
        self.assertEqual(outcome.result, "finished")
        self.assertEqual(delays, [2.0, 4.0], "the backoff schedule must actually be used")
        self.assertTrue(
            any(a.kind == "retry" for a in self.locus.activity),
            "a recovery nobody can see is indistinguishable from a run that never hiccuped",
        )
        self.assertIn("recovered", self.locus.activity[-1].summary)

    def test_a_permanent_failure_is_not_retried_at_all(self) -> None:
        delays: list[float] = []
        calls = {"n": 0}

        def broken():
            calls["n"] += 1
            raise RuntimeError("invalid api key provided")

        outcome = loci.run_with_retry(
            broken, locus=self.locus, goal=self.goal, sleep=delays.append
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.kind, loci.OUTCOME_PERMANENT)
        self.assertEqual(calls["n"], 1, "a revoked key must not be retried")
        self.assertEqual(delays, [])
        self.assertIn("invalid api key", outcome.error)
        self.assertEqual(self.goal.last_error, outcome.error)
        self.assertEqual(self.locus.last_error, outcome.error)
        self.assertTrue(any(a.kind == "error" for a in self.locus.activity))

    def test_a_permanent_failure_blocks_the_goal_instead_of_looping(self) -> None:
        """Waiting does not fix a bad model name, so the goal becomes a question immediately."""
        self.locus.proactive_prompt()  # claims the goal and records which one this run is for
        outcome = loci.run_with_retry(
            lambda: (_ for _ in ()).throw(RuntimeError("model not found")), locus=self.locus
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(self.goal.status, loci.GOAL_BLOCKED)
        self.assertIn("model not found", self.goal.notes[-1])
        self.assertIsNone(self.locus.next_goal(), "a blocked goal must never be selected again")

    def test_exhausted_transient_retries_are_reported_with_the_real_reason(self) -> None:
        delays: list[float] = []
        outcome = loci.run_with_retry(
            lambda: (_ for _ in ()).throw(TimeoutError("timed out waiting for the provider")),
            locus=self.locus,
            goal=self.goal,
            sleep=delays.append,
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.kind, loci.OUTCOME_EXHAUSTED)
        self.assertEqual(outcome.attempts, loci.max_run_attempts())
        self.assertEqual(len(delays), loci.max_run_attempts() - 1)
        self.assertIn("timed out", outcome.error)
        self.assertIn("gave up after", outcome.describe())
        self.assertEqual(self.goal.last_error, outcome.error)
        self.assertNotEqual(
            self.goal.status,
            loci.GOAL_BLOCKED,
            "a transient failure is worth another wake-up; the attempt ceiling is what bounds it",
        )
        self.assertFalse(self.locus.reflection_prompt(), "a failed run learned nothing to keep")

    def test_a_clean_run_is_recorded_exactly_once(self) -> None:
        outcome = loci.run_with_retry(lambda: "fine", locus=self.locus, goal=self.goal)
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.kind, loci.OUTCOME_SUCCESS)
        self.assertEqual(outcome.attempts, 1)
        entries = [a for a in self.locus.activity if a.kind == "turn"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].summary, "run completed")

    def test_max_attempts_can_be_lowered_for_a_cheap_caller(self) -> None:
        delays: list[float] = []
        outcome = loci.run_with_retry(
            lambda: (_ for _ in ()).throw(TimeoutError("nope")),
            locus=self.locus,
            sleep=delays.append,
            max_attempts=2,
        )
        self.assertEqual(outcome.attempts, 2)
        self.assertEqual(delays, [2.0])

    def test_an_interrupt_is_not_retried_and_not_swallowed(self) -> None:
        """^C is a user stopping a run, not a flaky provider, and must not look like one."""
        delays: list[float] = []

        def interrupted():
            raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            loci.run_with_retry(interrupted, locus=self.locus, sleep=delays.append)
        self.assertEqual(delays, [])

    def test_a_caller_with_only_an_exception_still_records_the_reason(self) -> None:
        report = self.locus.end_run(TimeoutError("connection reset"))
        self.assertFalse(report.ok)
        self.assertEqual(report.error_kind, loci.ERROR_TRANSIENT)
        self.assertEqual(
            report.kind,
            loci.OUTCOME_TRANSIENT,
            "nothing was retried here, so the log must not claim a retry budget was exhausted",
        )
        self.assertIn("connection reset", self.locus.last_error)
        self.assertTrue(any("transient failure" in a.summary for a in self.locus.activity))


class TestGoalCeiling(ReliabilityTestCase):
    def test_the_ceiling_is_a_module_constant_with_a_usable_value(self) -> None:
        self.assertIsInstance(loci.MAX_GOAL_ATTEMPTS, int)
        self.assertGreater(loci.MAX_GOAL_ATTEMPTS, 1)

    def test_an_exhausted_goal_is_blocked_with_an_explanation(self) -> None:
        locus = self.make("Stuck", proactive=True)
        goal = locus.goals[0]
        goal.attempts = loci.MAX_GOAL_ATTEMPTS
        goal.last_error = "provider said no"
        self.assertEqual(locus.proactive_prompt(), "")
        self.assertEqual(goal.status, loci.GOAL_BLOCKED)
        self.assertIn(str(loci.MAX_GOAL_ATTEMPTS), goal.notes[-1])
        self.assertIn("provider said no", goal.notes[-1])
        self.assertTrue(
            any(a.kind == "error" for a in locus.activity),
            "giving up must be an error entry, not a silent status change",
        )
        self.assertIsNone(locus.next_goal())

    def test_a_blocked_goal_is_never_selected_by_a_later_run(self) -> None:
        locus = self.make("Stuck2", proactive=True)
        locus.goals[0].attempts = loci.MAX_GOAL_ATTEMPTS
        locus.proactive_prompt()
        for _ in range(3):
            self.assertEqual(locus.proactive_prompt(), "")
            self.assertEqual(locus.goals[0].status, loci.GOAL_BLOCKED)
        self.assertEqual(locus.goals[0].attempts, loci.MAX_GOAL_ATTEMPTS, "no further attempts are spent")

    def test_a_second_objective_is_still_worked_when_the_first_is_exhausted(self) -> None:
        locus = self.make("Busy", goal="hopeless", proactive=True)
        locus.add_goal("reachable")
        locus.goals[0].attempts = loci.MAX_GOAL_ATTEMPTS
        prompt = locus.proactive_prompt()
        self.assertIn("reachable", prompt)
        self.assertEqual(locus.goals[0].status, loci.GOAL_BLOCKED)
        self.assertEqual(locus.goals[1].status, loci.GOAL_ACTIVE)
        self.assertEqual(locus.goals[1].attempts, 1)

    def test_repeated_wake_ups_stop_at_the_ceiling(self) -> None:
        locus = self.make("Loop", proactive=True)
        with mock.patch.object(loci, "MAX_GOAL_ATTEMPTS", 3):
            prompts = [locus.proactive_prompt() for _ in range(4)]
        self.assertTrue(all(prompts[:3]), "the first three attempts are real work")
        self.assertEqual(prompts[3], "", "the fourth wake-up must not spend another run")
        self.assertEqual(locus.goals[0].attempts, 3)
        self.assertEqual(locus.goals[0].status, loci.GOAL_BLOCKED)

    def test_a_goal_that_completes_is_never_blocked(self) -> None:
        locus = self.make("Done", proactive=True)
        goal = locus.goals[0]
        locus.proactive_prompt()
        locus.set_goal_status(goal.id, "done", "finished properly")
        self.assertEqual(locus.proactive_prompt(), "")
        self.assertEqual(goal.status, loci.GOAL_DONE)

    def test_a_dot_whose_only_objective_needs_a_person_is_not_woken(self) -> None:
        """Nothing to work on means no run: waking it spends a turn to hear the same answer."""
        locus = self.make("Waiting", proactive=True, interval_seconds=30)
        locus.set_goal_status(locus.goals[0].id, "blocked", "needs a decision")
        self.assertFalse(locus.is_due(now=time.time() + 10_000))
        self.assertIsNone(self.registry.next_due(now=time.time() + 10_000))

    def test_a_spent_goal_gets_the_one_wake_up_that_blocks_it(self) -> None:
        """The subtle half of the rule: a used-up goal must still be woken once, because the
        wake-up is what turns it into a blocked objective a person can see."""
        locus = self.make("Spent", proactive=True, interval_seconds=30)
        locus.goals[0].attempts = loci.MAX_GOAL_ATTEMPTS
        self.assertTrue(locus.is_due(), "a spent goal is still runnable until it is blocked")
        locus.proactive_prompt()
        self.assertEqual(locus.goals[0].status, loci.GOAL_BLOCKED)
        self.assertFalse(locus.is_due(now=time.time() + 10_000), "and then the wake-ups stop")

    def test_one_blocked_dot_does_not_silence_another(self) -> None:
        blocked = self.make("Blocked", proactive=True, interval_seconds=30)
        blocked.set_goal_status(blocked.goals[0].id, "blocked", "needs a decision")
        # Persisted, because creating the next locus reloads the registry from disk first -- an
        # unpersisted mutation is an in-memory edit that the next write legitimately outranks.
        self.registry.persist(blocked)
        ready = self.make("Ready", proactive=True, interval_seconds=30)
        schedule = self.registry.next_due(now=time.time() + 10_000)
        assert schedule is not None
        self.assertEqual(schedule[0].name, "Ready")


class TestRunningFlag(ReliabilityTestCase):
    def test_begin_run_marks_the_locus_running_with_a_timestamp(self) -> None:
        locus = self.make("R")
        self.assertFalse(locus.running)
        self.assertTrue(locus.begin_run())
        self.assertTrue(locus.running)
        self.assertTrue(locus.is_running())
        self.assertGreater(locus.run_started_at, 0)

    def test_a_dot_that_is_running_is_not_due_again(self) -> None:
        locus = self.make("R2", proactive=True, interval_seconds=30)
        self.assertTrue(locus.is_due())
        locus.begin_run()
        self.assertFalse(locus.is_due(now=time.time() + 10_000), "a run in flight must not be rescheduled")
        locus.end_run()
        self.assertTrue(locus.is_due(now=time.time() + 10_000))

    def test_end_run_clears_the_flag_and_the_lease(self) -> None:
        locus = self.make("R3")
        locus.begin_run()
        locus.end_run(loci.RunOutcome(ok=True, kind=loci.OUTCOME_SUCCESS, attempts=1))
        self.assertFalse(locus.running)
        self.assertEqual(locus.run_started_at, 0.0)
        self.assertFalse(locus.is_running())
        self.assertIsNone(loci.read_run_lease(locus))
        self.assertGreater(locus.last_run_at, 0)

    def test_the_flag_is_cleared_even_when_the_run_raises(self) -> None:
        locus = self.make("R4")
        with self.assertRaises(ValueError):
            with locus.run_scope():
                raise ValueError("the turn blew up")
        self.assertFalse(locus.running, "a locus left running is a locus nothing will ever run again")
        self.assertFalse(locus.is_running())
        self.assertIsNone(loci.read_run_lease(locus))
        self.assertTrue(any(a.kind == "error" for a in locus.activity))
        self.assertIn("the turn blew up", locus.last_error)

    def test_end_run_clears_the_flag_before_it_does_anything_that_can_fail(self) -> None:
        """The flag is the part that must not be lost to a logging or filesystem error."""
        locus = self.make("R5")
        locus.begin_run()
        with mock.patch.object(loci, "record_failure", side_effect=RuntimeError("log exploded")):
            with self.assertRaises(RuntimeError):
                locus.end_run(TimeoutError("provider died"))
        self.assertFalse(locus.running)
        self.assertIsNone(loci.read_run_lease(locus))

    def test_a_stale_flag_from_a_dead_process_self_heals(self) -> None:
        locus = self.make("R6", proactive=True, interval_seconds=30)
        locus.running = True
        locus.run_started_at = time.time()
        self.assertTrue(locus.is_running())
        locus.run_started_at = time.time() - loci.RUN_LEASE_STALE_SECONDS - 5
        self.assertFalse(locus.is_running(), "age is what stops a dead process looking busy forever")
        self.assertTrue(locus.is_due())

    def test_run_scope_refuses_rather_than_entering_a_second_time(self) -> None:
        """Entering anyway would be two agents in one workspace -- the one thing the lease forbids."""
        locus = self.make("R9")
        locus.begin_run()
        ran = {"body": False}
        with self.assertRaises(loci.RunInProgress) as caught:
            with locus.run_scope():
                ran["body"] = True
        self.assertFalse(ran["body"], "a refused slot must not execute the work")
        self.assertIn(locus.name, str(caught.exception))
        self.assertTrue(locus.running, "the holder's run is untouched by the refusal")

    def test_end_run_records_a_failure_with_its_real_reason(self) -> None:
        locus = self.make("R7")
        locus.proactive_prompt()  # a run claims a goal, which is what a failure is attributed to
        locus.begin_run()
        report = locus.end_run(RuntimeError("invalid model name"))
        self.assertFalse(report.ok)
        self.assertEqual(report.kind, loci.OUTCOME_PERMANENT)
        self.assertEqual(locus.last_error, "invalid model name")
        self.assertTrue(any(a.kind == "error" for a in locus.activity))
        self.assertEqual(locus.goals[0].status, loci.GOAL_BLOCKED)
        self.assertEqual(locus.goals[0].last_error, "invalid model name")

    def test_a_failure_is_not_attributed_to_a_goal_that_was_never_claimed(self) -> None:
        """Guessing which objective a message-driven run was working on would block the wrong one."""
        locus = self.make("R8")
        locus.begin_run()
        locus.end_run(RuntimeError("invalid model name"))
        self.assertEqual(locus.last_error, "invalid model name")
        self.assertEqual(locus.goals[0].status, loci.GOAL_PENDING)
        self.assertEqual(locus.goals[0].last_error, "")


class TestRunLease(ReliabilityTestCase):
    def test_a_second_claim_on_the_same_dot_is_refused(self) -> None:
        """The terminal and the dashboard are separate processes sharing one loci.json."""
        first = self.make("Lease")
        second = loci.Locus(id=first.id, name=first.name, workspace=first.workspace)
        self.assertTrue(first.begin_run())
        self.assertFalse(second.begin_run())
        self.assertFalse(second.running, "a refused claim must not mark the locus running")

    def test_a_refused_claim_is_written_to_the_activity_log(self) -> None:
        first = self.make("Lease2")
        second = loci.Locus(id=first.id, name=first.name, workspace=first.workspace)
        first.begin_run()
        second.begin_run()
        self.assertTrue(
            any("already running" in a.summary for a in second.activity),
            "a run that did not happen for a reason nobody can see looks like a bug",
        )

    def test_releasing_lets_the_next_claim_through(self) -> None:
        first = self.make("Lease3")
        second = loci.Locus(id=first.id, name=first.name, workspace=first.workspace)
        first.begin_run()
        first.end_run()
        self.assertTrue(second.begin_run())
        second.end_run()

    def test_a_stale_lease_expires_and_is_taken_over(self) -> None:
        locus = self.make("Lease4")
        lease = loci.acquire_run_lease(locus)
        assert lease is not None
        now = time.time()
        self.assertIsNone(
            loci.acquire_run_lease(locus, now=now + 5),
            "a lease belonging to a live run must not be taken",
        )
        taken = loci.acquire_run_lease(locus, now=now + loci.RUN_LEASE_STALE_SECONDS + 5)
        self.assertIsNotNone(taken, "a lease from a dead process must expire, never deadlock")

    def test_a_stale_owner_cannot_release_the_new_owners_lease(self) -> None:
        locus = self.make("Lease5")
        stale = loci.acquire_run_lease(locus)
        assert stale is not None
        fresh = loci.acquire_run_lease(locus, now=time.time() + loci.RUN_LEASE_STALE_SECONDS + 5)
        assert fresh is not None
        stale.release()
        self.assertIsNotNone(
            loci.read_run_lease(locus),
            "a late unwinding run must not unlock a locus somebody else is working on",
        )
        self.assertFalse(loci.renew_run_lease(stale))
        self.assertTrue(loci.renew_run_lease(fresh))

    def test_a_garbage_lease_file_counts_as_held_while_it_is_fresh(self) -> None:
        """Guessing "nobody is running this" from a parse failure is how two runs collide."""
        locus = self.make("Lease6")
        path = loci.run_lease_path(locus)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{ not json", encoding="utf-8")
        self.assertIsNone(loci.acquire_run_lease(locus))
        self.assertIsNotNone(loci.read_run_lease(locus))
        self.assertIsNotNone(
            loci.acquire_run_lease(locus, now=time.time() + loci.RUN_LEASE_STALE_SECONDS + 5)
        )

    def test_an_unwritable_workspace_runs_unguarded_instead_of_refusing(self) -> None:
        """A lock file that cannot be written must not be the reason a locus stops working."""
        blocker = self.root / "blocker"
        blocker.write_text("not a directory", encoding="utf-8")
        locus = self.registry.create("Lease7", goals=["g"], workspace=str(blocker / "sub"))
        lease = loci.acquire_run_lease(locus)
        assert lease is not None, "refusing here would turn a cosmetic problem into a stopped agent"
        self.assertFalse(lease.enforced)
        self.assertTrue(locus.begin_run())
        locus.end_run()

    def test_a_release_never_deletes_a_claim_it_cannot_verify(self) -> None:
        """Deleting a lock we cannot read is worse than leaving one behind: two runs, one workspace."""
        locus = self.make("Lease11")
        stale = loci.acquire_run_lease(locus)
        assert stale is not None
        self.assertIsNotNone(
            loci.acquire_run_lease(locus, now=time.time() + loci.RUN_LEASE_STALE_SECONDS + 5)
        )
        with mock.patch.object(loci, "_read_lease_file", return_value=None):
            stale.release()
        self.assertIsNotNone(
            loci.read_run_lease(locus), "an unreadable claim must be left alone, not deleted"
        )

    def test_a_takeover_survives_a_transient_unlink_failure(self) -> None:
        """Windows refuses to delete a file with an open handle, and that handle can briefly
        outlive the process that held it. Dropping the wake-up there would be a silent failure."""
        locus = self.make("Lease12")
        self.assertIsNotNone(loci.acquire_run_lease(locus))
        real_unlink = Path.unlink
        attempts = {"n": 0}

        def flaky_unlink(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            if self.name == loci.RUN_LEASE_FILENAME and attempts["n"] == 0:
                attempts["n"] += 1
                raise PermissionError("the file is still open")
            return real_unlink(self, *args, **kwargs)

        with mock.patch.object(Path, "unlink", flaky_unlink):
            taken = loci.acquire_run_lease(locus, now=time.time() + loci.RUN_LEASE_STALE_SECONDS + 5)
        self.assertIsNotNone(taken, "a transient unlink failure must not lose the run")
        self.assertEqual(attempts["n"], 1, "the retry must actually have been exercised")

    def test_a_lease_file_lives_in_the_dots_own_workspace(self) -> None:
        locus = self.make("Lease8")
        locus.begin_run()
        self.assertTrue((Path(locus.workspace) / loci.RUN_LEASE_FILENAME).is_file())
        locus.end_run()
        self.assertFalse((Path(locus.workspace) / loci.RUN_LEASE_FILENAME).is_file())

    def test_releasing_a_lease_that_is_not_held_reports_false(self) -> None:
        self.assertFalse(loci.release_run_lease(self.make("Lease9")))

    def test_is_running_sees_a_lease_held_by_another_process(self) -> None:
        """The dashboard's view: a run started elsewhere is still "running now"."""
        locus = self.make("Lease10")
        other_process = loci.Locus(id=locus.id, name=locus.name, workspace=locus.workspace)
        other_process.begin_run()
        self.assertFalse(locus.running, "this instance did not start it")
        self.assertTrue(locus.is_running(), "but the lease says it is running")
        other_process.end_run()
        self.assertFalse(locus.is_running())


class TestPreflight(ReliabilityTestCase):
    def test_a_healthy_dot_has_no_problems(self) -> None:
        locus = self.make("Ok", instructions="be careful", proactive=True)
        self.assertEqual(locus.preflight(), [])

    def test_a_disabled_dot_is_reported(self) -> None:
        locus = self.make("Off")
        locus.enabled = False
        problems = locus.preflight()
        self.assertTrue(any("disabled" in p for p in problems), problems)

    def test_a_dot_with_no_objectives_is_reported(self) -> None:
        locus = self.make("Idle", goal="")
        self.assertTrue(any("no open objectives" in p for p in locus.preflight()))

    def test_a_dot_whose_every_objective_is_blocked_is_reported(self) -> None:
        locus = self.make("Waiting")
        locus.set_goal_status(locus.goals[0].id, "blocked", "needs the API key")
        self.assertTrue(any("blocked" in p for p in locus.preflight()))

    def test_exhausted_objectives_are_reported_before_a_run_wastes_itself(self) -> None:
        locus = self.make("Tired")
        locus.goals[0].attempts = loci.MAX_GOAL_ATTEMPTS
        problems = locus.preflight()
        self.assertTrue(any("attempts" in p for p in problems), problems)

    def test_a_dot_with_nothing_to_go_on_is_reported(self) -> None:
        locus = self.make("Empty", goal="")
        self.assertTrue(any("instructions" in p for p in locus.preflight()))

    def test_an_unusable_workspace_is_reported(self) -> None:
        blocker = self.root / "wall"
        blocker.write_text("not a directory", encoding="utf-8")
        locus = self.make("Walled")
        locus.workspace = str(blocker / "sub")
        self.assertTrue(any("workspace" in p for p in locus.preflight()))

    def test_a_half_set_model_override_is_reported(self) -> None:
        locus = self.make("HalfSet", provider="anthropic")
        self.assertTrue(any("model" in p for p in locus.preflight()))
        locus.model = "claude-opus-5"
        self.assertEqual(locus.preflight(), [], "a complete override is a valid configuration")
        locus.provider = ""
        self.assertTrue(any("model" in p for p in locus.preflight()))

    def test_several_problems_are_all_reported(self) -> None:
        locus = self.make("Mess", goal="")
        locus.enabled = False
        problems = locus.preflight()
        self.assertGreaterEqual(len(problems), 3, problems)


class TestMultiStepRuns(ReliabilityTestCase):
    def test_one_step_is_the_default(self) -> None:
        locus = self.make("Single", proactive=True)
        self.assertEqual(locus.max_steps_per_run, 1)
        self.assertEqual(locus.step_budget(), 1)
        locus.proactive_prompt()
        self.assertEqual(locus.continuation_prompt(2), "", "multi-step runs are opt-in")

    def test_a_multi_step_dot_is_told_how_many_steps_it_may_take(self) -> None:
        locus = self.make("Multi", proactive=True, max_steps_per_run=3)
        self.assertIn("up to 3 steps", locus.proactive_prompt())

    def test_a_continuation_prompt_keeps_working_the_open_objective(self) -> None:
        locus = self.make("Multi2", proactive=True, max_steps_per_run=3)
        locus.proactive_prompt()
        follow_up = locus.continuation_prompt(2)
        self.assertIn(locus.goals[0].id, follow_up)
        self.assertIn("Step 2 of 3", follow_up)
        self.assertEqual(locus.continuation_prompt(4), "", "the budget is a ceiling, not a suggestion")

    def test_a_continuation_moves_on_to_the_next_objective(self) -> None:
        locus = self.make("Multi3", goal="first", proactive=True, max_steps_per_run=4)
        locus.add_goal("second")
        locus.proactive_prompt()
        locus.set_goal_status(locus.goals[0].id, "done", "finished")
        self.assertIn("save_skill", locus.continuation_prompt(2), "reflection comes first")
        prompt = locus.continuation_prompt(3)
        self.assertIn("second", prompt, "a continuous locus keeps stepping until the work is gone")

    def test_continuation_stops_when_nothing_is_left(self) -> None:
        locus = self.make("Multi4", proactive=True, max_steps_per_run=5)
        locus.proactive_prompt()
        locus.set_goal_status(locus.goals[0].id, "done", "finished")
        locus.continuation_prompt(2)  # the reflection step
        self.assertEqual(locus.continuation_prompt(3), "", "no work is left to continue")

    def test_the_step_budget_is_clamped(self) -> None:
        locus = self.make("Clamp")
        # The probe is derived from the ceiling rather than typed beside it. A hardcoded number only
        # proves the clamp while the two happen to agree: the previous version asked for 10,000 with a
        # ceiling of 10,000, which is correct AND proves nothing -- the value came back unchanged
        # whether the clamp ran or not.
        locus.max_steps_per_run = loci.MAX_STEPS_PER_RUN_CEILING * 3
        self.assertEqual(locus.step_budget(), loci.MAX_STEPS_PER_RUN_CEILING)
        locus.max_steps_per_run = 0
        self.assertEqual(locus.step_budget(), 1)
        locus.max_steps_per_run = "garbage"  # a hand-edited loci.json
        self.assertEqual(locus.step_budget(), 1)
        locus.max_steps_per_run = None
        self.assertEqual(locus.step_budget(), 1)

    def test_create_clamps_the_stored_value(self) -> None:
        # Derived from the ceiling for the same reason as the test above: `create` clamps to the
        # ceiling, so asking for the ceiling itself proves nothing about whether it clamps at all.
        requested = loci.MAX_STEPS_PER_RUN_CEILING * 2
        locus = self.make("Clamp2", max_steps_per_run=requested)
        self.assertEqual(locus.max_steps_per_run, loci.MAX_STEPS_PER_RUN_CEILING)
        self.assertEqual(self.reopened().get("Clamp2").max_steps_per_run, loci.MAX_STEPS_PER_RUN_CEILING)


class TestReflection(ReliabilityTestCase):
    def test_the_proactive_prompt_invites_reflection_and_permits_finding_nothing(self) -> None:
        locus = self.make("Think", proactive=True)
        prompt = locus.proactive_prompt()
        self.assertIn("save_skill", prompt)
        self.assertIn("normal, expected outcome", prompt)

    def test_no_reflection_before_anything_is_completed(self) -> None:
        locus = self.make("Think2", proactive=True)
        locus.begin_run()
        locus.proactive_prompt()
        self.assertEqual(locus.reflection_prompt(), "")
        locus.end_run()

    def test_a_completed_objective_produces_a_reflection_prompt(self) -> None:
        locus = self.make("Think3", proactive=True, max_steps_per_run=3)
        locus.begin_run()
        locus.proactive_prompt()
        locus.set_goal_status(locus.goals[0].id, "done", "shipped it")
        prompt = locus.reflection_prompt()
        self.assertIn("save_skill", prompt)
        self.assertIn(locus.goals[0].title, prompt)
        locus.end_run()

    def test_reflection_takes_the_first_continuation_step(self) -> None:
        locus = self.make("Think4", goal="first", proactive=True, max_steps_per_run=4)
        locus.add_goal("second")
        locus.begin_run()
        locus.proactive_prompt()
        locus.set_goal_status(locus.goals[0].id, "done", "shipped it")
        self.assertIn("save_skill", locus.continuation_prompt(2))
        self.assertNotIn("save_skill", locus.continuation_prompt(3), "asked once, not every step")
        self.assertIn("second", locus.continuation_prompt(3))
        locus.end_run()

    def test_a_failed_run_never_claims_a_lesson(self) -> None:
        locus = self.make("Think5", proactive=True, max_steps_per_run=3)
        locus.begin_run()
        locus.proactive_prompt()
        loci.run_with_retry(
            lambda: (_ for _ in ()).throw(RuntimeError("model not found")), locus=locus
        )
        self.assertEqual(locus.reflection_prompt(), "")
        self.assertEqual(locus.continuation_prompt(2), "")
        report = locus.end_run(loci.RunOutcome(ok=False, kind=loci.OUTCOME_PERMANENT, error="model not found"))
        self.assertFalse(report.ok)
        self.assertFalse(
            any("save_skill" in a.summary or "reusable" in a.summary for a in locus.activity),
            "a run that achieved nothing must not claim it learned something",
        )


class TestPersistenceOfTheNewState(ReliabilityTestCase):
    def test_the_new_fields_survive_a_json_round_trip(self) -> None:
        """The registry is JSON on disk, so a field that is not serialized is a field that is lost."""
        locus = self.make("Persist", proactive=True, max_steps_per_run=3)
        goal = locus.goals[0]
        goal.last_error = "the provider timed out"
        goal.attempts = 2
        locus.last_error = "the provider timed out"
        locus.last_goal_id = goal.id
        locus.log("retry", "transient failure on attempt 1/4")
        self.registry.persist(locus)

        raw = json.loads(self.path.read_text(encoding="utf-8"))["loci"][0]
        self.assertEqual(raw["last_error"], "the provider timed out")
        self.assertEqual(raw["max_steps_per_run"], 3)
        self.assertEqual(raw["last_goal_id"], goal.id)
        self.assertEqual(raw["goals"][0]["last_error"], "the provider timed out")

        again = self.reopened().get("Persist")
        assert again is not None
        self.assertEqual(again.last_error, "the provider timed out")
        self.assertEqual(again.max_steps_per_run, 3)
        self.assertEqual(again.last_goal_id, goal.id)
        self.assertEqual(again.goals[0].last_error, "the provider timed out")
        self.assertEqual(again.goals[0].attempts, 2)
        self.assertTrue(any(a.kind == "retry" for a in again.activity))

    def test_a_running_flag_is_not_restored_after_a_restart(self) -> None:
        """A run in flight belongs to a process, not to a file -- honouring it would strand a locus."""
        locus = self.make("Crashed")
        locus.begin_run()
        self.registry.persist(locus)
        again = self.reopened().get("Crashed")
        assert again is not None
        self.assertFalse(again.running)
        self.assertEqual(again.run_started_at, 0.0)
        self.assertTrue(again.is_running(), "the lease on disk is the cross-process truth")

    def test_a_file_written_before_these_fields_existed_still_loads(self) -> None:
        self.path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "loci": [
                        {
                            "id": "old1",
                            "name": "Older",
                            "goals": [{"id": "g1", "title": "legacy objective"}],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        locus = self.reopened().get("Older")
        assert locus is not None
        self.assertEqual(locus.max_steps_per_run, 1)
        self.assertEqual(locus.last_error, "")
        self.assertFalse(locus.running)
        self.assertEqual(locus.goals[0].last_error, "")

    def test_an_unknown_goal_key_does_not_lose_the_whole_dot(self) -> None:
        """Forward compatibility: one extra key must not take the locus that owns it down with it."""
        self.path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "loci": [
                        {
                            "id": "new1",
                            "name": "Newer",
                            "goals": [
                                {"id": "g1", "title": "objective", "from_the_future": True}
                            ],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        locus = self.reopened().get("Newer")
        assert locus is not None
        self.assertEqual([g.title for g in locus.goals], ["objective"])

    def test_one_unreadable_goal_entry_still_drops_only_its_own_dot(self) -> None:
        """The forward-compat filter must not turn validation off entirely."""
        self.path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "loci": [
                        {"id": "good", "name": "Good", "goals": [{"id": "g", "title": "t"}]},
                        {"id": "bad", "name": "Bad", "goals": [{"nonsense": True}]},
                    ],
                }
            ),
            encoding="utf-8",
        )
        self.assertEqual([d.name for d in self.reopened().all()], ["Good"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
