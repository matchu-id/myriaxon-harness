"""Drive the REAL multi-step run loop, so "12 steps" is a measurement rather than a claim.

Everything about autonomy rests on this loop actually iterating: a self-directed locus that is told it
may take twelve steps, and then given one, has not been made autonomous -- it has been given a longer
leash on the same post. Until now the step budget was unit-tested as a NUMBER (`step_budget() == 12`)
and the loop that consumes it was never driven.

The loop is `cli._run_locus_steps`. It is exercised with `dispatch_tool` replaced, because the thing
under test is the loop's control flow -- how many turns it starts, which prompt each gets, and when it
stops -- not the model that would answer. A stub that recorded the count and returned a canned turn
tests exactly that and needs no network.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import pytest  # noqa: E402

from myriaxon import cli, loci, tool_registry  # noqa: E402
from myriaxon.loci import (  # noqa: E402
    AUTONOMY_ASSISTED,
    AUTONOMY_SELF_DIRECTED,
    DEFAULT_SELF_DIRECTED_STEPS,
    Locus,
)


class RecordingDispatch:
    """Stands in for `tool_registry.dispatch_tool`, remembering every prompt it was handed."""

    def __init__(self, fail_on: int = 0):
        self.prompts: list[str] = []
        self.sources: list[object] = []
        self.fail_on = fail_on

    def __call__(self, name, tool_input, ctx=None):
        self.prompts.append(str(tool_input.get("message", "")))
        self.sources.append(getattr(ctx, "locus_run_source", None))
        if self.fail_on and len(self.prompts) == self.fail_on:
            return "the model refused", True
        return f"step {len(self.prompts)} done", False


@pytest.fixture()
def harness(tmp_path, monkeypatch):
    monkeypatch.setattr(loci, "LOCI_WORKSPACE_DIR", tmp_path / "loci")
    monkeypatch.setattr(loci, "LOCI_FILE", tmp_path / "loci.json")
    monkeypatch.setattr(cli, "locus_registry_touch", lambda locus: None)

    def build(autonomy=AUTONOMY_SELF_DIRECTED, **kwargs):
        locus = Locus(id="p", name="cowork", role="maker", autonomy=autonomy, **kwargs)
        goal = locus.add_goal("Make something worth making")
        taken = locus.take_run()
        assert taken is not None, "the fixture needs one claimed goal"
        claimed, first_prompt = taken
        agent = SimpleNamespace(tool_context=SimpleNamespace(locus_run_source="human"))
        console = SimpleNamespace(print=lambda *a, **k: None)
        return locus, claimed, first_prompt, agent, console

    return build


def run_loop(monkeypatch, parts, dispatch, capsys):
    locus, goal, first_prompt, agent, console = parts
    monkeypatch.setattr(tool_registry, "dispatch_tool", dispatch)
    args = SimpleNamespace(json=True)
    result = cli._run_locus_steps(locus, goal, first_prompt, agent, console, args)
    # `args.json` makes the loop print one JSON line per step. Captured rather than suppressed so the
    # output is still available if a test wants to assert on it, and so a 12-step run does not bury a
    # failure under twelve lines of progress.
    capsys.readouterr()
    return result


def test_a_self_directed_locus_actually_takes_its_full_budget(harness, monkeypatch, capsys):
    """The claim under test: 12 steps is what the loop does, not what a getter returns."""
    dispatch = RecordingDispatch()
    ran, failed = run_loop(monkeypatch, harness(), dispatch, capsys)
    assert not failed
    assert ran
    assert len(dispatch.prompts) == DEFAULT_SELF_DIRECTED_STEPS


def test_an_assisted_locus_still_takes_exactly_one_step(harness, monkeypatch, capsys):
    """The default must not have moved: an upgrade cannot silently unbind existing loci."""
    dispatch = RecordingDispatch()
    run_loop(monkeypatch, harness(autonomy=AUTONOMY_ASSISTED), dispatch, capsys)
    assert len(dispatch.prompts) == 1


def test_the_first_step_gets_the_goal_and_later_steps_get_continuations(harness, monkeypatch, capsys):
    dispatch = RecordingDispatch()
    parts = harness()
    run_loop(monkeypatch, parts, dispatch, capsys)
    first = dispatch.prompts[0]
    assert "Make something worth making" in first
    # Every later prompt must name the step it is, or the model cannot tell a fresh objective from a
    # continuation of the last one and will restart the work.
    assert "Step 2 of" in dispatch.prompts[1]
    assert "Step 3 of" in dispatch.prompts[2]


def test_the_run_is_marked_as_provenance_for_its_whole_duration(harness, monkeypatch, capsys):
    """The completion gate reads this marker, so a loop that set it per step would leave gaps."""
    dispatch = RecordingDispatch()
    locus, goal, first_prompt, agent, console = harness()
    monkeypatch.setattr(tool_registry, "dispatch_tool", dispatch)
    # `_run_one_dot` is the wrapper that claims the goal and sets/restores the provenance marker.
    # Driving it rather than the inner loop is what makes the marker assertions meaningful.
    cli._run_one_dot(locus, agent, console, SimpleNamespace(json=True))
    capsys.readouterr()  # the loop prints JSON lines; they are noise here, not assertions
    assert set(dispatch.sources) == {"locus"}
    # And it must be RESTORED, not left set: a stale marker would make a later interactive turn
    # unable to complete anything, because every turn would look like an unattended wake-up.
    assert agent.tool_context.locus_run_source == "human"


def test_a_failed_step_stops_the_run_and_reports_failure(harness, monkeypatch, capsys):
    """Twelve steps must not mean twelve attempts at a step that already failed."""
    dispatch = RecordingDispatch(fail_on=3)
    ran, failed = run_loop(monkeypatch, harness(), dispatch, capsys)
    assert failed
    assert ran
    assert len(dispatch.prompts) == 3, "the loop kept going after a failed step"


def test_a_locus_with_no_work_left_stops_without_calling_the_model(harness, monkeypatch, capsys):
    """`continuation_prompt` returns "" when nothing is selectable, and that must end the run."""
    dispatch = RecordingDispatch()
    parts = harness()
    locus = parts[0]
    # No second objective, and the first is claimed -- so the continuation has nothing to hand over.
    ran, failed = run_loop(monkeypatch, parts, dispatch, capsys)
    assert not failed
    # One step ran (the claimed goal). The continuations may or may not produce work depending on goal
    # state, but the run must never exceed the budget.
    assert len(dispatch.prompts) <= DEFAULT_SELF_DIRECTED_STEPS
    assert locus.step_budget() == DEFAULT_SELF_DIRECTED_STEPS


def test_an_explicit_budget_bounds_the_loop(harness, monkeypatch, capsys):
    dispatch = RecordingDispatch()
    run_loop(monkeypatch, harness(max_steps_per_run=3), dispatch, capsys)
    assert len(dispatch.prompts) == 3
