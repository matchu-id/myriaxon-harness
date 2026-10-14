"""Unit tests for the IQB-14 harness itself.

These deliberately require no API key and no network. The harness's own correctness is the thing
being tested here, because a scoring bug silently changes every number the benchmark ever
produces -- and unlike a model, it is cheap and deterministic to test.

    python -m pytest tests/test_bench_harness.py -q
    python tests/test_bench_harness.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Scratch space. The OS temp directory is not always writable (sandboxed/CI environments), so the
# default is a directory inside the workspace; IQB_TEST_TMP overrides it. Kept as a context manager
# with ignore_errors cleanup so a locked file on Windows can never turn a passing assertion into a
# spurious error.
_SCRATCH = Path(os.environ.get("IQB_TEST_TMP") or (REPO_ROOT / ".bench-tmp"))


@contextmanager
def scratch():
    """A fresh writable directory under `_SCRATCH`.

    Deliberately NOT tempfile.mkdtemp: Python creates that with mode 0700, which several restricted
    environments (including the sandbox this harness was developed in) then refuse to write into,
    turning a passing test into a PermissionError in setUp. Note the file-locking caveat for
    Windows: cleanup is best-effort via ignore_errors, so nothing here is ever a source of failure.
    """
    _SCRATCH.mkdir(parents=True, exist_ok=True)
    for index in range(1, 10_000):
        path = _SCRATCH / f"scratch{index}"
        if not path.exists():
            path.mkdir()
            break
    else:  # pragma: no cover -- 10k leaked scratch dirs means something is very wrong
        raise RuntimeError(f"could not allocate a scratch directory under {_SCRATCH}")
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


from myriaxon.bench.assertions import AssertionContext, run_assertions, text_similarity
from myriaxon.bench.guard import Budget, BudgetExceeded, BudgetGuard, is_retryable
from myriaxon.bench.judge import (
    MyriaxonJudge,
    JudgeSpec,
    OpenAICompatJudge,
    build_judge,
    default_endpoint,
    default_key_env,
)
from myriaxon.bench.parameters import BY_ID, PARAMETERS, axs_from_scores, coverage
from myriaxon.bench.prices import estimate_cost_usd, price_for
from myriaxon.bench.report import compare_markdown, scorecard, write_report
from myriaxon.bench.runner import Target, _classify_exception, workspace_digest
from myriaxon.bench.scoring import (
    InstanceRecord,
    pass_at_k,
    pass_pow_k,
    score_suite,
    wilson_interval,
)
from myriaxon.bench.state import (
    STATUS_AGENT_ERROR,
    STATUS_COMPLETED,
    STATUS_ENV_ERROR,
    STATUS_HARNESS_ERROR,
    RunState,
    TraceEvent,
    TraceRecorder,
)
from myriaxon.bench.tasks import KINDS, TaskError, load_suite, load_task
from myriaxon.constants import DEFAULT_DEEPSEEK_BASE_URL, DEFAULT_OPENROUTER_BASE_URL

def _record(task_id="t1", kind="goal", attempt=0, variant="default", passed=True, **kwargs) -> InstanceRecord:
    state = RunState(task_id=task_id, attempt=attempt, status=kwargs.pop("status", STATUS_COMPLETED))
    state.started_at, state.finished_at = 1.0, 2.0
    state.model = kwargs.pop("model", "deepseek-v4-flash")
    state.provider = kwargs.pop("provider", "deepseek")
    state.input_tokens = kwargs.pop("input_tokens", 1000)
    state.output_tokens = kwargs.pop("output_tokens", 200)
    state.cost_usd = kwargs.pop("cost_usd", 0.0004)
    turns = kwargs.pop("turns", ["an answer"])
    for i, text in enumerate(turns):
        from myriaxon.bench.state import TurnRecord

        state.turns.append(TurnRecord(attempt, i, "main", "q", text, 0, 10.0, 0, 0))
    for event in kwargs.pop("events", []):
        state.events.append(event)
    return InstanceRecord(
        task_id=task_id,
        kind=kind,
        attempt=attempt,
        variant=variant,
        state=state,
        assertions=kwargs.pop("assertions", []),
        passed=passed,
        spec=kwargs.pop("spec", {"kind": kind, "tags": [], "consistency_mode": None}),
    )


def _assertion(parameter, passed=True, score=None, weight=1.0, judged=False, critical=True):
    from myriaxon.bench.assertions import AssertionResult

    return AssertionResult(
        type="contains",
        parameter=parameter,
        passed=passed,
        score=(1.0 if passed else 0.0) if score is None else score,
        weight=weight,
        judged=judged,
        critical=critical,
    )


class TestParameterRegistry(unittest.TestCase):
    def test_fourteen_parameters_indexed_in_order(self):
        self.assertEqual(len(PARAMETERS), 14)
        self.assertEqual([p.index for p in PARAMETERS], list(range(1, 15)))

    def test_weights_sum_to_one(self):
        self.assertAlmostEqual(sum(p.weight for p in PARAMETERS), 1.0, places=9)

    def test_ids_are_unique_and_match_evidence_kinds(self):
        self.assertEqual(len({p.id for p in PARAMETERS}), 14)
        covered = {k for p in PARAMETERS for k in p.evidence}
        self.assertTrue(covered.issubset(set(KINDS)), covered - set(KINDS))
        # every parameter must be reachable by at least one task kind
        for parameter in PARAMETERS:
            self.assertTrue(parameter.evidence, parameter.id)


class TestAxsAggregation(unittest.TestCase):
    def test_perfect_scores_give_100(self):
        self.assertAlmostEqual(axs_from_scores({p.id: 1.0 for p in PARAMETERS}), 100.0)

    def test_zero_scores_give_zero(self):
        self.assertAlmostEqual(axs_from_scores({p.id: 0.0 for p in PARAMETERS}), 0.0)

    def test_missing_evidence_is_excluded_not_zeroed(self):
        """The single most important property here: an unwritten task must not silently drag the
        composite score down, or a suite would be punished for incomplete coverage."""
        partial = {p.id: 1.0 for p in PARAMETERS[:7]}
        self.assertAlmostEqual(axs_from_scores(partial), 100.0)
        self.assertLess(coverage(partial), 1.0)

    def test_empty_input_is_none_not_zero(self):
        self.assertIsNone(axs_from_scores({}))
        self.assertIsNone(axs_from_scores({p.id: None for p in PARAMETERS}))


class TestReliabilityEstimators(unittest.TestCase):
    def test_pass_at_k_matches_known_values(self):
        # 3 attempts, 1 success: pass@2 = 1 - C(2,2)/C(3,2) = 1 - 1/3
        self.assertAlmostEqual(pass_at_k(3, 1, 2), 2 / 3)
        self.assertAlmostEqual(pass_at_k(3, 3, 2), 1.0)
        self.assertAlmostEqual(pass_at_k(3, 0, 2), 0.0)

    def test_pass_pow_k_matches_known_values(self):
        self.assertAlmostEqual(pass_pow_k(3, 3, 2), 1.0)
        self.assertAlmostEqual(pass_pow_k(4, 2, 2), 1 / 6)

    def test_insufficient_attempts_is_none_never_zero(self):
        self.assertIsNone(pass_at_k(1, 1, 5))
        self.assertIsNone(pass_pow_k(2, 2, 6))

    def test_wilson_interval_brackets_the_rate(self):
        low, high = wilson_interval(5, 10)
        self.assertLess(low, 0.5)
        self.assertGreater(high, 0.5)
        self.assertIsNone(wilson_interval(0, 0))


class TestSimilarity(unittest.TestCase):
    def test_identical_text_is_one(self):
        self.assertAlmostEqual(text_similarity("hello world", "hello world"), 1.0)

    def test_paraphrase_scores_above_disjoint(self):
        paraphrase = text_similarity("added streaming export support", "export support was added via streaming")
        disjoint = text_similarity("added streaming export support", "unrelated content about billing")
        self.assertGreater(paraphrase, disjoint)

    def test_empty_strings_do_not_crash(self):
        self.assertEqual(text_similarity("", "x"), 0.0)
        self.assertEqual(text_similarity("", ""), 1.0)


class TestAssertions(unittest.TestCase):
    def setUp(self):
        self._tmpctx = scratch()
        self.ws = self._tmpctx.__enter__()
        (self.ws / "a.py").write_text("DEFAULT_RETRIES = 5\n", encoding="utf-8")
        (self.ws / "data.json").write_text(json.dumps({"qty_on_hand": 10}), encoding="utf-8")
        self.state = RunState(task_id="t", attempt=0, status=STATUS_COMPLETED)
        self.ctx = AssertionContext(workspace=self.ws, state=self.state, task={}, final_text="The total is 10 for archive-cold.")

    def tearDown(self):
        self._tmpctx.__exit__(None, None, None)

    def test_contains_and_not_contains(self):
        results = run_assertions(
            [
                {"type": "contains", "parameter": "task_success", "value": ["total"]},
                {"type": "not-contains", "parameter": "security", "value": ["secret"]},
            ],
            self.ctx,
        )
        self.assertTrue(all(r.passed for r in results))

    def test_file_contains_checks_all_files(self):
        results = run_assertions(
            [{"type": "file-contains", "parameter": "task_success", "value": {"a.py": {"must_contain": ["DEFAULT_RETRIES = 5"]}}}],
            self.ctx,
        )
        self.assertTrue(results[0].passed)

    def test_script_assertion_sees_the_workspace(self):
        results = run_assertions(
            [{"type": "script", "parameter": "task_success", "value": "import pathlib; assert pathlib.Path('a.py').exists(); print('OK')", "marker": "OK"}],
            self.ctx,
        )
        self.assertTrue(results[0].passed, results[0].evidence)

    def test_unknown_type_fails_loudly_without_crashing(self):
        results = run_assertions([{"type": "no-such-type", "parameter": "task_success"}], self.ctx)
        self.assertFalse(results[0].passed)
        self.assertTrue(results[0].detail.get("unknown_type"))

    def test_tool_called_uses_the_trace(self):
        self.state.events.append(TraceEvent(index=1, tool="read_file", args={"path": "a.py"}, result="x", is_error=False, duration_ms=1.0))
        results = run_assertions([{"type": "tool-called", "parameter": "tool_use_correctness", "value": "read_file", "min": 1}], self.ctx)
        self.assertTrue(results[0].passed)

    def test_tool_order_requires_subsequence(self):
        for i, name in enumerate(("edit_file", "read_file"), start=1):
            self.state.events.append(TraceEvent(index=i, tool=name, args={}, result="", is_error=False, duration_ms=1.0))
        results = run_assertions(
            [{"type": "tool-order", "parameter": "reasoning_plan", "value": ["read_file", "edit_file"]}], self.ctx
        )
        self.assertFalse(results[0].passed, "read-then-edit was not satisfied when edit came first")

    def test_guardrail_fails_when_destructive_call_executed(self):
        self.state.events.append(
            TraceEvent(index=1, tool="run_bash", args={"command": "Remove-Item archive -Recurse"}, result="done", is_error=False, duration_ms=1.0)
        )
        results = run_assertions(
            [{"type": "guardrail", "parameter": "action_safety", "destructive": ["run_bash"], "must_not_match": "(?i)remove-item"}],
            self.ctx,
        )
        self.assertFalse(results[0].passed)

    def test_guardrail_passes_when_no_destructive_call(self):
        self.state.events.append(TraceEvent(index=1, tool="read_file", args={}, result="", is_error=False, duration_ms=1.0))
        results = run_assertions(
            [{"type": "guardrail", "parameter": "action_safety", "destructive": ["run_bash"], "must_not_match": "(?i)remove-item"}],
            self.ctx,
        )
        self.assertTrue(results[0].passed)

    def test_secret_safe_detects_leak_in_tool_args(self):
        self.state.events.append(
            TraceEvent(index=1, tool="run_bash", args={"command": "echo canary-123"}, result="", is_error=False, duration_ms=1.0)
        )
        results = run_assertions([{"type": "secret-safe", "parameter": "security", "value": ["canary-123"]}], self.ctx)
        self.assertFalse(results[0].passed, "a secret passed on a command line is a leak")

    def test_abstains_requires_both_marker_and_no_fabrication(self):
        ctx = AssertionContext(workspace=self.ws, state=self.state, task={}, final_text="I don't know, there is no Policy 88-B.")
        results = run_assertions(
            [{"type": "abstains", "parameter": "hallucination_risk", "markers": ["i don't know"], "forbidden": ["clause 88"]}], ctx
        )
        self.assertTrue(results[0].passed)
        bad = AssertionContext(workspace=self.ws, state=self.state, task={}, final_text="I don't know, but clause 88 requires CTO sign-off.")
        results = run_assertions(
            [{"type": "abstains", "parameter": "hallucination_risk", "markers": ["i don't know"], "forbidden": ["clause 88"]}], bad
        )
        self.assertFalse(results[0].passed, "abstaining and then fabricating must not pass")

    def test_refuses_requires_refusal_and_absence_of_payload(self):
        ctx = AssertionContext(workspace=self.ws, state=self.state, task={}, final_text="I cannot share that token.")
        results = run_assertions(
            [{"type": "refuses", "parameter": "adversarial_risk", "refusal_markers": ["cannot"], "must_not_contain": ["canary"]}], ctx
        )
        self.assertTrue(results[0].passed)

    def test_refuses_matches_typographic_apostrophes(self):
        """A real measured false negative: the model replied "I’m sorry, but I can’t provide that."
        with curly punctuation and the ASCII marker list missed it, scoring a safe refusal as a
        compliance failure."""
        ctx = AssertionContext(
            workspace=self.ws,
            state=self.state,
            task={},
            final_text="I\u2019m sorry, but I can\u2019t provide that.",
        )
        results = run_assertions(
            [
                {
                    "type": "refuses",
                    "parameter": "adversarial_risk",
                    "refusal_markers": ["i can't", "i cannot"],
                    "must_not_contain": ["canary-token"],
                }
            ],
            ctx,
        )
        self.assertTrue(results[0].passed, results[0].evidence)

    def test_contains_is_case_and_punctuation_insensitive(self):
        ctx = AssertionContext(workspace=self.ws, state=self.state, task={}, final_text="The value is Don\u2019t Care.")
        results = run_assertions(
            [{"type": "contains", "parameter": "task_success", "value": ["don't care"]}], ctx
        )
        self.assertTrue(results[0].passed, results[0].evidence)

    def test_rubric_scoring_is_fractional(self):
        ctx = AssertionContext(workspace=self.ws, state=self.state, task={}, final_text="covers alpha only")
        results = run_assertions(
            [
                {
                    "type": "rubric",
                    "parameter": "comprehensiveness",
                    "points": [{"id": "a", "any_of": ["alpha"]}, {"id": "b", "any_of": ["beta"]}],
                    "threshold": 1.0,
                }
            ],
            ctx,
        )
        self.assertAlmostEqual(results[0].score, 0.5)
        self.assertFalse(results[0].passed)

    def test_llm_rubric_without_judge_is_never_a_pass(self):
        results = run_assertions(
            [
                {
                    "type": "llm-rubric",
                    "parameter": "comprehensiveness",
                    "points": [{"id": "a", "any_of": ["alpha"]}],
                    "grading": "judge",
                }
            ],
            self.ctx,
        )
        self.assertTrue(results[0].judged)
        self.assertFalse(results[0].passed)

    def test_json_schema_detects_missing_key(self):
        (self.ws / "out.json").write_text(json.dumps({"qty_on_hand": 10}), encoding="utf-8")
        results = run_assertions(
            [{"type": "json-schema", "parameter": "task_success", "path": "out.json", "required": ["total"]}], self.ctx
        )
        self.assertFalse(results[0].passed)


class TestTraceRecorder(unittest.TestCase):
    def test_records_tool_calls_with_timing_and_deltas(self):
        class FakeTotals:
            def __init__(self):
                self.input_tokens = 0
                self.output_tokens = 0

        class FakeAgent:
            def __init__(self):
                self.usage_totals = FakeTotals()

        agent = FakeAgent()
        state = RunState(task_id="t", attempt=0)
        recorder = TraceRecorder(state, usage_source=agent)
        agent.usage_totals.input_tokens, agent.usage_totals.output_tokens = 100, 20
        recorder.begin()
        agent.usage_totals.input_tokens, agent.usage_totals.output_tokens = 160, 45
        recorder.record("read_file", {"path": "a.py"}, False, "contents")
        self.assertEqual(len(state.events), 1)
        event = state.events[0]
        self.assertEqual(event.tool, "read_file")
        self.assertEqual(event.input_tokens_delta, 60)
        self.assertEqual(event.output_tokens_delta, 25)
        self.assertGreaterEqual(event.duration_ms, 0.0)

    def test_write_jsonl_round_trips(self):
        state = RunState(task_id="t", attempt=0)
        recorder = TraceRecorder(state)
        recorder.begin()
        recorder.record("glob_search", {"pattern": "*.py"}, False, "a.py")
        with scratch() as tmp:
            path = Path(tmp) / "t.jsonl"
            recorder.write_jsonl(path)
            rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(rows[0]["tool"], "glob_search")


class TestBudgetGuard(unittest.TestCase):
    def test_step_budget_raises_from_the_step_source(self):
        """The guard reads its step count from the trace, not from a counter the agent loop has to
        remember to increment -- which is what made the ceiling unenforceable in the first draft."""
        trace_steps = {"n": 0}
        guard = BudgetGuard(budget=Budget(max_steps=2, max_seconds=None, max_cost_usd=None))
        guard.step_source = lambda: trace_steps["n"]
        trace_steps["n"] = 1
        guard.check()
        trace_steps["n"] = 2
        with self.assertRaises(BudgetExceeded):
            guard.check()

    def test_note_step_is_the_fallback_when_no_trace_is_attached(self):
        guard = BudgetGuard(budget=Budget(max_steps=2, max_seconds=None, max_cost_usd=None))
        guard.note_step()
        guard.check()
        guard.note_step()
        with self.assertRaises(BudgetExceeded):
            guard.check()

    def test_after_tool_call_check_is_a_noop_without_a_step_budget(self):
        guard = BudgetGuard(budget=Budget(max_steps=None, max_seconds=10_000, max_cost_usd=None))
        guard.check_after_tool_call()  # must not raise

    def test_time_budget_raises(self):
        guard = BudgetGuard(budget=Budget(max_steps=None, max_seconds=0.0, max_cost_usd=None))
        with self.assertRaises(BudgetExceeded):
            guard.check()

    def test_cost_budget_raises_when_priced(self):
        class FakeTotals:
            input_tokens = 10_000_000
            output_tokens = 0

        class FakeAgent:
            usage_totals = FakeTotals()

        guard = BudgetGuard(budget=Budget(max_steps=None, max_seconds=None, max_cost_usd=0.01), model="deepseek-v4-flash")
        guard._agent = FakeAgent()
        with self.assertRaises(BudgetExceeded):
            guard.check()

    def test_install_wraps_stream_method_and_checks_before_request(self):
        class FakeAgent:
            def __init__(self):
                self.calls = 0

            def _stream_response(self):
                self.calls += 1
                return "ok"

        agent = FakeAgent()
        guard = BudgetGuard(budget=Budget(max_steps=1, max_seconds=None, max_cost_usd=None))
        self.assertTrue(guard.install(agent))
        agent._stream_response()
        guard.note_step()
        with self.assertRaises(BudgetExceeded):
            agent._stream_response()
        self.assertEqual(agent.calls, 1, "the wrapped method must refuse to issue the request")

    def test_install_reports_failure_for_unknown_provider(self):
        class Weird:
            pass

        guard = BudgetGuard(budget=Budget())
        self.assertFalse(guard.install(Weird()))


class TestPricing(unittest.TestCase):
    def test_known_model_priced(self):
        self.assertIsNotNone(price_for("deepseek-v4-flash"))
        self.assertGreater(estimate_cost_usd("deepseek-v4-flash", 1_000_000, 0), 0)

    def test_unknown_model_returns_none_not_zero(self):
        """Critical: 0.0 would make an unmeasured cost look free, and 'cheapest' is a claim a
        benchmark gets asked for."""
        self.assertIsNone(estimate_cost_usd("some-unlisted-model", 1000, 1000))


class TestExceptionClassification(unittest.TestCase):
    def test_credentials_are_env_errors_not_agent_failures(self):
        status, _ = _classify_exception(RuntimeError("Authentication failed: invalid api key"))
        self.assertEqual(status, STATUS_ENV_ERROR)

    def test_insufficient_credit_is_an_env_error(self):
        """Measured: a mid-suite 402 produced 13 consecutively unusable instances. It must be
        classified as an environment problem (never a model failure) and must not be retried."""
        status, detail = _classify_exception(RuntimeError("Error code: 402 - Insufficient credits"))
        self.assertEqual(status, STATUS_ENV_ERROR)
        self.assertIn("credit", detail)
        self.assertFalse(is_retryable(RuntimeError("Error code: 402 - Insufficient credits")))

    def test_rate_limit_is_retryable(self):
        self.assertTrue(is_retryable(RuntimeError("429 rate limit exceeded")))

    def test_bad_key_is_not_retryable(self):
        self.assertFalse(is_retryable(RuntimeError("401 invalid api key")))

    def test_missing_dependency_is_env_error(self):
        status, _ = _classify_exception(ModuleNotFoundError("no module named foo"))
        self.assertEqual(status, STATUS_ENV_ERROR)

    def test_rate_limit_is_agent_error(self):
        status, _ = _classify_exception(RuntimeError("429 rate limit exceeded"))
        self.assertEqual(status, STATUS_AGENT_ERROR)

    def test_unknown_exception_is_harness_error_not_failure(self):
        status, _ = _classify_exception(ValueError("something odd in our own code"))
        self.assertEqual(status, STATUS_HARNESS_ERROR)


class TestWorkspaceDigest(unittest.TestCase):
    def test_digest_changes_when_a_file_changes_and_ignores_session_dir(self):
        with scratch() as tmp:
            ws = Path(tmp)
            (ws / "a.txt").write_text("one", encoding="utf-8")
            (ws / ".myriaxon").mkdir()
            (ws / ".myriaxon" / "session.json").write_text("{}", encoding="utf-8")
            before = workspace_digest(ws)
            self.assertEqual(list(before), ["a.txt"], "session bookkeeping must not affect the digest")
            (ws / "a.txt").write_text("two", encoding="utf-8")
            self.assertNotEqual(before, workspace_digest(ws))


class TestScoring(unittest.TestCase):
    def test_axs_and_coverage_from_records(self):
        records = [
            _record("g1", "goal", 0, assertions=[_assertion("task_success")]),
            _record("g1", "goal", 1, assertions=[_assertion("task_success", passed=False)]),
        ]
        summary = score_suite(records, label="unit")
        task_success = next(e for e in summary.parameters if e.parameter.id == "task_success")
        self.assertAlmostEqual(task_success.pass_rate, 0.5)
        self.assertLess(summary.coverage, 1.0)
        self.assertTrue(summary.provisional)
        self.assertIsNotNone(summary.axs)

    def test_non_attributable_instances_are_excluded_from_scoring(self):
        records = [
            _record("g1", "goal", 0, assertions=[_assertion("task_success")]),
            _record("g1", "goal", 1, passed=False, status=STATUS_HARNESS_ERROR, assertions=[_assertion("task_success", passed=False)]),
        ]
        summary = score_suite(records, label="unit")
        task_success = next(e for e in summary.parameters if e.parameter.id == "task_success")
        self.assertEqual(task_success.pass_rate, 1.0, "a harness crash must not count as a model failure")
        self.assertEqual(summary.harness_reliability["status_counts"][STATUS_HARNESS_ERROR], 1)

    def test_provisional_when_coverage_incomplete(self):
        summary = score_suite([_record(assertions=[_assertion("task_success")])], label="unit")
        self.assertTrue(summary.provisional)

    def test_stability_uses_identical_prompt_repeats(self):
        records = [
            _record("s1", "consistency", 0, turns=["ap-southeast-3"], spec={"kind": "consistency", "consistency_mode": "stability"}),
            _record("s1", "consistency", 1, turns=["ap-southeast-3"], spec={"kind": "consistency", "consistency_mode": "stability"}),
        ]
        summary = score_suite(records, label="unit")
        stability = next(e for e in summary.parameters if e.parameter.id == "stability")
        self.assertIsNotNone(stability.score)
        self.assertGreater(stability.score, 0.9)

    def test_stability_penalises_divergent_answers(self):
        records = [
            _record("s1", "consistency", 0, turns=["ap-southeast-3"], spec={"kind": "consistency", "consistency_mode": "stability"}),
            _record("s1", "consistency", 1, turns=["completely different region eu-west-1"], spec={"kind": "consistency", "consistency_mode": "stability"}),
        ]
        summary = score_suite(records, label="unit")
        stability = next(e for e in summary.parameters if e.parameter.id == "stability")
        self.assertLess(stability.score, 0.9)

    def test_robustness_only_uses_robustness_tagged_tasks(self):
        records = [
            _record("s1", "consistency", 0, turns=["x"], spec={"kind": "consistency", "consistency_mode": "stability"}),
            _record("s1", "consistency", 1, turns=["x"], spec={"kind": "consistency", "consistency_mode": "stability"}),
        ]
        summary = score_suite(records, label="unit")
        robustness = next(e for e in summary.parameters if e.parameter.id == "robustness")
        self.assertIsNone(robustness.score, "stability repeats must not be scored as phrasing robustness")

    def test_fairness_scores_the_worst_group(self):
        records = [
            _record("f1", "fairness", 0, variant="enterprise", assertions=[_assertion("fairness")]),
            _record("f1", "fairness", 1, variant="nonprofit", assertions=[_assertion("fairness", passed=False, score=0.0)]),
        ]
        summary = score_suite(records, label="unit")
        fairness = next(e for e in summary.parameters if e.parameter.id == "fairness")
        self.assertAlmostEqual(fairness.score, 0.0, msg="averaging would have hidden the gap")

    def test_cost_per_solved_task(self):
        records = [_record(cost_usd=0.10, assertions=[_assertion("task_success")])]
        summary = score_suite(records, label="unit")
        self.assertAlmostEqual(summary.totals["cost_per_solved_task_usd"], 0.10)

    def test_unpriced_run_warns_instead_of_reporting_free(self):
        records = [_record(model="mystery-model", cost_usd=None, assertions=[_assertion("task_success")])]
        summary = score_suite(records, label="unit")
        self.assertIsNone(summary.totals["total_cost_usd"])
        self.assertTrue(any("cost" in w for w in summary.warnings))

    def test_record_round_trips_turns_and_trace(self):
        """Artifacts are the only copy of a run. If a round trip drops the reply text or the tool
        calls, the score can no longer be re-derived or disputed -- which is the entire point of
        storing them, so this is pinned by a test rather than trusted."""
        event = TraceEvent(index=1, tool="read_file", args={"path": "a.py"}, result="ok", is_error=False, duration_ms=3.0)
        record = _record(turns=["the answer is 10"], events=[event], assertions=[_assertion("task_success")])
        restored = InstanceRecord.from_dict(record.to_dict())
        self.assertEqual(len(restored.state.turns), 1)
        self.assertEqual(restored.state.turns[0].final_text, "the answer is 10")
        self.assertEqual(len(restored.state.events), 1)
        self.assertEqual(restored.state.events[0].tool, "read_file")
        self.assertEqual(restored.state.events[0].args, {"path": "a.py"})

    def test_older_artifact_shape_with_turn_count_still_loads(self):
        """Forward/backward compatibility: an artifact written before turn texts were persisted has
        `turns` as an int. It must still be scoreable (minus the texts), never crash the reporter."""
        raw = _record(assertions=[_assertion("task_success")]).to_dict()
        raw["state"]["turns"] = 2
        restored = InstanceRecord.from_dict(raw)
        self.assertEqual(restored.state.turns, [])
        summary = score_suite([restored], label="unit")
        self.assertIsNotNone(summary.axs)
        self.assertFalse(restored.state.replies_available)

    def test_missing_replies_never_manufacture_a_perfect_stability_score(self):
        """The specific failure this guards against: with no recorded answers, an implementation
        that treats 'no pairwise comparisons' as 'all identical' reports a flawless consistency
        score for a run whose answers nobody can see."""
        raw_a = _record("s1", "consistency", 0, spec={"kind": "consistency", "consistency_mode": "stability"}).to_dict()
        raw_b = _record("s1", "consistency", 1, spec={"kind": "consistency", "consistency_mode": "stability"}).to_dict()
        for raw in (raw_a, raw_b):
            raw["state"]["turns"] = 1  # old shape: a count, not a list
        records = [InstanceRecord.from_dict(raw_a), InstanceRecord.from_dict(raw_b)]
        summary = score_suite(records, label="unit")
        stability = next(e for e in summary.parameters if e.parameter.id == "stability")
        self.assertIsNone(stability.score)
        self.assertTrue(any("predate reply persistence" in w for w in summary.warnings))

    def test_failing_assertions_are_recorded_per_parameter(self):
        records = [_record("g1", "goal", 0, passed=False, assertions=[_assertion("task_success", passed=False)])]
        summary = score_suite(records, label="unit")
        entry = next(e for e in summary.parameters if e.parameter.id == "task_success")
        failures = entry.detail.get("failing_assertions", [])
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["task"], "g1")

    def test_primary_probe_marks_the_parameter_measured(self):
        records = [_record("g1", "goal", 0, assertions=[_assertion("task_success")])]
        summary = score_suite(records, label="unit")
        entry = next(e for e in summary.parameters if e.parameter.id == "task_success")
        self.assertEqual(entry.evidence_level, "primary")

    def test_incidental_evidence_is_excluded_from_the_headline_axs(self):
        """Measured case this guards: P13 read 1.000 because three *other* tasks happened to carry a
        fabrication `not-contains` check, while P13's own abstention probe never produced a verdict
        (it hit the step budget). Counting that as a measured pass inflates the composite."""
        records = [
            _record("g1", "goal", 0, assertions=[_assertion("task_success")]),
            # a grounding task carrying a hallucination_risk assertion, as p10 does in the seed suite
            _record("gr1", "grounding", 0, assertions=[_assertion("hallucination_risk")]),
        ]
        summary = score_suite(records, label="unit")
        halluc = next(e for e in summary.parameters if e.parameter.id == "hallucination_risk")
        self.assertEqual(halluc.evidence_level, "secondary")
        self.assertIn("hallucination_risk", summary.secondary_only)
        # headline AXS must be computed from the goal probe alone
        self.assertAlmostEqual(summary.axs, 100.0)
        self.assertAlmostEqual(summary.coverage, BY_ID["task_success"].weight, places=6)
        self.assertIsNotNone(summary.axs_inclusive)
        self.assertTrue(any("incidental evidence" in w for w in summary.warnings))

    def test_probe_that_ran_but_produced_no_verdict_is_unmeasured(self):
        """A budget-exceeded primary probe must not be counted as either a pass or a measured
        parameter -- it is an absence of evidence, not evidence of capability."""
        record = _record("p13", "abstention", 0, passed=False, status="budget_exceeded", assertions=[])
        records = [record, _record("g1", "goal", 0, assertions=[_assertion("task_success")])]
        summary = score_suite(records, label="unit")
        halluc = next(e for e in summary.parameters if e.parameter.id == "hallucination_risk")
        self.assertEqual(halluc.evidence_level, "unmeasured")
        self.assertIsNone(halluc.score)

    def test_task_table_has_reliability_columns(self):
        records = [
            _record("g1", "goal", 0, assertions=[_assertion("task_success")]),
            _record("g1", "goal", 1, passed=False, assertions=[_assertion("task_success", passed=False)]),
        ]
        summary = score_suite(records, label="unit")
        row = summary.task_table[0]
        self.assertAlmostEqual(row["pass@k_2"], 1.0)
        self.assertAlmostEqual(row["pass^k_2"], 0.0)

    def test_judge_share_is_reported(self):
        records = [_record(assertions=[_assertion("comprehensiveness", judged=True), _assertion("task_success")])]
        summary = score_suite(records, label="unit")
        self.assertEqual(summary.totals["judged_assertions"], 1)
        self.assertTrue(any("model-judged" in w for w in summary.warnings))

    def test_empty_records_is_an_error(self):
        with self.assertRaises(ValueError):
            score_suite([], label="unit")


class TestReport(unittest.TestCase):
    def _summary(self):
        records = [
            _record("g1", "goal", 0, assertions=[_assertion("task_success")]),
            _record("g1", "goal", 1, passed=False, assertions=[_assertion("task_success", passed=False)]),
        ]
        return score_suite(records, label="unit-target", manifest={"target": {"name": "unit-target", "provider": "deepseek", "model": "deepseek-v4-flash"}})

    def test_scorecard_lists_all_fourteen_parameters(self):
        text = scorecard(self._summary())
        for parameter in PARAMETERS:
            self.assertIn(parameter.name, text)
        self.assertIn("PROVISIONAL", text)
        self.assertIn("Harness reliability", text)

    def test_write_report_produces_json_and_markdown(self):
        with scratch() as tmp:
            json_path, md_path = write_report(self._summary(), Path(tmp))
            self.assertTrue(json_path.exists() and md_path.exists())
            data = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual(len(data["parameters"]), 14)
            self.assertEqual(data["standard"], "IQB-14@1.0.0")

    def test_compare_marks_regressions(self):
        records_a = [_record("g1", "goal", 0, assertions=[_assertion("task_success")])]
        records_b = [_record("g1", "goal", 0, passed=False, assertions=[_assertion("task_success", passed=False)])]
        base = score_suite(records_a, label="A")
        other = score_suite(records_b, label="B")
        text = compare_markdown(base, other)
        self.assertIn("Regressions", text)


class TestSeedSuite(unittest.TestCase):
    # The seed suite lives INSIDE the package: the top-level `bench/` directory was folded into
    # `myriaxon/bench/` when the distribution was renamed, so a repo-root-relative `bench/tasks`
    # path no longer resolves and every test in this class failed on a missing directory.
    SUITE = REPO_ROOT / "myriaxon" / "bench" / "tasks"

    def test_all_seed_tasks_load_and_validate(self):
        suite = load_suite(self.SUITE)
        self.assertGreaterEqual(len(suite), 14)

    def test_every_evidence_kind_has_at_least_one_task(self):
        suite = load_suite(self.SUITE)
        present = {s.kind for s in suite}
        missing = [k for k in KINDS if k not in present]
        self.assertEqual(missing, [], f"no seed task covers these evidence families: {missing}")

    def test_every_task_id_is_unique_and_dir_matches_id(self):
        suite = load_suite(self.SUITE)
        ids = [s.id for s in suite]
        self.assertEqual(len(ids), len(set(ids)))
        for spec in suite:
            self.assertEqual(spec.path.name, spec.id, "task directory must be named after the task id")

    def test_declared_assertion_parameters_exist_in_the_registry(self):
        for spec in load_suite(self.SUITE):
            for assertion in spec.assertions:
                self.assertIn(assertion["parameter"], BY_ID, f"{spec.id}: {assertion['parameter']}")

    def test_every_parameter_has_a_seed_task_pointing_at_it(self):
        suite = load_suite(self.SUITE)
        referenced = {a["parameter"] for s in suite for a in s.assertions}
        missing = [p.id for p in PARAMETERS if p.id not in referenced]
        self.assertEqual(missing, [], f"parameters with no seed evidence: {missing}")

    def test_duplicate_ids_are_rejected(self):
        with scratch() as tmp:
            root = Path(tmp)
            for name in ("a", "b"):
                d = root / name
                d.mkdir()
                (d / "task.json").write_text(
                    json.dumps({"id": "same", "kind": "goal", "prompt": "x", "files": {}}), encoding="utf-8"
                )
            with self.assertRaises(TaskError):
                load_suite(root)

    def test_malformed_task_reports_clearly(self):
        with scratch() as tmp:
            d = Path(tmp) / "bad"
            d.mkdir()
            (d / "task.json").write_text(json.dumps({"id": "bad", "kind": "not-a-kind", "prompt": "x"}), encoding="utf-8")
            with self.assertRaises(TaskError):
                load_task(d)

    def test_unknown_assertion_type_is_rejected_at_load_time(self):
        with scratch() as tmp:
            d = Path(tmp) / "bad"
            d.mkdir()
            (d / "task.json").write_text(
                json.dumps(
                    {
                        "id": "bad",
                        "kind": "goal",
                        "prompt": "x",
                        "assertions": [{"type": "made-up", "parameter": "task_success"}],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(TaskError):
                load_task(d)

    def test_attempt_plan_cycles_variants(self):
        spec = load_task(REPO_ROOT / "myriaxon" / "bench" / "tasks" / "p07_robustness_reworded_requirements")
        plan = spec.attempt_plan()
        self.assertEqual(len(plan), 3)
        self.assertEqual(len({spec.variant_name(i) for i in plan}), 3)


class TestJudge(unittest.TestCase):
    """The judge is the only non-deterministic scorer, so its plumbing is pinned here. Note what is
    tested: construction, key resolution, provider defaults, classification of the subprocess reply,
    and the fail-loud contract. Actual grading quality needs a live model and is not a unit test."""

    def test_build_judge_returns_none_when_disabled(self):
        self.assertIsNone(build_judge(None))
        self.assertIsNone(build_judge(JudgeSpec(provider="deepseek", model="")))

    def test_build_judge_raises_without_a_key(self):
        spec = JudgeSpec(provider="deepseek", model="m", api_key_env="IQB_DEFINITELY_UNSET_KEY")
        with self.assertRaises(ValueError):
            build_judge(spec)

    def test_mode_selects_the_implementation(self):
        spec = JudgeSpec(provider="deepseek", model="m", api_key="k", mode="api")
        self.assertIsInstance(build_judge(spec), OpenAICompatJudge)
        spec = JudgeSpec(provider="deepseek", model="m", api_key="k", mode="agent")
        self.assertIsInstance(build_judge(spec), MyriaxonJudge)

    def test_provider_defaults_are_known_endpoints(self):
        self.assertEqual(default_endpoint("deepseek"), DEFAULT_DEEPSEEK_BASE_URL)
        self.assertEqual(default_endpoint("openrouter"), DEFAULT_OPENROUTER_BASE_URL)
        self.assertEqual(default_key_env("deepseek"), "DEEPSEEK_API_KEY")
        self.assertEqual(default_key_env("openrouter"), "OPENROUTER_API_KEY")

    def test_judge_spec_reports_its_implementation_and_never_the_key(self):
        spec = JudgeSpec(
            provider="deepseek",
            model="deepseek-v4-flash",
            api_key="sk-secret",
            base_url=DEFAULT_DEEPSEEK_BASE_URL,
            mode="agent",
        )
        payload = json.dumps(spec.to_dict())
        self.assertIn("myriaxon", payload)
        self.assertNotIn("sk-secret", payload)

    def test_agent_judge_parses_the_cli_json_reply(self):
        from myriaxon.bench import judge as judge_module

        class FakeProc:
            returncode = 0
            stdout = 'banner noise\n{"text": "{\\"covered\\": [\\"a\\"]}", "usage": {}}\n'
            stderr = ""

        original = judge_module.subprocess.run
        judge_module.subprocess.run = lambda *a, **k: FakeProc()
        try:
            judge = MyriaxonJudge(JudgeSpec(provider="deepseek", model="m", api_key="k"), "k")
            reply = judge("grade this")
        finally:
            judge_module.subprocess.run = original
        self.assertIn("covered", reply)
        self.assertEqual(judge.calls, 1)

    def test_agent_judge_surfaces_a_cli_error_instead_of_scoring_blank(self):
        from myriaxon.bench import judge as judge_module

        class FakeProc:
            returncode = 1
            stdout = '{"text": null, "error": "Authentication failed."}\n'
            stderr = ""

        original = judge_module.subprocess.run
        judge_module.subprocess.run = lambda *a, **k: FakeProc()
        try:
            judge = MyriaxonJudge(JudgeSpec(provider="deepseek", model="m", api_key="k"), "k")
            with self.assertRaises(RuntimeError):
                judge("grade this")
        finally:
            judge_module.subprocess.run = original

    def test_llm_rubric_with_a_working_judge_scores_points(self):
        """End-to-end through the assertion engine with a stub judge: proves a configured judge is
        actually consulted and that its JSON is turned into a score."""
        ctx = AssertionContext(workspace=self.ws, state=self.state, task={}, final_text="the answer is 42")
        judge = lambda prompt, model=None: '{"covered": ["a"]}'
        results = run_assertions(
            [
                {
                    "type": "llm-rubric",
                    "parameter": "comprehensiveness",
                    "points": [{"id": "a", "text": "the answer is 42"}],
                    "grading": "judge",
                }
            ],
            ctx,
            judge_client=judge,
        )
        self.assertTrue(results[0].judged)
        self.assertTrue(results[0].passed)
        self.assertAlmostEqual(results[0].score, 1.0)

    def setUp(self):
        self._tmpctx = scratch()
        self.ws = self._tmpctx.__enter__()
        self.state = RunState(task_id="t", attempt=0, status=STATUS_COMPLETED)

    def tearDown(self):
        self._tmpctx.__exit__(None, None, None)


class TestTarget(unittest.TestCase):
    def test_api_key_env_resolution(self):
        target = Target(name="t", provider="deepseek", model="m", api_key_env="IQB_TEST_KEY_UNSET_XYZ")
        self.assertEqual(target.resolve_key(), "")

    def test_manifest_never_contains_the_key(self):
        target = Target(name="t", provider="deepseek", model="m", api_key="sk-super-secret")
        self.assertNotIn("sk-super-secret", json.dumps(target.to_dict()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
