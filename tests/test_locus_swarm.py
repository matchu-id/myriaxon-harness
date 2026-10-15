"""The swarm board: the coordination primitive that Level 4 is defined by.

Level 4 is "coordinates with other agents", and before this existed every locus was confined to its own
workspace and therefore mutually invisible -- three could hold the same objective and never know. The
board is the place they can all see. Confinement is not relaxed to achieve it: the board sits beside the
loci rather than inside them, and a member reads it by explicit path.

These tests pin the properties that make a filesystem board trustworthy, which are the ones a naive
implementation gets wrong:

  * **Two members cannot both claim a task.** Decided by an exclusive create, not by a read-then-write,
    because the gap between a read and a write is exactly where the race lives.
  * **A dead holder does not block work forever.** An expired lease is reclaimable, and reclaiming it is
    reported rather than silent -- a task that had to be recovered is evidence somebody died mid-task.
  * **A task is done only when its result is readable.** `done.json` is written after `result.md`, so a
    reader that sees completion can always read the answer.
  * **A task id cannot escape the board.** Ids become directory names, so they are restricted rather than
    escaped; a `..` is a path-traversal primitive and escaping rules are where that gets subtly wrong.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


@pytest.fixture
def swarm(tmp_path, monkeypatch):
    """A board of its own per test, so nothing leaks into the developer's real swarm."""
    from myriaxon import locus_swarm as sw

    monkeypatch.setenv("MYRIAXON_SWARM_DIR", str(tmp_path / "swarm"))
    sw.ensure_layout()
    return sw


class TestTaskIdsCannotEscape:
    def test_a_traversal_id_is_not_a_path(self, swarm):
        """The id becomes a directory name, so `..` must not survive as one."""
        cleaned = swarm.safe_id("../../etc/passwd")
        assert ".." not in cleaned
        assert "/" not in cleaned
        assert "\\" not in cleaned.replace(swarm.safe_id("a\\b"), "")

    def test_an_id_with_nothing_usable_is_refused(self, swarm):
        with pytest.raises(ValueError):
            swarm.safe_id("///")
        with pytest.raises(ValueError):
            swarm.safe_id("")

    def test_a_task_directory_stays_under_the_board(self, swarm):
        swarm.create_task("../../evil", title="x")
        for path in swarm.board_dir().rglob("item.json"):
            assert swarm.board_dir() in path.parents


class TestOnlyOneMemberCanHoldATask:
    def test_the_second_claim_is_refused(self, swarm):
        """The property the whole board rests on."""
        swarm.create_task("t1", title="one")
        assert swarm.claim("t1", "alpha") is True
        assert swarm.claim("t1", "beta") is False, "two members cannot both hold one task"

    def test_the_holder_can_reclaim_its_own_task(self, swarm):
        """Re-entering your own task is not a conflict."""
        swarm.create_task("t1", title="one")
        swarm.claim("t1", "alpha")
        assert swarm.claim("t1", "alpha") is True

    def test_a_duplicate_task_id_is_refused_while_open(self, swarm):
        swarm.create_task("t1", title="first")
        task, problem = swarm.create_task("t1", title="second")
        assert task is None and "already exists" in problem

    def test_a_finished_task_can_be_requeued(self, swarm):
        swarm.create_task("t1", title="one")
        swarm.claim("t1", "alpha")
        swarm.complete("t1", "alpha", "done")
        task, problem = swarm.create_task("t1", title="second attempt")
        assert task is not None, problem
        assert swarm.read_result("t1") == "", "a requeued task must not serve the previous answer"


class TestExpiryIsTheRecoveryPath:
    def test_an_expired_lease_becomes_available(self, swarm):
        swarm.create_task("t1", title="one")
        swarm.claim("t1", "alpha", lease_seconds=0.01)
        time.sleep(0.05)
        assert [t.id for t in swarm.available()] == ["t1"]

    def test_reclaiming_is_reported_not_silent(self, swarm):
        """A recovered task is evidence a member died mid-work, which is worth seeing."""
        swarm.create_task("t1", title="one")
        swarm.claim("t1", "alpha", lease_seconds=0.01)
        time.sleep(0.05)
        assert swarm.reclaim_expired() == ["t1"]

    def test_a_live_holder_is_not_reclaimed(self, swarm):
        swarm.create_task("t1", title="one")
        swarm.claim("t1", "alpha", lease_seconds=600)
        assert swarm.reclaim_expired() == []
        assert swarm.available() == []

    def test_renewal_keeps_a_slow_holder_alive(self, swarm):
        """A model call can outlast a lease; without renewal two members would write a result.

        THIS TEST WAS WRONG AND ANOTHER TEST FILE EXPOSED IT. It claimed with a `0.01`-second lease and
        immediately renewed, which passed alone on a fast run and failed when the suite was busier: the lease
        had already EXPIRED by the time `renew` was called, and `renew` correctly answers False for an expired
        lease. So the test was measuring how quickly the machine reached the next line.

        `now` is passed explicitly here instead. That is not a workaround -- it is what makes the assertion
        about the RENEWAL rather than about the clock, and it is the same fix applied to the dead-letter
        window test after a clean clone caught that one.

        The production behaviour being pinned:
          * a renewal while the lease is live extends it, and
          * the extension is really written to disk, so a peer reads the longer expiry.
        """
        swarm.create_task("t1", title="one")
        swarm.claim("t1", "alpha", lease_seconds=5.0, now=100.0)

        # Still inside the lease: renewal succeeds and moves the expiry out.
        #
        # `expires_at = now + lease_seconds` = 104 + 600 = 704, NOT 604. The first version of this assertion
        # added the new lease to the ORIGINAL claim time, which is arithmetic the code does not do and should
        # not do -- a renewal measured from the old claim would silently shorten a lease that was renewed
        # late. The failure message named the real value, which is why it was a one-line fix.
        assert swarm.renew("t1", "alpha", lease_seconds=600.0, now=104.0) is True
        lease = swarm.read_lease("t1")
        assert lease.expires_at == 704.0, f"renewal did not extend the lease: {lease}"

        # Read back from disk, past the ORIGINAL expiry, so the extension is proven to be persisted rather
        # than held in memory.
        assert lease.held(200.0) is True, "the renewed lease expired on its original schedule"
        assert swarm.available(now=200.0) == []

    def test_renewing_an_expired_lease_is_refused(self, swarm):
        """The counterpart, asserted deliberately rather than left implicit.

        An expired lease has already been offered to other members, so a holder that could still renew it
        would be claiming a task a peer may hold. The recovery path for a slow holder is `claim`, which lets
        an expired-lease holder take its own task back when it is still unclaimed -- that is where "I am late
        but nobody else took it" is handled, and this is where "I am late and it may be gone" is refused.
        """
        swarm.create_task("t1", title="one")
        swarm.claim("t1", "alpha", lease_seconds=5.0, now=100.0)
        assert swarm.renew("t1", "alpha", lease_seconds=600.0, now=200.0) is False

        # And the same member can still recover it with a fresh claim, because nobody else took it.
        assert swarm.claim("t1", "alpha", lease_seconds=600.0, now=200.0) is True

    def test_a_non_holder_cannot_renew(self, swarm):
        swarm.create_task("t1", title="one")
        swarm.claim("t1", "alpha", lease_seconds=600)
        assert swarm.renew("t1", "beta") is False


class TestCompletionIsOrdered:
    def test_the_result_is_readable_when_the_task_reads_as_done(self, swarm):
        """`done.json` is written last, so seeing completion implies the answer is on disk."""
        swarm.create_task("t1", title="one")
        swarm.claim("t1", "alpha")
        swarm.complete("t1", "alpha", "the answer")
        assert swarm.get_task("t1").done is True
        assert swarm.read_result("t1") == "the answer"

    def test_only_the_holder_may_complete(self, swarm):
        swarm.create_task("t1", title="one")
        swarm.claim("t1", "alpha")
        ok, problem = swarm.complete("t1", "beta", "not mine")
        assert ok is False and "alpha" in problem

    def test_a_finished_task_is_not_available(self, swarm):
        swarm.create_task("t1", title="one")
        swarm.claim("t1", "alpha")
        swarm.complete("t1", "alpha", "x")
        assert swarm.available() == []

    def test_completion_records_who_and_when(self, swarm):
        swarm.create_task("t1", title="one")
        swarm.claim("t1", "alpha")
        swarm.complete("t1", "alpha", "x")
        task = swarm.get_task("t1")
        assert task.completed_by == "alpha" and task.completed_at > 0


class TestReleaseIsNotCompletion:
    def test_a_release_returns_the_task_to_the_pool(self, swarm):
        swarm.create_task("t1", title="one")
        swarm.claim("t1", "alpha")
        assert swarm.release("t1", "alpha", reason="not my specialism") is True
        assert [t.id for t in swarm.available()] == ["t1"]
        assert swarm.get_task("t1").done is False, "handing back is not finishing"

    def test_the_attempt_counter_survives_a_release(self, swarm):
        """A task everyone bounces should be visible as such, not look fresh forever."""
        swarm.create_task("t1", title="one")
        swarm.claim("t1", "alpha")
        swarm.release("t1", "alpha")
        swarm.claim("t1", "beta")
        assert swarm.read_lease("t1").attempt >= 2

    def test_a_non_holder_cannot_release(self, swarm):
        swarm.create_task("t1", title="one")
        swarm.claim("t1", "alpha")
        assert swarm.release("t1", "beta") is False


class TestTagFiltering:
    def test_a_member_claims_only_what_it_is_suited_to(self, swarm):
        swarm.create_task("design-1", title="poster", tags=["design"])
        swarm.create_task("data-1", title="csv", tags=["data"])
        assert [t.id for t in swarm.available(tags=["design"])] == ["design-1"]

    def test_no_filter_returns_everything_open(self, swarm):
        swarm.create_task("a", tags=["design"])
        swarm.create_task("b", tags=["data"])
        assert len(swarm.available()) == 2


class TestStatusNeverRaises:
    def test_an_empty_board_reports_zeros(self, swarm):
        status = swarm.board_status()
        assert status["total"] == 0 and status["open"] == 0

    def test_a_missing_board_is_not_an_error(self, tmp_path, monkeypatch):
        """A status panel must render before anything has ever been created."""
        from myriaxon import locus_swarm as sw

        monkeypatch.setenv("MYRIAXON_SWARM_DIR", str(tmp_path / "never-created"))
        status = sw.board_status()
        assert status["total"] == 0

    def test_the_report_names_the_holders(self, swarm):
        swarm.create_task("t1")
        swarm.claim("t1", "alpha", lease_seconds=600)
        assert "alpha" in swarm.format_board()
