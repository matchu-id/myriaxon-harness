"""The dead-letter store: where a failed run goes so somebody can find out why.

Level 4 requirement 4.17. Before this, a run that died left almost nothing behind: a line in the Locus's own
activity log, a run-state file that was deleted, and no way to answer "what has been failing and why" short
of reading logs by hand. Nobody reads logs by hand, so a repeated failure stayed invisible until it had
happened many times -- which is exactly what happened this session, where the same sandbox defect was
rediscovered on seven separate benchmark runs.

THE PROPERTY THAT MAKES THIS USEFUL is arithmetic across records: one failure is noise, three failures in a
day is a defect, and that distinction cannot be made from a log line. `repeat_offenders` is therefore the
part worth testing hardest.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


@pytest.fixture
def letters(tmp_path, monkeypatch):
    monkeypatch.setenv("MYRIAXON_SWARM_DIR", str(tmp_path / "swarm"))
    from myriaxon import dead_letter

    return dead_letter


class TestFiling:
    def test_a_failure_is_recorded_with_its_context(self, letters):
        entry = letters.record("cowork", "write the brand kit", letters.KIND_FAILED,
                               "WorkspaceEscape: path outside the workspace",
                               pointer="step 3")
        assert entry.id.startswith("dl-")
        assert entry.locus == "cowork" and entry.kind == letters.KIND_FAILED
        assert "WorkspaceEscape" in entry.reason
        assert entry.pointer == "step 3"

    def test_the_goal_is_kept_so_a_reader_knows_what_was_being_attempted(self, letters):
        letters.record("cowork", "produce the poster", letters.KIND_FAILED, "boom")
        assert letters.entries()[0].goal == "produce the poster"

    def test_an_unknown_kind_falls_back_rather_than_being_stored_raw(self, letters):
        """A closed vocabulary is what makes the counts mean anything."""
        letters.record("cowork", "g", "a-typo-of-a-kind", "boom")
        assert letters.entries()[0].kind == letters.KIND_FAILED

    def test_a_long_reason_is_truncated(self, letters):
        """A store that swallows megabytes is a store that gets deleted to save space."""
        letters.record("cowork", "g", letters.KIND_FAILED, "x" * 5000)
        assert len(letters.entries()[0].reason) <= 600

    def test_a_long_goal_is_truncated(self, letters):
        letters.record("cowork", "y" * 5000, letters.KIND_FAILED, "boom")
        assert len(letters.entries()[0].goal) <= 300

    def test_the_transcript_is_not_copied_here(self, letters):
        """A pointer plus a reason. Copying run content would make this a staler second transcript."""
        entry = letters.record("cowork", "g", letters.KIND_FAILED, "boom")
        fields = set(entry.__dataclass_fields__)
        assert "transcript" not in fields and "messages" not in fields and "output" not in fields

    def test_the_pointer_is_kept_for_going_and_looking(self, letters):
        letters.record("cowork", "g", letters.KIND_FAILED, "boom", pointer="step 7, run abc123")
        assert "abc123" in letters.entries()[0].pointer


class TestTheCountIsThePoint:
    def test_one_failure_is_not_an_offender(self, letters):
        letters.record("cowork", "g", letters.KIND_FAILED, "boom")
        assert letters.repeat_offenders() == []

    def test_three_of_the_same_are_an_offender(self, letters):
        """The distinction a log line cannot make."""
        for _ in range(3):
            letters.record("cowork", "g", letters.KIND_FAILED, "the same failure")
        offenders = letters.repeat_offenders()
        assert len(offenders) == 1
        assert offenders[0]["locus"] == "cowork" and offenders[0]["count"] == 3

    def test_different_loci_do_not_combine(self, letters):
        letters.record("cowork", "g", letters.KIND_FAILED, "boom")
        letters.record("sales", "g", letters.KIND_FAILED, "boom")
        letters.record("principal", "g", letters.KIND_FAILED, "boom")
        assert letters.repeat_offenders() == [], "three different agents failing once is not one defect"

    def test_different_kinds_do_not_combine(self, letters):
        letters.record("cowork", "g", letters.KIND_FAILED, "a")
        letters.record("cowork", "g", letters.KIND_REPEATING, "b")
        letters.record("cowork", "g", letters.KIND_BUDGET, "c")
        assert letters.repeat_offenders() == []

    def test_the_window_excludes_old_failures(self, letters):
        """A defect fixed last month must stop being reported.

        THIS TEST WAS WRONG AND A CLEAN CLONE CAUGHT IT. It recorded three failures and then asked for
        offenders inside a `0.0001`-second window, expecting the three records to fall outside it. On this
        machine enough time passed between writing and reading; on a faster one it did not, and the three
        entries landed inside a tenth of a millisecond -- so `repeat_offenders` returned them and the
        assertion failed. The test was measuring the speed of the machine it ran on.

        Fixed by asking the question directly instead of racing for it: `now` is passed explicitly, so the
        window is decided by the test rather than by the clock. The original also assigned `entry` inside the
        loop and never used it, which is the shape of a test written quickly.
        """
        now = time.time()
        for _ in range(3):
            letters.record("cowork", "g", letters.KIND_FAILED, "old")

        # Inside a generous window they ARE offenders -- which is the behaviour that matters.
        assert letters.repeat_offenders(now=now + 60) != []

        # Long after the window closes, they are not.
        assert letters.repeat_offenders(window_seconds=3600, now=now + 7200) == [], (
            "a defect fixed last month must stop being reported"
        )

    def test_offenders_are_ordered_by_count(self, letters):
        for _ in range(5):
            letters.record("cowork", "g", letters.KIND_FAILED, "many")
        for _ in range(3):
            letters.record("sales", "g", letters.KIND_FAILED, "few")
        offenders = letters.repeat_offenders()
        assert [o["count"] for o in offenders] == [5, 3]


class TestReading:
    def test_entries_are_newest_first(self, letters):
        letters.record("a", "g", letters.KIND_FAILED, "first")
        letters.record("b", "g", letters.KIND_FAILED, "second")
        assert letters.entries()[0].reason == "second"

    def test_filter_by_locus(self, letters):
        letters.record("cowork", "g", letters.KIND_FAILED, "from cowork")
        letters.record("sales", "g", letters.KIND_FAILED, "from sales")
        assert [e.reason for e in letters.entries(locus="cowork")] == ["from cowork"]

    def test_filter_by_kind(self, letters):
        letters.record("a", "g", letters.KIND_FAILED, "x")
        letters.record("a", "g", letters.KIND_REPEATING, "y")
        assert [e.reason for e in letters.entries(kind=letters.KIND_REPEATING)] == ["y"]

    def test_the_limit_is_respected(self, letters):
        for index in range(10):
            letters.record("a", "g", letters.KIND_FAILED, f"failure {index}")
        assert len(letters.entries(limit=3)) == 3

    def test_a_missing_store_reads_as_empty_not_an_error(self, tmp_path, monkeypatch):
        from myriaxon import dead_letter

        monkeypatch.setenv("MYRIAXON_SWARM_DIR", str(tmp_path / "never-made"))
        assert dead_letter.entries() == []
        assert dead_letter.summary()["total"] == 0

    def test_a_corrupt_line_does_not_lose_the_others(self, letters):
        letters.record("a", "g", letters.KIND_FAILED, "good one")
        path = letters._store_path()
        with open(path, "a", encoding="utf-8") as handle:
            handle.write("{ this is not json\n")
        letters.record("b", "g", letters.KIND_FAILED, "good two")
        reasons = [e.reason for e in letters.entries()]
        assert "good one" in reasons and "good two" in reasons, (
            "a torn tail must not cost the records around it"
        )


class TestBounded:
    def test_the_store_does_not_grow_without_bound(self, letters, monkeypatch):
        monkeypatch.setattr(letters, "MAX_ENTRIES", 10)
        for index in range(30):
            letters.record("a", "g", letters.KIND_FAILED, f"failure {index}")
        assert len(letters.entries(limit=1000)) <= 10

    def test_trimming_keeps_the_newest(self, letters, monkeypatch):
        monkeypatch.setattr(letters, "MAX_ENTRIES", 3)
        for index in range(8):
            letters.record("a", "g", letters.KIND_FAILED, f"failure {index}")
        assert letters.entries()[0].reason == "failure 7"


class TestTheReport:
    def test_an_empty_store_says_so(self, letters):
        assert "none recorded" in letters.format_report()

    def test_the_report_names_repeating_failures(self, letters):
        for _ in range(3):
            letters.record("cowork", "g", letters.KIND_FAILED, "the same failure")
        report = letters.format_report()
        assert "Repeating failures" in report
        assert "cowork" in report
        assert "defects, not noise" in report

    def test_one_failure_is_reported_without_alarm(self, letters):
        letters.record("cowork", "g", letters.KIND_FAILED, "a one-off")
        report = letters.format_report()
        assert "a one-off" in report
        assert "Repeating failures" not in report


class TestClearingNeedsConfirmation:
    def test_clear_without_confirm_does_nothing(self, letters):
        letters.record("a", "g", letters.KIND_FAILED, "keep me")
        assert letters.clear() == 0
        assert len(letters.entries()) == 1

    def test_clear_with_confirm_empties_it(self, letters):
        letters.record("a", "g", letters.KIND_FAILED, "gone")
        assert letters.clear(confirm=True) == 1
        assert letters.entries() == []
