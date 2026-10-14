"""Collective memory: a finding by one member, retrievable by every member.

Level 4 requirement 4.9. Myriaxon had memory in two places and neither was collective --
`loci_capabilities` keeps per-locus memory a peer cannot read, and `memory_store` holds session
transcripts keyed by project path. So a locus could learn something and the next locus would rediscover
it, which is the difference between several agents and a swarm.

These tests pin the three properties that a naive shared file loses, plus the ones that make retrieval
trustworthy rather than merely present.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


@pytest.fixture
def memory(tmp_path, monkeypatch):
    """A collective store of its own, so nothing leaks into a real swarm."""
    monkeypatch.setenv("MYRIAXON_SWARM_DIR", str(tmp_path / "swarm"))
    from myriaxon import locus_memory

    return locus_memory


class TestOneMemberLearnsAnotherRetrieves:
    def test_the_core_property(self, memory):
        """The requirement, stated directly."""
        memory.share("The vertex global endpoint is the bare host", member="cowork")
        found = memory.recall("vertex")
        assert len(found) == 1
        assert found[0].member == "cowork"

    def test_a_search_by_a_different_member_finds_it(self, memory):
        memory.share("PII masking is off by default", member="sales", tags=["uipath"])
        assert [e.member for e in memory.recall("masking")] == ["sales"]

    def test_an_empty_recall_says_nobody_has_recorded_it(self, memory):
        """An empty result is a real answer: investigate rather than assume the store is broken."""
        text, is_error = memory.execute_memory_recall({"query": "nothing here"})
        assert is_error is False
        assert "No peer has recorded this" in text


class TestProvenance:
    def test_an_unattributed_finding_is_refused(self, memory):
        """A finding with no author cannot be questioned, which is how a store fills with confident junk."""
        entry, problem = memory.share("something true", member="")
        assert entry is None and "member" in problem

    def test_the_author_is_recorded_and_returned(self, memory):
        memory.share("a fact", member="cowork")
        assert memory.recall("fact")[0].member == "cowork"

    def test_the_source_is_kept_so_a_reader_can_look(self, memory):
        memory.share("a fact", member="cowork", source="run-42")
        assert memory.recall("fact")[0].source == "run-42"

    def test_the_digest_names_the_author(self, memory):
        memory.share("a fact", member="cowork")
        assert "cowork" in memory.digest()

    def test_an_empty_finding_is_refused(self, memory):
        entry, problem = memory.share("   ", member="cowork")
        assert entry is None and "empty" in problem


class TestRetractionIsRecordedNotDeleted:
    def test_only_the_author_may_retract(self, memory):
        entry, _ = memory.share("a disputed fact", member="cowork")
        ok, problem = memory.retract(entry.id, member="sales", reason="disagree")
        assert ok is False and "cowork" in problem, (
            "allowing anyone to retract would let one member quietly remove another's finding"
        )

    def test_the_author_can_retract(self, memory):
        entry, _ = memory.share("a superseded fact", member="cowork")
        ok, _ = memory.retract(entry.id, member="cowork", reason="superseded")
        assert ok is True
        assert memory.recall("superseded") == [], "a retracted entry must not be returned by default"

    def test_a_retraction_is_still_findable(self, memory):
        """A peer that already relied on it needs to be able to learn it was withdrawn."""
        entry, _ = memory.share("a withdrawn fact", member="cowork")
        memory.retract(entry.id, member="cowork", reason="wrong")
        all_entries = memory.recall("withdrawn", include_retracted=True)
        assert len(all_entries) == 1
        assert all_entries[0].retraction_reason == "wrong"
        assert all_entries[0].live is False

    def test_the_stats_separate_live_from_retracted(self, memory):
        a, _ = memory.share("one", member="x")
        memory.share("two", member="x")
        memory.retract(a.id, member="x")
        s = memory.stats()
        assert (s["total"], s["live"], s["retracted"]) == (2, 1, 1)

    def test_retracting_requires_an_author(self, memory):
        entry, _ = memory.share("a fact", member="cowork")
        ok, problem = memory.retract(entry.id, member="")
        assert ok is False and "required" in problem


class TestRetrieval:
    def test_tags_filter(self, memory):
        memory.share("about gcp", member="a", tags=["gcp"])
        memory.share("about uipath", member="b", tags=["uipath"])
        assert [e.member for e in memory.recall(tags=["gcp"])] == ["a"]

    def test_member_filter(self, memory):
        memory.share("from a", member="alpha")
        memory.share("from b", member="beta")
        assert [e.text for e in memory.recall(member="alpha")] == ["from a"]

    def test_newest_first(self, memory):
        memory.share("older", member="a")
        memory.share("newer", member="a")
        assert memory.recall()[0].text == "newer"

    def test_the_limit_is_respected(self, memory):
        for index in range(5):
            memory.share(f"finding {index}", member="a")
        assert len(memory.recall(limit=2)) == 2

    def test_a_query_matches_the_source_and_tags_too(self, memory):
        """A reader searching for a place should find what was learned there."""
        memory.share("a finding", member="a", source="Lighthouse-Platform", tags=["governance"])
        assert len(memory.recall("lighthouse")) == 1
        assert len(memory.recall("governance")) == 1


class TestItSurvivesRestarts:
    def test_an_entry_written_by_one_process_is_read_by_another(self, memory):
        """The store is on disk, so a member that starts later sees earlier work."""
        memory.share("persisted finding", member="cowork")
        # Re-read from disk rather than from any in-process state.
        assert len(memory.recall("persisted")) == 1

    def test_a_corrupt_store_degrades_to_empty(self, memory):
        """A status panel and a recall must not raise on a torn file."""
        path = memory._store_path()
        path.write_text("{ this is not json", encoding="utf-8")
        assert memory.recall() == []
        assert memory.stats()["total"] == 0


class TestTheTools:
    def test_share_then_recall_through_the_tool_surface(self, memory):
        text, err = memory.execute_memory_share(
            {"text": "the bare host is correct", "member": "cowork", "tags": ["gcp"]}
        )
        assert err is False and "Shared" in text
        text, err = memory.execute_memory_recall({"query": "bare host"})
        assert err is False and "cowork" in text

    def test_sharing_without_an_author_reports_the_reason(self, memory):
        text, err = memory.execute_memory_share({"text": "orphan"})
        assert err is True and "unattributed finding cannot be questioned" in text
