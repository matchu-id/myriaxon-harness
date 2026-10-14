"""The budget must stop the OpenAI-compatible loop, not just the Anthropic one.

Found by an audit, not by a failing test: `run_budget` was imported in `agent.py` and NOWHERE else.
Grepping `openai_agent.py` and `gemini_agent.py` for it returned zero hits, so DeepSeek, Netra,
Ollama, OpenRouter and every generic OpenAI-compatible endpoint ran with NO step limit, NO
wall-clock limit and NO token ceiling -- while the documented limits read as protection that was
not there. An unattended Locus on a gateway or local model could loop until the account or the
machine gave out.

These tests pin the enforcement rather than the numbers: the numbers can be tuned, but a check that
is never reached is the bug.

The approach is deliberately the cheapest possible: a budget of ZERO steps with a stubbed stream
that records whether it was called. If the loop is bounded properly, the stream must NEVER run. If
the old behaviour is restored, the stream runs and the test fails -- which is what makes this a test
rather than a description.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import pytest  # noqa: E402
from rich.console import Console  # noqa: E402

from myriaxon import openai_agent, run_budget  # noqa: E402


class Agent(openai_agent.OpenAIAgent):
    """A real OpenAIAgent with only the network call replaced.

    Subclassing rather than mocking the object keeps every line under test the genuine article --
    the loop, the budget check and the message handling are all the shipped code.
    """

    def __init__(self, budget):
        # `client=None` is safe: the stream is replaced below, so nothing reaches the network. The
        # constructor only stores it.
        super().__init__(client=None, model="test-model", console=Console(quiet=True), yolo=False)
        self.console = Console(quiet=True)
        self.calls = 0
        self._budget = budget

    # -- the seams the loop uses -------------------------------------------------
    def _system_prompt(self) -> str:
        return "test"

    def _prepare_context(self) -> None:
        return None

    def _stream_response_with_retry(self):
        self.calls += 1
        return ("hello", [], "stop", None, None)


@pytest.fixture()
def zero_step_budget(monkeypatch):
    """Force every budget to zero steps, so the very first check must stop the turn."""
    real = run_budget.budget_for

    def forced(*, interactive=True, unattended=False, label="turn", **overrides):
        return real(interactive=interactive, unattended=unattended, label=label, max_steps=0)

    monkeypatch.setattr(run_budget, "budget_for", forced)
    return forced


def test_a_zero_step_budget_stops_the_turn_before_any_model_call(zero_step_budget):
    """THE regression: the loop must consult the budget before spending anything."""
    agent = Agent(zero_step_budget)
    agent.run_turn("do something expensive")
    assert agent.calls == 0, (
        "the model was called despite a zero-step budget -- the budget is not wired into "
        "openai_agent's loop"
    )


def test_the_stop_reason_is_recorded(zero_step_budget):
    """A run that stopped must say why; a silent stop is indistinguishable from a crash."""
    agent = Agent(zero_step_budget)
    agent.run_turn("do something expensive")
    assert getattr(agent, "last_stop_reason", None) is not None


def test_the_turn_is_kept_rather_than_rolled_back(zero_step_budget):
    """Reaching a ceiling is a normal end. The user's prompt must survive it.

    An exception would roll the turn back; the budget must not, because discarding the prompt would
    make a ceiling look like a failure and lose what the user typed.
    """
    agent = Agent(zero_step_budget)
    agent.run_turn("keep me")
    text = " ".join(
        str(m.get("content", "")) for m in agent.messages if isinstance(m, dict)
    )
    assert "keep me" in text


def test_the_budget_reaches_the_tool_context(zero_step_budget):
    """Tools read `ctx.run_budget` to narrow their own scope -- it must be set on this path too."""
    agent = Agent(zero_step_budget)
    agent.run_turn("hello")
    assert agent.tool_context.run_budget is not None


def test_the_loop_detector_is_armed_on_this_path(zero_step_budget):
    """`agent.py` armed one; this loop did not, so a stuck OpenAI-compatible run went unnoticed."""
    agent = Agent(zero_step_budget)
    agent.run_turn("hello")
    assert agent.tool_context.loop_detector is not None


def test_an_unbounded_budget_still_runs_the_turn(monkeypatch):
    """The fix must not stop normal turns: with a real budget the model IS called."""
    agent = Agent(run_budget.budget_for)
    agent.run_turn("hello")
    assert agent.calls >= 1, "a normally-budgeted turn did not reach the model"
