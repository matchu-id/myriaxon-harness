"""Level 5.9: memory curation -- deciding what to remember and what to forget.

Before this, collective memory was APPENDED and never curated. That is two problems at once: an unbounded
store is a retrieval tax every recall pays, and once it holds anything sensitive it is a privacy liability
nobody can discharge because nobody knows what is in it.

THE HARD RULE is the same one goal retirement uses: **an agent may never discard what its OWNER wrote.** An
agent handed a delete button for its owner's instructions fails silently -- the entry simply stops appearing
and nobody is told.

USE IS THE EVIDENCE. An entry a peer actually read is worth keeping; one that cost effort and helped nobody is
not, and nothing can tell those apart after the fact. So `recall` counts reads and the sweep keys on that,
rather than on age alone -- a finding established once and used every week is OLDER than one written yesterday
and ignored, and age alone would sweep the wrong one.

A REAL BUG IS PINNED HERE. `_new_id()` was a millisecond timestamp and nothing else, so four entries shared in
the same millisecond received the SAME id. That quietly broke every operation addressing an entry by id:
`retract` marked whichever it found first, `keep_forever` protected the wrong entry, `note_use` bumped the
wrong counter, and the sweep reported ids matching nothing. It surfaced only because a loop wrote four entries
and their ids were compared -- a single-entry test would have passed forever.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


@pytest.fixture
def memory(tmp_path, monkeypatch):
    monkeypatch.setenv("MYRIAXON_SWARM_DIR", str(tmp_path / "swarm"))
    from myriaxon import locus_memory

    return locus_memory


def age_all(memory, days: float = 40) -> None:
    """Push every entry's creation back in time, which is what a sweep keys on when nothing read it."""
    path = memory._store_path()
    data = json.loads(path.read_text(encoding="utf-8"))
    for entry in data["entries"]:
        entry["learned_at"] = time.time() - days * 86400
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def author_as_owner(memory, entry_id: str) -> None:
    path = memory._store_path()
    data = json.loads(path.read_text(encoding="utf-8"))
    for entry in data["entries"]:
        if entry["id"] == entry_id:
            entry["author"] = memory.OWNER_AUTHOR
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


class TestIdsAreUnique:
    """The bug that broke every by-id operation, and the reason this file leads with it."""

    def test_four_entries_in_one_millisecond_get_four_ids(self, memory):
        ids = [memory.share(f"finding {index}", member="cowork")[0].id for index in range(6)]
        assert len(set(ids)) == 6, f"ids collided: {ids}"

    def test_ids_still_sort_by_creation(self, memory):
        """The timestamp prefix is kept so a human can read when an entry was made."""
        first = memory.share("a", member="m")[0].id
        second = memory.share("b", member="m")[0].id
        assert first < second

    def test_retract_marks_the_entry_asked_for(self, memory):
        a = memory.share("first", member="m")[0]
        b = memory.share("second", member="m")[0]
        memory.retract(a.id, member="m", reason="wrong")
        assert memory.recall("second")[0].id == b.id
        assert memory.recall("first") == []

    def test_keep_forever_protects_the_entry_asked_for(self, memory):
        a = memory.share("first", member="m")[0]
        memory.share("second", member="m")
        memory.keep_forever(a.id, member="m")
        age_all(memory)
        doomed = {item["id"] for item in memory.sweep()}
        assert a.id not in doomed


class TestUseIsWhatCounts:
    def test_reading_an_entry_counts_as_a_use(self, memory):
        entry = memory.share("a useful finding", member="m")[0]
        memory.recall("useful")
        assert memory.recall("useful", count_uses=False)[0].uses == 1

    def test_a_report_does_not_count_as_a_use(self, memory):
        """A status panel listing entries is not a peer reading them."""
        memory.share("a finding", member="m")
        memory.recall("finding", count_uses=False)
        assert memory.recall("finding", count_uses=False)[0].uses == 0

    def test_a_used_entry_survives_a_sweep_that_takes_an_unread_one(self, memory):
        """The distinction age alone cannot make."""
        used = memory.share("used one", member="m")[0]
        memory.share("ignored one", member="m")
        memory.recall("used one")          # records a use
        age_all(memory, days=40)

        doomed = {item["id"] for item in memory.sweep()}
        assert used.id not in doomed, "an entry read 40 days ago is still worth keeping"
        assert len(doomed) == 1

    def test_the_reason_names_which_case_it_is(self, memory):
        memory.share("never read", member="m")
        age_all(memory, days=40)
        item = memory.sweep()[0]
        assert "never read" in item["reason"]
        assert item["uses"] == 0


class TestTheOwnerRule:
    def test_an_owner_entry_is_never_swept(self, memory):
        entry = memory.share("the owner's own note", member="m")[0]
        author_as_owner(memory, entry.id)
        age_all(memory, days=999)
        assert entry.id not in {item["id"] for item in memory.sweep()}

    def test_a_locus_entry_is_swept_when_unused(self, memory):
        entry = memory.share("a locus note", member="m")[0]
        age_all(memory, days=999)
        assert entry.id in {item["id"] for item in memory.sweep()}


class TestKeepForever:
    def test_a_protected_entry_is_never_swept(self, memory):
        entry = memory.share("a working configuration", member="m")[0]
        assert memory.keep_forever(entry.id, member="m", reason="took a day to establish")[0] is True
        age_all(memory, days=999)
        assert memory.sweep() == []

    def test_only_the_author_may_protect(self, memory):
        entry = memory.share("a finding", member="cowork")[0]
        ok, problem = memory.keep_forever(entry.id, member="sales")
        assert ok is False and "cowork" in problem

    def test_protecting_an_unknown_id_reports_rather_than_raising(self, memory):
        ok, problem = memory.keep_forever("cm-nope", member="m")
        assert ok is False and "no entry" in problem


class TestTheDryRunIsTheDefault:
    def test_sweep_without_apply_removes_nothing(self, memory):
        """A curation that acts before it can be inspected is one nobody trusts."""
        memory.share("a finding", member="m")
        age_all(memory)
        listed = memory.sweep()
        assert len(listed) == 1
        assert len(memory.recall(count_uses=False)) == 1, "the dry run must not remove anything"

    def test_apply_removes_them(self, memory):
        memory.share("a finding", member="m")
        age_all(memory)
        assert len(memory.sweep(apply=True)) == 1
        assert memory.recall(count_uses=False) == []

    def test_a_swept_entry_is_marked_not_deleted(self, memory):
        entry = memory.share("a finding", member="m")[0]
        age_all(memory)
        memory.sweep(apply=True)
        everyone = memory.recall(include_retracted=True)
        assert any(e.id == entry.id for e in everyone), "a peer that relied on it must be able to find it"
        assert everyone[0].swept_reason

    def test_a_recent_entry_is_not_due(self, memory):
        memory.share("written just now", member="m")
        assert memory.sweep() == []


class TestEveryDecisionIsOnTheRecord:
    def test_the_sweep_is_logged_with_its_reason(self, memory):
        memory.share("a finding", member="m")
        age_all(memory)
        memory.sweep(apply=True)
        log = memory.curation_log()
        assert any(item["action"] == "sweep" for item in log)
        assert all(item.get("reason") for item in log)

    def test_keeping_is_logged_too(self, memory):
        entry = memory.share("a finding", member="m")[0]
        memory.keep_forever(entry.id, member="m", reason="important")
        assert any(item["action"] == "keep" for item in memory.curation_log())

    def test_a_curation_that_cannot_be_audited_is_indistinguishable_from_data_loss(self, memory):
        """The first question anybody asks about a missing entry is 'why did that go'."""
        memory.share("a finding", member="m")
        age_all(memory)
        memory.sweep(apply=True)
        assert memory.curation_log(), "a drop with no record is data loss"


class TestTheReport:
    def test_it_lists_what_would_go_without_acting(self, memory):
        memory.share("an unread finding", member="m")
        age_all(memory)
        report = memory.format_curation()
        assert "would be swept" in report
        assert "Nothing is removed without being listed first" in report
        assert len(memory.recall(count_uses=False)) == 1

    def test_an_empty_report_says_nothing_is_due(self, memory):
        assert "Nothing is due" in memory.format_curation()
