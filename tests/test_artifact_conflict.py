"""Conflict detection on shared artifacts: Level 4.11.

Two members writing one file is not a merge problem to solve later -- it is a problem to DETECT, because the
second write silently destroys the first and neither member finds out.

These tests pin the two questions the module answers, and the property that makes it survivable: it DETECTS
and never BLOCKS. A lock held by a member that dies is a lock nobody releases, which turns one crash into a
stopped swarm -- the same shape the board avoids with expiring leases rather than locks.

The honest gap is asserted too: the check-and-write window cannot be closed without a lock, so `check_write`
narrows the collision window rather than eliminating it. A test says so, so nobody assumes a guarantee that
is not there.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


@pytest.fixture
def conflict(tmp_path, monkeypatch):
    monkeypatch.setenv("MYRIAXON_SWARM_DIR", str(tmp_path / "swarm"))
    from myriaxon import artifact_conflict

    return artifact_conflict


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    return root


class TestTheLostUpdateIsCaught:
    def test_a_file_changed_since_the_read_is_reported_unsafe(self, conflict, workspace):
        """The case that actually loses work: A reads, B writes, A writes back."""
        target = workspace / "out.svg"
        target.write_text("<svg>v1</svg>")
        read_digest = conflict.digest(target)

        target.write_text("<svg>v2 by someone else</svg>")
        safe, problem = conflict.check_write(target, "cowork", read_digest, root=workspace)

        assert safe is False
        assert "changed since you read it" in problem
        assert "erase that change" in problem

    def test_an_untouched_file_is_safe_to_write(self, conflict, workspace):
        target = workspace / "out.svg"
        target.write_text("<svg>v1</svg>")
        safe, problem = conflict.check_write(target, "cowork", conflict.digest(target), root=workspace)
        assert safe is True and problem == ""

    def test_no_digest_recorded_means_no_objection(self, conflict, workspace):
        """A caller that did not record a digest is not second-guessed."""
        target = workspace / "out.svg"
        target.write_text("x")
        assert conflict.check_write(target, "cowork", "", root=workspace) == (True, "")

    def test_a_vanished_file_is_not_reported_as_a_conflict(self, conflict, workspace):
        """'It is gone' and 'somebody wrote it' need different handling."""
        target = workspace / "gone.md"
        safe, problem = conflict.check_write(target, "cowork", "deadbeef", root=workspace)
        assert safe is True
        assert "could not be read" in problem and "changed since" not in problem

    def test_the_named_holder_is_included_when_known(self, conflict, workspace):
        target = workspace / "out.svg"
        target.write_text("v1")
        conflict.declare(target, "sales", root=workspace)
        target.write_text("v2")
        safe, problem = conflict.check_write(target, "cowork", "someotherdigest", root=workspace)
        assert safe is False and "sales" in problem


class TestAdvisoryIntents:
    def test_a_second_member_is_warned_not_refused(self, conflict, workspace):
        """Warned, because refusing lets one member's crash stop another's work."""
        target = workspace / "out.svg"
        ok, warning = conflict.declare(target, "cowork", root=workspace, note="the logo")
        assert ok is True and warning == ""

        ok, warning = conflict.declare(target, "sales", root=workspace)
        assert ok is True, "a declared artifact must not be blocked"
        assert "cowork" in warning and "the logo" in warning

    def test_redeclaring_your_own_artifact_is_not_a_conflict(self, conflict, workspace):
        target = workspace / "out.svg"
        conflict.declare(target, "cowork", root=workspace)
        ok, warning = conflict.declare(target, "cowork", root=workspace)
        assert ok is True and warning == ""

    def test_an_unattributed_declaration_is_refused(self, conflict, workspace):
        ok, problem = conflict.declare(workspace / "out.svg", "", root=workspace)
        assert ok is False and "member" in problem

    def test_release_frees_the_artifact(self, conflict, workspace):
        target = workspace / "out.svg"
        conflict.declare(target, "cowork", root=workspace)
        assert conflict.release(target, "cowork", root=workspace) is True
        assert conflict.holders(target, root=workspace) == []

    def test_a_non_holder_cannot_release(self, conflict, workspace):
        target = workspace / "out.svg"
        conflict.declare(target, "cowork", root=workspace)
        assert conflict.release(target, "sales", root=workspace) is False

    def test_an_expired_intent_stops_reserving_the_file(self, conflict, workspace):
        """A member that dies must not block the artifact forever."""
        target = workspace / "out.svg"
        conflict.declare(target, "cowork", root=workspace, seconds=0.01)
        time.sleep(0.05)
        assert conflict.holders(target, root=workspace) == []
        ok, warning = conflict.declare(target, "sales", root=workspace)
        assert ok is True and warning == ""


class TestArtifactIdentity:
    def test_the_same_file_under_two_roots_is_the_same_artifact(self, conflict, tmp_path):
        """Relative keys, so two members using different absolute roots still collide."""
        a = tmp_path / "one" / "out.svg"
        b = tmp_path / "two" / "out.svg"
        a.parent.mkdir(); b.parent.mkdir()
        a.write_text("x"); b.write_text("x")
        assert conflict.artifact_key(a, root=a.parent) == conflict.artifact_key(b, root=b.parent)

    def test_a_path_outside_the_root_still_produces_a_key(self, conflict, workspace):
        outside = workspace.parent / "elsewhere.svg"
        outside.write_text("x")
        key = conflict.artifact_key(outside, root=workspace)
        assert key and "/" not in key

    def test_backslashes_and_slashes_agree(self, conflict):
        assert conflict.artifact_key(r"sub\out.svg") == conflict.artifact_key("sub/out.svg")

    def test_an_unreadable_file_digests_to_empty_not_to_a_hash(self, conflict, workspace):
        """Empty means 'unknown'. A caller that read it as 'unchanged' would skip the check."""
        assert conflict.digest(workspace / "missing.md") == ""


class TestItNeverBlocks:
    def test_declaring_an_artifact_always_succeeds_for_a_named_member(self, conflict, workspace):
        """Two members, one file, both succeed. The decision belongs to the caller."""
        target = workspace / "out.svg"
        assert conflict.declare(target, "alpha", root=workspace)[0] is True
        assert conflict.declare(target, "beta", root=workspace)[0] is True

    def test_the_active_list_shows_who_holds_what(self, conflict, workspace):
        conflict.declare(workspace / "a.svg", "alpha", root=workspace)
        conflict.declare(workspace / "b.svg", "beta", root=workspace)
        current = conflict.active(root=workspace)
        assert sorted(current.values()) == ["alpha", "beta"]

    def test_expired_entries_are_not_reported(self, conflict, workspace):
        conflict.declare(workspace / "a.svg", "alpha", root=workspace, seconds=0.01)
        time.sleep(0.05)
        assert conflict.active(root=workspace) == {}

    def test_the_report_is_readable_when_empty(self, conflict):
        assert "nobody has declared" in conflict.format_intents()

    def test_the_intent_store_is_bounded(self, conflict, workspace, monkeypatch):
        """An unbounded intent file is a slow leak in a mechanism nobody watches."""
        monkeypatch.setattr(conflict, "MAX_TRACKED", 5)
        for index in range(12):
            conflict.declare(workspace / f"f{index}.svg", "alpha", root=workspace)
        assert len(conflict.active(root=workspace)) <= 5


class TestTheStatedGap:
    def test_two_writes_without_a_check_are_not_prevented(self, conflict, workspace):
        """Asserted so nobody mistakes this for a guarantee it does not make.

        Closing the check-and-write window needs a lock, and a lock held by a member that dies stops the
        swarm. This narrows the window; it does not eliminate it.
        """
        target = workspace / "out.svg"
        target.write_text("v1")
        conflict.declare(target, "alpha", root=workspace)
        conflict.declare(target, "beta", root=workspace)
        # beta writes without ever calling check_write -- nothing stops it, by design.
        target.write_text("v2")
        assert target.read_text() == "v2"
