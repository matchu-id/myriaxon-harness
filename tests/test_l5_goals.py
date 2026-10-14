"""Level 5 goal machinery: decomposition, value ordering, and retirement.

Three requirements that share one theme -- an agent deciding what to work on, rather than being handed a list
and working it in order:

  * **5.1** decompose an objective into sub-goals it defines itself
  * **5.2** prioritise competing objectives by its own judgement of value
  * **5.3** retire its own obsolete objectives -- and never the owner's

The refusal in 5.3 is the requirement most worth testing, because getting it wrong is SILENT: an agent that
can retire an objective its owner wrote has been handed a delete button for its instructions, and nothing
tells anyone the objective stopped being pursued.

Two numbers in here are judgements rather than measurements, and both are stated so a reader can disagree
with a number instead of discovering an unbounded process:

  * `MAX_GOAL_DEPTH = 3` -- an agent told to break work into smaller pieces will do so indefinitely
  * the depth cap FLATTENS rather than raising -- refusing would fail a run because the agent tried
    something reasonable
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from myriaxon import loci  # noqa: E402


@pytest.fixture
def locus(tmp_path, monkeypatch):
    monkeypatch.setenv("MYRIAXON_CONFIG_DIR", str(tmp_path / "config"))
    return loci.Locus(id="test", name="tester", role="engineer")


class TestDecomposition:
    """5.1 -- an agent that can only work a flat list is following instructions, not setting sub-goals."""

    def test_a_sub_goal_records_its_parent_and_depth(self, locus):
        parent = locus.add_goal("produce the brand kit")
        child = locus.add_goal("design the logo mark", parent=parent.id, author=loci.LOCUS_GOAL_AUTHOR)
        assert child.parent == parent.id
        assert child.depth == 1

    def test_depth_increases_with_each_level(self, locus):
        goal = locus.add_goal("level zero")
        depths = []
        for _ in range(3):
            goal = locus.add_goal("next", parent=goal.id, author=loci.LOCUS_GOAL_AUTHOR)
            depths.append(goal.depth)
        assert depths == [1, 2, 3]

    def test_the_depth_cap_is_enforced(self, locus):
        """Unbounded decomposition is a fork bomb with a token budget."""
        goal = locus.add_goal("root")
        for _ in range(10):
            goal = locus.add_goal("deeper", parent=goal.id, author=loci.LOCUS_GOAL_AUTHOR)
        assert max(g.depth for g in locus.goals) <= loci.MAX_GOAL_DEPTH

    def test_exceeding_the_cap_flattens_rather_than_raising(self, locus):
        """Refusing would fail a run because the agent tried something reasonable."""
        goal = locus.add_goal("root")
        for _ in range(loci.MAX_GOAL_DEPTH + 1):
            goal = locus.add_goal("deeper", parent=goal.id, author=loci.LOCUS_GOAL_AUTHOR)
        beyond = locus.add_goal("too deep", parent=goal.id, author=loci.LOCUS_GOAL_AUTHOR)
        assert beyond is not None, "the work must survive the cap being reached"
        assert beyond.depth == loci.MAX_GOAL_DEPTH

    def test_an_unknown_parent_is_treated_as_no_parent(self, locus):
        """A bookkeeping mistake must not lose the objective."""
        goal = locus.add_goal("orphan", parent="does-not-exist")
        assert goal.parent == "" and goal.depth == 0

    def test_a_top_level_goal_has_depth_zero(self, locus):
        assert locus.add_goal("plain").depth == 0


class TestValueOrdering:
    """5.2 -- with no human ranking them, something must, and the judgement has to be reviewable."""

    def test_the_highest_value_pending_goal_is_chosen(self, locus):
        locus.add_goal("low", value=0.1, author=loci.LOCUS_GOAL_AUTHOR)
        locus.add_goal("high", value=0.95, author=loci.LOCUS_GOAL_AUTHOR)
        assert locus.next_goal().title == "high"

    def test_the_value_is_recorded_not_just_used(self, locus):
        """The agent will sometimes value what is easiest. A stored number makes that drift inspectable."""
        goal = locus.add_goal("something", value=0.77)
        assert goal.value == 0.77

    def test_an_active_goal_beats_a_higher_valued_pending_one(self, locus):
        """Value decides WHICH eligible objective, not whether to abandon work in progress.

        This preserves a measured fix: returning the first active-or-pending goal meant adding a new objective
        silently starved whatever was half-done. Ordering everything by value would reintroduce that.
        """
        in_progress = locus.add_goal("half done", value=0.1, author=loci.LOCUS_GOAL_AUTHOR)
        in_progress.status = loci.GOAL_ACTIVE
        locus.add_goal("shiny and new", value=0.99, author=loci.LOCUS_GOAL_AUTHOR)
        assert locus.next_goal().title == "half done"

    def test_equal_values_fall_back_to_creation_order(self, locus):
        """An agent that values everything equally behaves exactly as it did before this existed."""
        first = locus.add_goal("first", value=0.5, author=loci.LOCUS_GOAL_AUTHOR)
        locus.add_goal("second", value=0.5, author=loci.LOCUS_GOAL_AUTHOR)
        assert locus.next_goal().id == first.id

    def test_value_is_clamped_to_the_unit_interval(self, locus):
        assert locus.add_goal("too high", value=9.0).value == 1.0
        assert locus.add_goal("too low", value=-5.0).value == 0.0

    def test_a_blocked_goal_is_never_selected(self, locus):
        """Blocked means a person owes an answer; retrying spends tokens to be told the same thing."""
        goal = locus.add_goal("blocked one", value=0.99)
        goal.status = loci.GOAL_BLOCKED
        assert locus.next_goal() is None


class TestRetirement:
    """5.3 -- retire its own obsolete objectives, and never the owner's."""

    def test_an_owner_goal_cannot_be_retired_by_the_agent(self, locus):
        """The refusal that matters, because getting it wrong is silent."""
        owner_goal = locus.add_goal("written by the owner")
        retired, problem = locus.retire_goal(owner_goal.id, "superseded")
        assert retired is None
        assert "owner" in problem
        assert owner_goal.status != loci.GOAL_OBSOLETE, "the objective must be untouched"

    def test_the_refusal_says_what_to_do_instead(self, locus):
        """A bare refusal leaves the agent to try again or give up."""
        goal = locus.add_goal("owner's objective")
        _, problem = locus.retire_goal(goal.id, "x")
        assert "mark it blocked" in problem
        assert "do not try to remove it" in problem

    def test_the_agent_can_retire_its_own_goal(self, locus):
        goal = locus.add_goal("my own idea", author=loci.LOCUS_GOAL_AUTHOR)
        retired, problem = locus.retire_goal(goal.id, "superseded by the kit objective")
        assert problem == ""
        assert retired.status == loci.GOAL_OBSOLETE

    def test_the_reason_is_kept(self, locus):
        goal = locus.add_goal("my own idea", author=loci.LOCUS_GOAL_AUTHOR)
        locus.retire_goal(goal.id, "the logo is now part of the kit")
        assert "part of the kit" in goal.retired_reason

    def test_retirement_is_a_state_change_not_a_deletion(self, locus):
        """A deletion cannot answer 'why did this stop being pursued'."""
        goal = locus.add_goal("my own idea", author=loci.LOCUS_GOAL_AUTHOR)
        locus.retire_goal(goal.id, "done elsewhere")
        assert any(g.id == goal.id for g in locus.goals), "the record must survive"

    def test_a_retired_goal_is_no_longer_selected(self, locus):
        goal = locus.add_goal("my own idea", value=0.99, author=loci.LOCUS_GOAL_AUTHOR)
        locus.retire_goal(goal.id, "not worth doing")
        assert locus.next_goal() is None
        assert goal not in locus.runnable_goals()

    def test_retiring_twice_reports_rather_than_raising(self, locus):
        goal = locus.add_goal("my own idea", author=loci.LOCUS_GOAL_AUTHOR)
        locus.retire_goal(goal.id, "first")
        retired, problem = locus.retire_goal(goal.id, "second")
        assert retired is None and "already" in problem

    def test_the_author_defaults_to_the_owner(self, locus):
        """A goal added without saying who wrote it is treated as the owner's, which is the safe direction."""
        assert locus.add_goal("ambiguous").author == loci.OWNER_GOAL_AUTHOR


class TestTheCapValueIsStated:
    def test_the_depth_cap_is_a_named_constant(self):
        """A judgement, not a measurement -- so a reader can disagree with a number."""
        assert loci.MAX_GOAL_DEPTH == 3

    def test_obsolete_is_a_terminal_state_not_a_removal(self):
        assert loci.GOAL_OBSOLETE in loci.GOAL_STATES
        assert loci.GOAL_OBSOLETE not in (loci.GOAL_PENDING, loci.GOAL_ACTIVE, loci.GOAL_BLOCKED)
