"""Autonomy: a locus may decide its own work, and the boundary stays visible.

The owner asked for `cowork` to be a self-directed agent rather than something that waits to be told,
having been shown that this partly reverses their earlier "do not do anything outside what I told
you". These tests pin the three things that make that request real rather than cosmetic: the mode
changes what the locus is TOLD it may do, it changes what the locus is ALLOWED to do, and it never
changes where the locus may act.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import pytest  # noqa: E402

from myriaxon import loci  # noqa: E402
from myriaxon.loci import (  # noqa: E402
    AUTONOMY_ASSISTED,
    AUTONOMY_MODES,
    AUTONOMY_SELF_DIRECTED,
    DEFAULT_SELF_DIRECTED_STEPS,
    Locus,
)


@pytest.fixture()
def locus(tmp_path, monkeypatch):
    # The registry's workspace root is redirected so a test never writes into the real
    # ~/.myriaxon/loci tree -- a test that mutates the user's own loci is not a test.
    monkeypatch.setattr(loci, "LOCI_WORKSPACE_DIR", tmp_path / "loci")
    return Locus(id="probe", name="cowork", role="maker")


def test_assisted_is_the_default_and_stays_one_step(locus):
    """An upgrade must not silently turn every existing locus loose."""
    assert locus.autonomy == AUTONOMY_ASSISTED
    assert not locus.is_self_directed()
    assert locus.step_budget() == 1


def test_the_brief_withholds_permission_when_the_locus_is_assisted(locus):
    brief = locus.briefing()
    assert "Do not invent work" in brief
    assert "SELF-DIRECTED" not in brief
    # It must not be told about a capability it does not have: naming the tool here would invite a
    # call that the mode does not authorise.
    assert "locus_goal_update" not in brief


def test_a_self_directed_locus_is_told_it_may_decide_and_how_to_record_it(locus):
    locus.autonomy = AUTONOMY_SELF_DIRECTED
    brief = locus.briefing()
    assert "Do not invent work" not in brief
    assert "SELF-DIRECTED" in brief
    # The tool it is told to use must be the REAL one. An earlier version of this brief named
    # `locus_add_goal`, which does not exist anywhere in the package -- a permission that could only
    # ever produce a tool-not-found error.
    assert "locus_goal_update" in brief
    assert "locus_add_goal" not in brief
    # And it must be told which name to address the tool to, since the tool takes a locus name and the
    # locus is the caller rather than the caller's owner.
    assert locus.name in brief


def test_owner_objectives_keep_priority_over_self_set_ones(locus):
    """Self-direction is not a licence to ignore the objectives the owner wrote."""
    locus.autonomy = AUTONOMY_SELF_DIRECTED
    brief = locus.briefing()
    assert "always take priority" in brief


def test_the_step_budget_follows_the_mode_unless_it_was_chosen(locus):
    locus.autonomy = AUTONOMY_SELF_DIRECTED
    assert locus.step_budget() == DEFAULT_SELF_DIRECTED_STEPS
    # An explicit budget wins in both directions: a caller who wants a bound still gets one.
    locus.max_steps_per_run = 3
    assert locus.step_budget() == 3
    locus.max_steps_per_run = loci.MAX_STEPS_PER_RUN_CEILING * 2
    assert locus.step_budget() == loci.MAX_STEPS_PER_RUN_CEILING


def test_the_ceiling_no_longer_stops_a_real_task(locus):
    """The owner chose no spend ceiling, having been shown the runaway-bill argument."""
    assert loci.MAX_STEPS_PER_RUN_CEILING > 100
    locus.max_steps_per_run = loci.MAX_STEPS_PER_RUN_CEILING
    assert locus.step_budget() > DEFAULT_SELF_DIRECTED_STEPS


def test_the_mode_survives_a_round_trip_through_the_store(locus):
    locus.autonomy = AUTONOMY_SELF_DIRECTED
    reopened = Locus.from_dict(locus.to_dict())
    assert reopened.autonomy == AUTONOMY_SELF_DIRECTED
    assert reopened.is_self_directed()


def test_an_unknown_mode_is_not_self_directed(locus):
    """A hand-edited loci.json must fail closed, not open."""
    for value in ("", "selfdirected", "SELF-DIRECTED ", "yes", None, "autonomous"):
        locus.autonomy = value
        assert not locus.is_self_directed(), f"{value!r} must not grant self-direction"


def test_self_direction_does_not_widen_the_workspace(locus):
    """The owner chose self-direction WITH confinement: the two are independent."""
    locus.autonomy = AUTONOMY_ASSISTED
    before = locus.workspace_path()
    locus.autonomy = AUTONOMY_SELF_DIRECTED
    assert locus.workspace_path() == before


def test_the_modes_are_exactly_the_two_documented(locus):
    assert AUTONOMY_MODES == (AUTONOMY_ASSISTED, AUTONOMY_SELF_DIRECTED)
