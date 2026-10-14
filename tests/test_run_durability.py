"""Durable run state: a killed run resumes, and a side effect never happens twice.

These are the level-4 durability requirements, and both are tested the way the failure actually
occurs rather than the way it is convenient to simulate.

**Resumability is tested against a real SIGKILL.** A simulated interruption -- raising an exception, or
setting a flag -- passes against code that flushes on shutdown, and that is precisely the code which
fails when the power goes out. `kill` gives the process no opportunity to run anything, so only state
already on disk can come back. Measured before this existed: a kill at step 9 of 12 discarded every
step and the next wake-up began the objective again.

**Exactly-once is tested at the point of send.** A message to a real person is the clearest case of an
outbound action that cannot be taken back, so the check is that a resumed run does not send it twice.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from myriaxon import run_state  # noqa: E402


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    """Point run state at a scratch directory.

    Monkeypatched rather than set through the environment, because `RUN_STATE_DIR` is derived from
    `CONFIG_DIR` at import time -- so a test that sets the variable after the package is imported would
    write into the developer's real `~/.myriaxon/runs`.
    """
    monkeypatch.setattr(run_state, "RUN_STATE_DIR", tmp_path / "runs")
    return tmp_path / "runs"


class Goal:
    def __init__(self, goal_id="g1", title="do the thing"):
        self.id = goal_id
        self.title = title


class Locus:
    def __init__(self, locus_id="l1", name="tester"):
        self.id = locus_id
        self.name = name


class TestCrashResume:
    """The whole point: a process dies without warning and the work is not lost."""

    CHILD = r'''
import os, sys, time
sys.path.insert(0, {repo!r})
from pathlib import Path
from myriaxon import run_state
run_state.RUN_STATE_DIR = Path({state_dir!r})

class Goal:
    id = "g1"; title = "survive a kill"

state = run_state.begin_run("crash-test", goal=Goal(), budget=12)
for step in range(1, 5):
    run_state.record_step(state, step, text=f"finished step {{step}}")
    print(f"step {{step}} checkpointed", flush=True)
print("PARKED", flush=True)
time.sleep(300)
'''

    def test_a_killed_run_resumes_from_its_last_checkpoint(self, state_dir):
        script = state_dir.parent / "child.py"
        script.write_text(self.CHILD.format(repo=str(REPO), state_dir=str(state_dir)), encoding="utf-8")

        process = subprocess.Popen(
            [sys.executable, str(script)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        parked = False
        try:
            for _ in range(300):
                line = process.stdout.readline()
                if not line:
                    break
                if "PARKED" in line:
                    parked = True
                    break
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=30)

        assert parked, f"child never reached its checkpoint: {(process.stderr.read() or '')[:400]}"

        # What survives a process that was given no chance to clean up.
        state = run_state.load_state("crash-test")
        assert state is not None, "nothing survived a kill -- the run is unrecoverable"
        assert state.step == 4, f"expected 4 completed steps, got {state.step}"
        assert state.resumable, "a killed run must be resumable"
        assert not state.finished, "a killed run must not be recorded as finished"

        # And a NEW run of the same objective continues instead of starting over.
        resumed = run_state.begin_run("crash-test", budget=12)
        assert resumed.step == 4, "begin_run restarted the objective instead of resuming it"

    def test_a_stale_snapshot_is_not_resumed_forever(self, state_dir, monkeypatch):
        """A resumed run and a silently continued run are the same mechanism with different feelings.

        This test found a real bug. `save_state` used to set `updated_at` unconditionally, which made
        that field mean "when this file was last written" rather than "when this run last made
        progress" -- so any bookkeeping save pushed the staleness deadline back, and a run that kept
        re-saving could be resumed forever.
        """
        state = run_state.begin_run("old-test", goal=Goal(), budget=5)
        run_state.record_step(state, 2, text="halfway")
        assert run_state.list_resumable()[0].run_id == "old-test"

        # Age the run the way time would, by moving its progress clock -- the field the staleness check
        # actually reads. Writing through `save_state` is deliberately not enough, and asserting that
        # is the point of the test below.
        import time as _time

        monkeypatch.setattr(run_state, "RUN_STATE_MAX_AGE_SECONDS", 60.0)
        stale = run_state.load_state("old-test")
        stale.updated_at = _time.time() - 3600
        run_state.save_state(stale)
        assert run_state.list_resumable() == [], "an interrupted run older than the limit must not come back"

    def test_bookkeeping_does_not_refresh_the_staleness_clock(self, state_dir):
        """The bug the test above found, asserted directly so it cannot come back.

        A side-effect record is bookkeeping, not progress. If it advanced `updated_at`, a run could
        postpone its own expiry indefinitely simply by doing work that is not a step.
        """
        state = run_state.begin_run("clock-test", goal=Goal(), budget=5)
        run_state.record_step(state, 1, text="one")
        after_progress = run_state.load_state("clock-test").updated_at

        run_state.record_side_effect(state, "message:x:1")
        run_state.save_state(state)
        after_bookkeeping = run_state.load_state("clock-test").updated_at

        assert after_bookkeeping == after_progress, \
            "recording a side effect must not make an abandoned run look freshly active"


class TestExactlyOnce:
    """The requirement that a resumed run cannot repeat an outbound action."""

    def test_a_side_effect_is_remembered_and_reported_done(self, state_dir):
        state = run_state.begin_run("send-test", goal=Goal(), budget=4)
        key = "message:+15551234567:abc123"
        assert not run_state.already_done(state, key)
        run_state.record_side_effect(state, key)
        assert run_state.already_done(state, key)

        # A fresh read from disk, because the crash case is the one that matters.
        assert run_state.already_done(run_state.load_state("send-test"), key)

    def test_the_ledger_outlives_the_run_that_filled_it(self, state_dir):
        """Otherwise "run it again" silently means "send it again"."""
        first = run_state.begin_run("carry-test", goal=Goal(), budget=4)
        run_state.record_side_effect(first, "message:a:1")
        run_state.finish_run(first, outcome="done")

        assert run_state.load_state("carry-test") is None, "a finished run must leave no live snapshot"
        second = run_state.begin_run("carry-test", goal=Goal(), budget=4)
        assert run_state.already_done(second, "message:a:1"), \
            "a completed run's side effects must carry forward"

    def test_an_unknown_state_suppresses_nothing(self, state_dir):
        """Conservative in the direction that matters: no ledger must mean 'send it', not 'skip it'.

        Suppressing a real message is a worse failure than sending a duplicate one, so the absence of
        state has to leave the caller behaving exactly as it did before this module existed.
        """
        assert run_state.already_done(None, "message:a:1") is False
        assert run_state.already_done(run_state.begin_run("x", goal=Goal()), "") is False


class TestMessagingDeduplication:
    """The real outbound path, not a stand-in for it."""

    def test_send_skips_a_message_already_sent_in_this_run(self, state_dir, monkeypatch):
        from myriaxon import messaging

        state = run_state.begin_run("msg-run", goal=Goal(), budget=3)
        monkeypatch.setenv("MYRIAXON_RUN_ID", "msg-run")

        config = messaging.MessagingConfig(provider="generic", send_url="http://127.0.0.1:9/send")
        client = messaging.MessagingClient(config)

        # First send records the side effect, then fails to reach the (deliberately dead) endpoint.
        with pytest.raises(Exception):
            client.send("hello there", to="+15551234567")

        reloaded = run_state.load_state("msg-run")
        assert len(reloaded.side_effects) == 1, "the send must be recorded before it is attempted"

        # Second send is refused before any network call, so it cannot double-send after a resume.
        result = client.send("hello there", to="+15551234567")
        assert result.startswith("skipped:"), f"expected a skip, got {result!r}"
        assert len(run_state.load_state("msg-run").side_effects) == 1

    def test_a_different_message_is_not_skipped(self, state_dir, monkeypatch):
        """The ledger must not be a blanket 'never send twice' -- different content is different work."""
        from myriaxon import messaging

        run_state.begin_run("msg-run2", goal=Goal(), budget=3)
        monkeypatch.setenv("MYRIAXON_RUN_ID", "msg-run2")
        config = messaging.MessagingConfig(provider="generic", send_url="http://127.0.0.1:9/send")
        client = messaging.MessagingClient(config)

        with pytest.raises(Exception):
            client.send("first message", to="+15551234567")
        with pytest.raises(Exception):
            client.send("second message", to="+15551234567")

        assert len(run_state.load_state("msg-run2").side_effects) == 2

    def test_outside_a_run_nothing_is_suppressed(self, state_dir, monkeypatch):
        """An interactive send must never be skipped because of a stale run id."""
        from myriaxon import messaging

        monkeypatch.delenv("MYRIAXON_RUN_ID", raising=False)
        assert messaging._current_run_state() is None
