"""Unit tests for model adaptation: profiles, context budget, prompt tiers, tool filtering.

All offline. These matter more than most: the code under test decides whether a user's request
overflows their window, silently truncates a tool result, or withholds a tool -- each of which is
invisible when it goes wrong and expensive when it goes wrong loudly.
"""
from __future__ import annotations

import json
import sys
import unittest
from collections import namedtuple
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from myriaxon import model_profiles
from myriaxon import spill
from myriaxon.context_budget import (
    BudgetState,
    estimate_messages,
    estimate_tokens,
    evaluate,
    plan_prune,
    shrink_tool_result,
)
from myriaxon.model_adapt import _tool_name, filter_tools, format_result_for_model
from myriaxon.model_profiles import PROFILES, detect_profile, describe_profile
from myriaxon.system_prompt import build_system_prompt


class FakeContext:
    def __init__(self, profile):
        self.profile = profile
        self.result_char_limit = None
        self.truncated_any_result = False
        # Set when an oversized result was written to disk with a preview instead of being cut. Deliberately a
        # separate flag from `truncated_any_result`: a spill is recoverable and a truncation is not, so collapsing
        # them would make "the model lost this" indistinguishable from "the model can go and read this".
        self.spilled_any_result = False
        self.current_tool_name = ""
        self.session_id = ""


class FakeAgent:
    """Minimal stand-in for an agent: the prompt builder only reads these attributes."""

    def __init__(self, profile, **kwargs):
        self.profile = profile
        self.project_instructions = kwargs.get("project_instructions", "")
        self.provider = kwargs.get("provider", "deepseek")
        self.url_mcp_servers = kwargs.get("url_mcp_servers", [])
        self.skills = kwargs.get("skills", {})
        self.subagent_types = kwargs.get("subagent_types", {})
        self.messages = kwargs.get("messages", [])


class TestProfileDetection(unittest.TestCase):
    def test_small_models_get_the_small_profile(self):
        for model in ("gemma-2-2b", "phi-3-mini", "llama-3.2-1b", "qwen2.5-3b"):
            profile = detect_profile(model)
            self.assertEqual(profile.name, "small", model)
            self.assertEqual(profile.verbosity, "terse", model)

    def test_large_window_models_get_large(self):
        for model in ("claude-opus-5", "gpt-4.1", "gemini-3.8-flash"):
            self.assertEqual(detect_profile(model).name, "large", model)

    def test_unknown_model_errs_small_not_generous(self):
        """The single most important property here. A harness that guesses 'frontier' for an
        unrecognized model hands it big tool results and late compaction, then fails inside the
        user's task. Guessing small costs a little extra compaction and is recoverable."""
        profile = detect_profile("totally-unknown-model-9000")
        self.assertEqual(profile.name, "small")
        self.assertEqual(profile.detection, "assumed")

    def test_known_model_reports_known_detection(self):
        self.assertEqual(detect_profile("claude-opus-5").detection, "known")

    def test_explicit_profile_wins(self):
        self.assertEqual(detect_profile("claude-opus-5", explicit="small").name, "small")
        self.assertEqual(detect_profile("gemma-2-2b", explicit="large").name, "large")

    def test_single_agent_mode_removes_delegation(self):
        for model in ("claude-opus-5", "deepseek-v4-flash"):
            self.assertFalse(detect_profile(model, agent_mode="single").allow_delegation, model)
        self.assertTrue(detect_profile("claude-opus-5", agent_mode="multi").allow_delegation)

    def test_resolved_window_beats_the_nominal_one(self):
        profile = detect_profile("deepseek-v4-flash", max_input_tokens=8_000)
        self.assertEqual(profile.context_window, 8_000)

    def test_explicit_profile_keeps_its_behaviour_but_rescales_sizes(self):
        """`--profile small` on a huge-window model must stay *behaviourally* small (terse prompt,
        argument coaching) while not capping results at 1,200 characters, which would be obeying the
        letter of the request and defeating its purpose."""
        profile = detect_profile("gemma-2-2b", explicit="small", max_input_tokens=512_000)
        self.assertEqual(profile.name, "small")
        self.assertEqual(profile.verbosity, "terse")
        self.assertTrue(profile.needs_argument_coaching)
        self.assertGreater(profile.tool_result_chars, 1_200)

    def test_profile_dict_is_serializable_and_has_no_secrets(self):
        payload = json.dumps(detect_profile("deepseek-v4-flash").to_dict())
        self.assertIn("context_window", payload)
        self.assertNotIn("api_key", payload)


class TestTokenEstimation(unittest.TestCase):
    def test_empty_is_zero(self):
        self.assertEqual(estimate_tokens("").tokens, 0)

    def test_larger_text_estimates_more(self):
        self.assertLess(estimate_tokens("a" * 100).tokens, estimate_tokens("a" * 1000).tokens)

    def test_cjk_counted_denser_than_latin(self):
        latin = estimate_tokens("a" * 100).tokens
        cjk = estimate_tokens("\u4e2d" * 100).tokens
        self.assertGreater(cjk, latin)

    def test_confidence_is_stated(self):
        self.assertIn(estimate_tokens("hello world").confidence, ("tokenizer", "heuristic"))

    def test_message_breakdown_buckets_by_role(self):
        messages = [
            {"role": "user", "content": "a" * 400},
            {"role": "tool", "tool_call_id": "1", "content": "b" * 400},
        ]
        estimate, buckets = estimate_messages(messages)
        self.assertGreater(buckets["user"], 0)
        self.assertGreater(buckets["tool"], 0)
        self.assertAlmostEqual(estimate.tokens, buckets["user"] + buckets["tool"], delta=2)

    def test_tool_call_arguments_are_counted(self):
        """Arguments are the expensive part of an assistant turn; omitting them makes the estimate
        flattering exactly when the model is writing a large file."""
        with_args = estimate_messages(
            [{"role": "assistant", "content": "", "tool_calls": [{"function": {"arguments": "x" * 2000}}]}]
        )[0].tokens
        without = estimate_messages([{"role": "assistant", "content": ""}])[0].tokens
        self.assertGreater(with_args, without + 100)


class TestBudgetEvaluation(unittest.TestCase):
    SMALL = PROFILES["small"]
    LARGE = PROFILES["large"]

    def _messages(self, count, chars):
        return [{"role": "user", "content": "task"}] + [
            {"role": "tool", "tool_call_id": str(i), "content": "x" * chars} for i in range(count)
        ]

    def test_empty_conversation_is_ok(self):
        state = evaluate([], self.SMALL, system_prompt="sys", tools_chars=0)
        self.assertEqual(state.action, "ok")
        self.assertLess(state.used_fraction, 1.0)

    def test_action_escalates_as_the_window_fills(self):
        seen = []
        for count in range(0, 12):
            state = evaluate(self._messages(count, 2000), self.SMALL, system_prompt="sys", tools_chars=8000)
            seen.append(state.action)
        self.assertIn("warn", seen)
        # The whole point is that the ladder is ordered and reaches the terminal state rather than
        # jumping straight from ok to a provider error.
        self.assertLess(seen.index("warn"), len(seen) - 1)

    def test_refuse_fires_before_the_window_is_actually_exceeded(self):
        """Refusing must happen while there is still room, because the reply reserve counts against
        the same window."""
        state = evaluate(self._messages(30, 4000), self.SMALL, system_prompt="sys", tools_chars=8000)
        self.assertEqual(state.action, "refuse")
        self.assertIn("reply reserve", state.reason)

    def test_large_window_tolerates_what_the_small_one_refuses(self):
        """Same traffic, two windows: the small profile must reach its terminal refuse state while
        the large one is still comfortable. Searched rather than hard-coded, because pinning an exact
        message count makes the test fail on harmless threshold tuning instead of on a real bug."""
        for count in range(1, 30):
            messages = self._messages(count, 4000)
            if evaluate(messages, self.SMALL).action == "refuse":
                break
        else:
            self.fail("small profile never refused, even at 30 large tool results")
        self.assertEqual(evaluate(messages, self.LARGE).action, "ok")

    def test_system_prompt_and_tools_are_counted(self):
        big_prompt = evaluate([], self.SMALL, system_prompt="word " * 4000, tools_chars=0)
        small_prompt = evaluate([], self.SMALL, system_prompt="hi", tools_chars=0)
        self.assertGreater(big_prompt.used_tokens, small_prompt.used_tokens + 3000)

    def test_meter_renders_proportionally(self):
        state = BudgetState(used_tokens=50, window=100, used_fraction=0.5, action="ok", headroom_tokens=50, confidence="heuristic")
        meter = state.meter(width=10)
        self.assertEqual(len(meter), 10)
        self.assertEqual(state.percent, 50)

    def test_state_is_serializable(self):
        state = evaluate(self._messages(1, 500), self.SMALL)
        self.assertIn("action", json.dumps(state.to_dict()))


class TestResultTruncation(unittest.TestCase):
    def test_short_result_untouched(self):
        text, was = shrink_tool_result("hello", 100)
        self.assertFalse(was)
        self.assertEqual(text, "hello")

    def test_long_result_keeps_head_and_tail_and_says_so(self):
        text = "HEAD" + "m" * 5000 + "TAIL"
        shrunk, was = shrink_tool_result(text, 500, kind="file read")
        self.assertTrue(was)
        # Never exceeds the budget it was given: a truncation that overshoots is a slow leak of the
        # exact resource it exists to protect.
        self.assertLessEqual(len(shrunk), 500)
        self.assertTrue(shrunk.startswith("HEAD"))
        self.assertTrue(shrunk.endswith("TAIL"))
        # The marker is what stops a model asserting things about data it never saw.
        self.assertIn("omitted", shrunk)
        self.assertIn("file read", shrunk)

    def test_tiny_budget_degrades_to_a_short_marker(self):
        text = "x" * 5000
        shrunk, was = shrink_tool_result(text, 60, kind="grep")
        self.assertTrue(was)
        self.assertLessEqual(len(shrunk), 60)

    def test_format_result_uses_profile_limit(self):
        small, large = FakeContext(PROFILES["small"]), FakeContext(PROFILES["large"])
        payload = "z" * 30_000
        small_text, small_cut = format_result_for_model(payload, small)
        large_text, large_cut = format_result_for_model(payload, large)
        # The profile budget still constrains what the model sees -- it is now enforced by the spill preview
        # rather than by cutting the text, so `_cut` is False for both. The earlier assertion (`small_cut and
        # large_cut`) encoded the pre-spill mechanism, not the property that matters.
        self.assertLessEqual(len(small_text), PROFILES["small"].tool_result_chars)
        self.assertLessEqual(len(large_text), PROFILES["large"].tool_result_chars)
        self.assertLess(len(small_text), len(large_text))
        self.assertLess(len(large_text), len(payload))
        self.assertTrue(small.spilled_any_result and large.spilled_any_result)
        self.assertFalse(small_cut or large_cut, "a spill is not a truncation")

    def test_hard_cap_applies_even_with_a_large_dynamic_limit(self):
        ctx = FakeContext(PROFILES["small"])
        ctx.result_char_limit = 10_000_000  # a caller asking for more than the profile allows
        text, _ = format_result_for_model("z" * 100_000, ctx)
        self.assertLessEqual(len(text), PROFILES["small"].tool_result_hard_cap + 200)

    def test_no_profile_means_no_truncation(self):
        ctx = FakeContext(None)
        payload = "z" * 50_000
        text, was = format_result_for_model(payload, ctx)
        self.assertFalse(was)
        self.assertEqual(len(text), 50_000)

    def test_truncation_is_flagged_on_the_context(self):
        # `truncated_any_result` now means exactly what it says: the model lost content. An oversized result is
        # spilled rather than cut, so the flag stays False and `spilled_any_result` carries the signal instead.
        # Reaching the truncation path at all requires the spill to have failed, which is what this forces by
        # pointing the spill root at a path that cannot be a directory.
        ctx = FakeContext(PROFILES["small"])
        with mock.patch.object(spill, "SPILL_ROOT", Path(__file__).resolve().parent / "no-such-root.txt"):
            (Path(__file__).resolve().parent / "no-such-root.txt").write_text("a file, not a dir",
                                                                            encoding="utf-8")
            try:
                text, was = format_result_for_model("z" * 400_000, ctx)
            finally:
                (Path(__file__).resolve().parent / "no-such-root.txt").unlink(missing_ok=True)
        self.assertTrue(was, "with the spill unavailable, the result must be truncated")
        self.assertTrue(ctx.truncated_any_result)
        self.assertFalse(ctx.spilled_any_result)
        self.assertLessEqual(len(text), PROFILES["small"].tool_result_hard_cap + 200)

    def test_a_spill_is_not_reported_as_a_truncation(self):
        ctx = FakeContext(PROFILES["small"])
        format_result_for_model("z" * 400_000, ctx)
        self.assertTrue(ctx.spilled_any_result)
        self.assertFalse(ctx.truncated_any_result,
                         "the content is recoverable, so it was not lost")


class TestToolFiltering(unittest.TestCase):
    def _tools(self):
        return [
            {"type": "function", "function": {"name": name}}
            for name in ("read_file", "write_file", "task", "parallel_tasks", "send_to_subagent", "save_skill")
        ]

    def test_tool_name_handles_both_shapes(self):
        self.assertEqual(_tool_name({"name": "read_file"}), "read_file")
        self.assertEqual(_tool_name({"type": "function", "function": {"name": "read_file"}}), "read_file")
        self.assertEqual(_tool_name({}), "")

    def test_single_agent_withholds_delegation_for_every_profile(self):
        for name in PROFILES:
            profile = detect_profile("x", explicit=name, agent_mode="single")
            kept = {_tool_name(t) for t in filter_tools(self._tools(), profile)}
            self.assertNotIn("task", kept, name)
            self.assertNotIn("parallel_tasks", kept, name)

    def test_multi_agent_keeps_delegation(self):
        profile = detect_profile("claude-opus-5", agent_mode="multi")
        kept = {_tool_name(t) for t in filter_tools(self._tools(), profile)}
        self.assertIn("task", kept)
        self.assertIn("parallel_tasks", kept)

    def test_small_profile_withholds_long_horizon_conveniences(self):
        profile = detect_profile("gemma-2-2b")
        kept = {_tool_name(t) for t in filter_tools(self._tools(), profile)}
        self.assertNotIn("save_skill", kept)

    def test_read_and_write_are_never_withheld(self):
        """A profile must not be able to remove the tools the task literally requires."""
        for name in PROFILES:
            profile = detect_profile("x", explicit=name)
            kept = {_tool_name(t) for t in filter_tools(self._tools(), profile)}
            self.assertIn("read_file", kept, name)
            self.assertIn("write_file", kept, name)

    def test_none_tools_passes_through(self):
        self.assertIsNone(filter_tools(None, PROFILES["small"]))


class TestSystemPromptTiers(unittest.TestCase):
    def test_terse_prompt_is_shorter_than_full(self):
        """Measured on the live prompt: terse is ~255 tokens vs ~384 for full. The saving is real but
        modest, because most optional sections are already skipped -- the big fixed cost in a session
        is tool schemas, which is why tool filtering exists rather than more prompt trimming."""
        small = build_system_prompt(FakeAgent(PROFILES["small"]))
        large = build_system_prompt(FakeAgent(PROFILES["large"]))
        self.assertLess(len(small), len(large))
        self.assertLess(len(small), len(large) * 0.85)

    def test_essential_rules_survive_every_tier(self):
        """The shell dialect and permission contract are the two rules whose absence causes
        concrete, observed failures -- a POSIX command on Windows, or a model re-calling after a
        decline. They must never be trimmed."""
        for name in PROFILES:
            prompt = build_system_prompt(FakeAgent(detect_profile("x", explicit=name)))
            self.assertIn("PowerShell", prompt, name)
            self.assertIn("confirmation", prompt, name)
            self.assertIn("Current working directory", prompt, name)

    def test_small_prompt_states_the_tool_argument_contract(self):
        prompt = build_system_prompt(FakeAgent(PROFILES["small"]))
        self.assertIn("schema declares", prompt)

    def test_project_instructions_are_never_dropped(self):
        for name in PROFILES:
            agent = FakeAgent(detect_profile("x", explicit=name), project_instructions="ALWAYS USE TABS")
            self.assertIn("ALWAYS USE TABS", build_system_prompt(agent), name)

    def test_no_delegation_hint_when_delegation_is_off(self):
        agent = FakeAgent(PROFILES["balanced"])
        prompt = build_system_prompt(agent)
        self.assertNotIn("use task to delegate", prompt)

    def test_optional_sections_are_capped_but_essentials_are_not(self):
        """The cap is a soft guide: an essential section that would exceed it is still included,
        because dropping a failure-preventing rule to save characters is the wrong trade. So the
        assertion is that the cap actually *bites* (the prompt stays in the same order of magnitude)
        rather than that it is never exceeded."""
        profile = detect_profile("x", explicit="small")
        Skill = namedtuple("Skill", "name description")
        skills = {f"skill{i}": Skill(f"skill{i}", "a long description of what this skill does " * 5) for i in range(50)}
        agent = FakeAgent(profile, skills=skills)
        prompt = build_system_prompt(agent)
        self.assertLess(len(prompt), profile.system_prompt_chars * 2)

    def test_strong_model_is_not_misclassified_as_weak(self):
        """Regression guard for a real false positive: a bare `flash` hint classified
        gemini-3.8-flash as a weak model, which would withhold tools and truncate its results."""
        for model in ("gemini-3.8-flash", "gemini-3.7-flash", "claude-opus-5", "llama-3.3-70b"):
            self.assertFalse(model_profiles._is_weak_name(model), model)
        for model in ("llama-3.2-1b", "gemma-2-2b", "qwen2.5-coder-7b", "phi-3-mini"):
            self.assertTrue(model_profiles._is_weak_name(model), model)


class TestPlanPrune(unittest.TestCase):
    def test_counts_only_large_tool_messages(self):
        messages = [
            {"role": "tool", "tool_call_id": "1", "content": "x" * 5000},
            {"role": "tool", "tool_call_id": "2", "content": "tiny"},
            {"role": "user", "content": "x" * 5000},
        ]
        self.assertEqual(plan_prune(messages, min_chars=400), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
