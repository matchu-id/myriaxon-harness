"""Level 5.8: a context budget the agent can SEE and CHOOSE, rather than one applied to it.

The harness already manages context automatically -- refuse, then prune, then compact. That is a good
mechanism and it is not the requirement. The requirement is that the agent manages it DELIBERATELY, and an
agent cannot decide about a number it is never shown.

The genuinely new capability is SPILLING. The automatic path can prune, which DISCARDS an old result, and
compact, which spends a model call to summarise. Neither lets the agent say "I will need this later". Moving
it to a file does, and that distinction is the whole value: pruning loses information, spilling relocates it.

A test asserts the numbers come from the SAME evaluation the harness uses (`context_budget.evaluate`), because
a second calculation would eventually disagree with the first -- and the agent would then be deciding on a
number nobody enforces.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from myriaxon import context_budget as cb  # noqa: E402


class FakeAgent:
    """Only the attributes the status reads, plus a real profile so the numbers are real."""

    model = "gemini-3.8-flash"
    max_input_tokens = 1_000_000
    profile = None

    def __init__(self, messages=None):
        self.messages = messages if messages is not None else [{"role": "user", "content": "hello"}]

    def _system_prompt(self):
        return "you are a careful agent"


class TestTheStatusIsReadable:
    def test_it_reports_a_real_window(self):
        status = cb.status_for_agent(FakeAgent())
        # `detect_profile` resolves the model rather than trusting `max_input_tokens` alone, so the window is
        # whatever that resolver decides. Asserted as "a real positive number that leaves room", not as an
        # exact figure -- pinning the exact number would make this test fail the day the table is updated,
        # which teaches nothing about whether the status works.
        assert status.window > 0
        assert status.used > 0
        # Headroom is SMALLER than window-minus-used, because the evaluation also reserves room for the
        # model's own reply. Asserting equality here was wrong the first time and the test caught it -- which
        # is the right outcome: reserving output space is deliberate, since a window with no room to answer in
        # is a window that fails at the moment it matters.
        assert 0 < status.headroom <= status.window - status.used

    def test_it_reports_the_percentage(self):
        assert 0 <= cb.status_for_agent(FakeAgent()).percent <= 100

    def test_it_names_what_the_harness_would_do(self):
        """The agent needs to know the harness is about to act on its behalf."""
        status = cb.status_for_agent(FakeAgent())
        assert status.harness_action in ("ok", "warn", "prune", "compact", "refuse")

    def test_it_states_when_the_count_is_an_estimate(self):
        """A report from an approximation must not imply precision it does not have."""
        status = cb.status_for_agent(FakeAgent())
        if status.confidence == "heuristic":
            text, _ = cb.execute_check_context(FakeAgent())
            assert "character estimate" in text

    def test_a_full_window_reads_as_tight(self):
        agent = FakeAgent([{"role": "tool", "content": "x" * 4_000_000}])
        status = cb.status_for_agent(agent)
        assert status.tight is True or status.harness_action in ("prune", "compact", "refuse")

    def test_it_never_raises_on_a_broken_agent(self):
        """Advice that can crash the thing it advises is worse than no advice."""

        class Broken:
            @property
            def messages(self):
                raise RuntimeError("boom")

            model = "unknown"
            profile = None
            max_input_tokens = None

        status = cb.status_for_agent(Broken())
        # Zero and a verdict explaining it, NOT an exception. When the agent cannot even be inspected there is
        # no number to report, and inventing one would be worse than admitting the budget is unknown.
        assert status.window == 0
        assert "unknown" in status.verdict

    def test_an_unrecognised_model_still_gets_a_conservative_window(self):
        """Better than reporting zero, which would read as 'no budget at all'."""

        class NoProfile:
            model = ""
            profile = None
            max_input_tokens = None
            messages = []

        status = cb.status_for_agent(NoProfile())
        assert status.window > 0, "an unknown model must get the safe default, not nothing"
        assert status.verdict


class TestTheNumbersComeFromTheRealEvaluation:
    def test_it_uses_the_same_evaluation_the_harness_enforces(self):
        """Otherwise the agent decides on a number nobody else uses."""
        from myriaxon.model_profiles import detect_profile

        agent = FakeAgent()
        profile = detect_profile(agent.model, max_input_tokens=agent.max_input_tokens)
        direct = cb.evaluate(agent.messages, profile, system_prompt=agent._system_prompt())
        status = cb.status_for_agent(agent)

        assert status.window == direct.window
        assert status.harness_action == direct.action
        # The used count is the same to within the estimate's own rounding.
        assert abs(status.used - direct.used_tokens) <= 2


class TestSpillingRelocatesRatherThanDiscards:
    def test_a_large_result_is_written_and_a_pointer_returned(self, tmp_path):
        big = "line of build output\n" * 500
        pointer, problem = cb.spill(big, "build-log", workspace=tmp_path)
        assert problem == ""
        assert "Spilled" in pointer
        assert "read_file" in pointer, "the agent must be told how to get it back"
        assert str(tmp_path) in pointer

    def test_the_file_really_contains_the_content(self, tmp_path):
        """A spill the agent cannot read back is a deletion with extra steps."""
        big = "important line\n" * 400
        pointer, _ = cb.spill(big, "log", workspace=tmp_path)
        path = Path(pointer.split("to ")[1].split("\n")[0].strip())
        assert path.exists()
        assert path.read_text(encoding="utf-8") == big

    def test_the_pointer_does_not_contain_the_content(self, tmp_path):
        """The point is that the content leaves the conversation."""
        big = "x" * 10_000
        pointer, _ = cb.spill(big, "big", workspace=tmp_path)
        assert len(pointer) < 600
        assert big not in pointer

    def test_a_small_result_is_not_worth_spilling(self, tmp_path):
        pointer, problem = cb.spill("tiny", "x", workspace=tmp_path)
        assert pointer == ""
        assert "below the" in problem and "costs more than it saves" in problem

    def test_the_threshold_is_a_named_constant(self):
        assert cb.MIN_SPILL_CHARS == 4_000

    def test_the_spill_dir_is_created(self, tmp_path):
        target = tmp_path / "fresh"
        cb.spill("y" * 5_000, "x", workspace=target)
        assert (target / "spill").is_dir()

    def test_the_name_is_sanitised(self, tmp_path):
        """A name from a model is a path component, so it must not be able to escape."""
        pointer, _ = cb.spill("z" * 5_000, "../../evil", workspace=tmp_path)
        assert ".." not in Path(pointer.split("to ")[1].split("\n")[0].strip()).name


class TestTheVerdictIsActionable:
    def test_a_roomy_window_needs_no_action(self):
        status = cb.status_for_agent(FakeAgent())
        assert "No action needed" in status.verdict

    def test_a_refusing_window_says_there_is_no_room(self):
        status = cb.ContextStatus(window=1000, used=1000, headroom=0, harness_action="refuse")
        assert "NO room left" in cb._verdict(status)

    def test_an_impending_action_explains_the_trade(self):
        """Spilling relocates; pruning discards and compacting costs a call."""
        status = cb.ContextStatus(window=1000, used=900, headroom=100, harness_action="prune")
        verdict = cb._verdict(status)
        assert "about to prune" in verdict
        assert "relocates" in verdict

    def test_tight_is_a_named_threshold(self):
        assert cb.TIGHT_HEADROOM_TOKENS == 8_000
        tight = cb.ContextStatus(window=1000, used=995, headroom=5)
        roomy = cb.ContextStatus(window=1_000_000, used=1000, headroom=999_000)
        assert tight.tight is True and roomy.tight is False

    def test_propose_action_suggests_spilling_a_large_result_in_a_filling_context(self):
        status = cb.ContextStatus(window=1000, used=700, headroom=300, percent=70)
        assert "Spill" in cb.propose_action(status, large_result_chars=10_000)

    def test_propose_action_says_nothing_when_there_is_room(self):
        status = cb.ContextStatus(window=1_000_000, used=1000, headroom=999_000, percent=1,
                                  harness_action="ok")
        assert cb.propose_action(status) == ""


class TestTheTools:
    def test_check_context_returns_a_readable_block(self):
        text, err = cb.execute_check_context(FakeAgent())
        assert err is False
        assert "Context:" in text and "remaining" in text

    def test_check_context_without_an_agent_reports_rather_than_crashes(self):
        text, err = cb.execute_check_context(None)
        assert err is True and "No agent context" in text

    def test_spill_through_the_tool_surface(self, tmp_path):
        text, err = cb.execute_spill({"text": "q" * 6_000, "name": "out"}, workspace=tmp_path)
        assert err is False and "Spilled" in text

    def test_a_too_small_spill_is_reported_as_an_error(self, tmp_path):
        text, err = cb.execute_spill({"text": "small"}, workspace=tmp_path)
        assert err is True

    def test_the_tool_defs_are_real_schema(self):
        names = {d["name"] for d in cb.CONTEXT_TOOL_DEFS}
        assert names == {"check_context", "spill"}
        for definition in cb.CONTEXT_TOOL_DEFS:
            assert "input_schema" in definition


class TestItDoesNotReplaceAutomaticManagement:
    def test_the_automatic_path_still_exists(self):
        """An unattended run has nobody to ask, so waiting for a decision never made would run out of context."""
        assert hasattr(cb, "evaluate")
        assert hasattr(cb, "plan_prune")
        assert hasattr(cb, "shrink_tool_result")

    def test_spilling_performs_no_context_mutation(self, tmp_path):
        """It writes a file and returns a pointer. Removing the content is the caller's decision."""
        messages = [{"role": "tool", "content": "x" * 10_000}]
        before = len(messages[0]["content"])
        cb.spill(messages[0]["content"], "x", workspace=tmp_path)
        assert len(messages[0]["content"]) == before, "spill must not mutate the caller's messages"
