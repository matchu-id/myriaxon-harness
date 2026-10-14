"""A locus must be creatable self-directed, and must not redo finished work.

Two gaps closed after the first round of autonomy work:

  1. the mode could be switched but not SET AT CREATION, so a self-directed locus had to be made
     assisted and then converted -- and `locus_create` is the tool Myriaxon itself calls, which made
     the mode effectively unreachable from the normal path;
  2. a self-directed locus could finish an objective and never close it, because a locus's own
     unattended turn is refused completion authority. The objective stayed open, so the locus redid
     the same work on every wake-up with nothing in the transcript to explain why.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import pytest  # noqa: E402

from myriaxon import loci  # noqa: E402
from myriaxon.loci import (  # noqa: E402
    AUTONOMY_ASSISTED,
    AUTONOMY_SELF_DIRECTED,
    Locus,
    execute_locus_create,
)


@pytest.fixture()
def registry(tmp_path, monkeypatch):
    """A real registry on a temporary path, so creation exercises the real persist/load."""
    monkeypatch.setattr(loci, "LOCI_WORKSPACE_DIR", tmp_path / "loci")
    monkeypatch.setattr(loci, "LOCI_FILE", tmp_path / "loci.json")
    made = loci.LocusRegistry()
    monkeypatch.setattr(loci, "get_registry", lambda: made)
    return made


def test_a_locus_can_be_created_self_directed(registry):
    text, failed = execute_locus_create({"name": "maker", "autonomy": AUTONOMY_SELF_DIRECTED})
    assert not failed, text
    created = registry.get("maker")
    assert created is not None
    assert created.is_self_directed()
    # It must be on DISK, not only in memory: a locus that reports self-directed until the process
    # restarts is worse than one that never offered the setting.
    assert loci.LocusRegistry().get("maker").is_self_directed()


def test_creation_defaults_to_assisted(registry):
    execute_locus_create({"name": "quiet"})
    assert registry.get("quiet").autonomy == AUTONOMY_ASSISTED
    assert not registry.get("quiet").is_self_directed()


def test_a_bad_mode_creates_nothing_at_all(registry):
    """Half-creating a locus and then failing is the outcome that wastes a user's time."""
    text, failed = execute_locus_create({"name": "broken", "autonomy": "autonomous"})
    assert failed
    assert "autonomous" in text
    assert registry.get("broken") is None


def test_an_explicit_step_budget_is_honoured_at_creation(registry):
    execute_locus_create({"name": "bounded", "autonomy": AUTONOMY_SELF_DIRECTED,
                          "max_steps_per_run": 4})
    assert registry.get("bounded").step_budget() == 4


def test_a_bad_step_budget_creates_nothing(registry):
    text, failed = execute_locus_create({"name": "badsteps", "max_steps_per_run": "lots"})
    assert failed
    assert registry.get("badsteps") is None


def test_a_self_directed_locus_is_told_how_to_stop_repeating_finished_work(tmp_path, monkeypatch):
    """The instruction that replaces the missing completion authority."""
    monkeypatch.setattr(loci, "LOCI_WORKSPACE_DIR", tmp_path / "loci")
    locus = Locus(id="p", name="cowork", autonomy=AUTONOMY_SELF_DIRECTED)
    brief = locus.briefing()
    assert "awaiting your owner's confirmation" in brief
    # It must name `blocked`, because that is the state the runner will not select again. A brief that
    # only said "tell your owner" would leave the objective open and the loop running.
    assert "`blocked`" in brief


def test_an_assisted_locus_is_not_given_that_instruction(tmp_path, monkeypatch):
    """It has completion authority for the owner's objectives, so the workaround would confuse it."""
    monkeypatch.setattr(loci, "LOCI_WORKSPACE_DIR", tmp_path / "loci")
    locus = Locus(id="p", name="quiet", autonomy=AUTONOMY_ASSISTED)
    assert "awaiting your owner's confirmation" not in locus.briefing()


def test_the_per_step_prompt_carries_the_same_instruction_as_the_brief(tmp_path, monkeypatch):
    """Two prompts describing the same agent must not disagree.

    The briefing told a self-directed locus to park finished-but-unconfirmable work as `blocked`, while
    the PER-STEP prompt -- the one it actually sees on every step of a multi-step run, which is where
    the work happens -- offered `blocked` only for "I need a person". The instruction that stops the
    redo loop was therefore absent exactly where it was needed, and the fix would have looked complete
    while the loop survived.
    """
    monkeypatch.setattr(loci, "LOCI_WORKSPACE_DIR", tmp_path / "loci")
    locus = Locus(id="p", name="cowork", autonomy=AUTONOMY_SELF_DIRECTED)
    locus.add_goal("Something worth doing")
    locus.take_run()  # claim it, so a continuation is available
    step_two = locus.continuation_prompt(2)
    if not step_two:
        pytest.skip("no continuation was produced for this goal state")
    assert "awaiting your owner's confirmation" in step_two
    assert "blocked" in step_two


def test_an_assisted_locus_gets_the_plain_step_prompt(tmp_path, monkeypatch):
    monkeypatch.setattr(loci, "LOCI_WORKSPACE_DIR", tmp_path / "loci")
    locus = Locus(id="p", name="quiet", autonomy=AUTONOMY_ASSISTED, max_steps_per_run=3)
    locus.add_goal("Something worth doing")
    locus.take_run()
    step_two = locus.continuation_prompt(2)
    if not step_two:
        pytest.skip("no continuation was produced for this goal state")
    assert "awaiting your owner's confirmation" not in step_two
    assert "blocked" in step_two
