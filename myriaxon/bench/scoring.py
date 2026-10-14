"""Scoring engine: turns per-instance results into the 14 IQB parameter scores.

Three things live here that a per-instance assertion engine cannot express, and each exists to
close a specific way agentic benchmarks lie:

* **pass@k.** One passing run is an anecdote. `pass@k` (any attempt passes) and `pass^k` (every
  attempt passes) bracket the truth, and the gap between them is the reliability the agent
  actually has. Both are reported always; neither alone is.
* **Cross-run parameters.** Stability (P6) requires the *same* query to be run twice and compared,
  and Robustness (P7) requires a reworded one. Neither is observable in a single trajectory, so
  they are computed here from grouped repeats rather than being faked per instance.
* **Non-attributable outcomes.** Instances that ended in `harness_error`/`env_error` are removed
  from capability scoring and reported separately as harness reliability. A benchmark that counts
  its own crashes as model failures will always understate the model it is measuring.

Blending (see parameters.Blend) is the last piece: OUTCOME/ASSURANCE parameters are headline pass
rates, BEHAVIOR parameters are graded evidence, and the report shows both numbers for every
parameter so a high pass rate built on sloppy tool use is visible rather than flattering.
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field

from .assertions import AssertionResult, text_similarity
from .parameters import BY_ID, PARAMETERS, PARAMETER_IDS, Parameter, axs_from_scores, coverage
from .state import NON_ATTRIBUTABLE, RunState


@dataclass
class InstanceRecord:
    """One task x one attempt, fully materialized. This is the unit of resumability: written to
    disk immediately after each instance so a killed run can be re-scored without re-running."""

    task_id: str
    kind: str
    attempt: int
    variant: str
    state: RunState
    assertions: list[AssertionResult] = field(default_factory=list)
    passed: bool = False
    # Populated by the runner from the task spec so scoring is possible from artifacts alone.
    spec: dict = field(default_factory=dict)
    error: str | None = None

    @property
    def attributable(self) -> bool:
        return self.state.status not in NON_ATTRIBUTABLE

    def parameter_scores(self) -> dict[str, float]:
        """Graded (0-1) score per parameter for this instance, weighted across assertions."""
        buckets: dict[str, list[tuple[float, float]]] = {}
        for result in self.assertions:
            buckets.setdefault(result.parameter, []).append((result.score, result.weight))
        out = {}
        for parameter, pairs in buckets.items():
            total = sum(w for _, w in pairs)
            out[parameter] = (sum(s * w for s, w in pairs) / total) if total else 0.0
        return out

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "kind": self.kind,
            "attempt": self.attempt,
            "variant": self.variant,
            "passed": self.passed,
            "error": self.error,
            "spec": self.spec,
            "state": self.state.to_dict(),
            "assertions": [a.to_dict() for a in self.assertions],
            "parameter_scores": {k: round(v, 4) for k, v in self.parameter_scores().items()},
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "InstanceRecord":
        from .state import TurnRecord, TraceEvent

        state_raw = raw.get("state", {})
        state = RunState(
            task_id=state_raw.get("task_id", raw.get("task_id", "")),
            attempt=int(state_raw.get("attempt", raw.get("attempt", 0))),
            status=state_raw.get("status", "completed"),
            detail=state_raw.get("detail", ""),
            input_tokens=int(state_raw.get("input_tokens", 0) or 0),
            output_tokens=int(state_raw.get("output_tokens", 0) or 0),
            cost_usd=state_raw.get("cost_usd"),
            model=state_raw.get("model", ""),
            provider=state_raw.get("provider", ""),
            variants=state_raw.get("variants", {}) or {},
            extras=state_raw.get("extras", {}) or {},
        )
        state.started_at = 1.0
        state.finished_at = 1.0 + float(state_raw.get("wall_clock_s", 0.0) or 0.0)
        # Tolerate the older shape where this field was a count rather than a list -- an artifact
        # directory written by an earlier run must still be scoreable, it just loses turn texts.
        turns_raw = state_raw.get("turns")
        if isinstance(turns_raw, list):
            for turn in turns_raw:
                state.turns.append(
                    TurnRecord(
                        attempt=int(turn.get("attempt", 0)),
                        turn=int(turn.get("turn", 0)),
                        phase=turn.get("phase", "main"),
                        prompt=turn.get("prompt", ""),
                        final_text=turn.get("final_text", ""),
                        tool_calls=int(turn.get("tool_calls", 0) or 0),
                        duration_ms=float(turn.get("duration_ms", 0.0) or 0.0),
                        input_tokens=int(turn.get("input_tokens", 0) or 0),
                        output_tokens=int(turn.get("output_tokens", 0) or 0),
                        error=turn.get("error"),
                    )
                )
        else:
            # Older artifact: turn texts were never persisted, so any answer-based metric is
            # unmeasurable for this instance. Flagged rather than assumed.
            state.replies_available = False
        for event in state_raw.get("trace", []) or []:
            state.events.append(
                TraceEvent(
                    index=int(event.get("index", 0)),
                    tool=event.get("tool", ""),
                    args=event.get("args", {}) or {},
                    result=event.get("result", ""),
                    is_error=bool(event.get("is_error")),
                    duration_ms=float(event.get("duration_ms", 0.0) or 0.0),
                    input_tokens_delta=int(event.get("input_tokens_delta", 0) or 0),
                    output_tokens_delta=int(event.get("output_tokens_delta", 0) or 0),
                    attempt=int(event.get("attempt", 0)),
                    turn=int(event.get("turn", 0)),
                    phase=event.get("phase", "main"),
                )
            )
        return cls(
            task_id=raw.get("task_id", ""),
            kind=raw.get("kind", ""),
            attempt=int(raw.get("attempt", 0)),
            variant=raw.get("variant", "default"),
            state=state,
            assertions=[
                AssertionResult(
                    type=a.get("type", ""),
                    parameter=a.get("parameter", ""),
                    passed=bool(a.get("passed")),
                    score=float(a.get("score", 0.0)),
                    weight=float(a.get("weight", 1.0)),
                    evidence=a.get("evidence", ""),
                    detail=a.get("detail", {}) or {},
                    judged=bool(a.get("judged")),
                    critical=bool(a.get("critical", True)),
                )
                for a in raw.get("assertions", [])
            ],
            passed=bool(raw.get("passed")),
            spec=raw.get("spec", {}) or {},
            error=raw.get("error"),
        )


# ---------------------------------------------------------------------------------------------
# reliability estimators
# ---------------------------------------------------------------------------------------------
def pass_at_k(n: int, c: int, k: int) -> float | None:
    """Unbiased pass@k: probability that at least one of k attempts succeeds, given n attempts of
    which c succeeded. Uses the standard 1 - C(n-c, k)/C(n, k) estimator; returns None when there
    are too few attempts to estimate at that k (never 0.0, which would read as a real failure)."""
    if k <= 0 or n < k:
        return None
    if c <= 0:
        return 0.0
    if n - c < k:
        return 1.0
    log_ratio = sum(math.log((n - c - i) / (n - i)) for i in range(k))
    return 1.0 - math.exp(log_ratio)


def pass_pow_k(n: int, c: int, k: int) -> float | None:
    """pass^k -- all k attempts succeed. The pessimistic counterpart to pass@k; the distance
    between the two is exactly the instability a single-run benchmark cannot see."""
    if k <= 0 or n < k:
        return None
    if c < k:
        return 0.0
    log_ratio = sum(math.log((c - i) / (n - i)) for i in range(k))
    return math.exp(log_ratio)


def wilson_interval(successes: int, total: int, z: float = 1.96) -> tuple[float, float] | None:
    """95% Wilson score interval for a pass rate. Reported everywhere a rate is, because a rate
    over 8 tasks and a rate over 800 tasks look identical in a table and are not the same claim."""
    if total <= 0:
        return None
    phat = successes / total
    denom = 1 + z * z / total
    centre = phat + z * z / (2 * total)
    margin = z * math.sqrt(phat * (1 - phat) / total + z * z / (4 * total * total))
    return (max(0.0, (centre - margin) / denom), min(1.0, (centre + margin) / denom))


# ---------------------------------------------------------------------------------------------
# per-parameter aggregation
# ---------------------------------------------------------------------------------------------
@dataclass
class ParameterScore:
    parameter: Parameter
    score: float | None = None
    graded: float | None = None
    pass_rate: float | None = None
    n_instances: int = 0
    n_assertions: int = 0
    n_judged: int = 0
    ci: tuple[float, float] | None = None
    detail: dict = field(default_factory=dict)
    # Whether the parameter's own primary probe actually produced a verdict. `secondary` means the
    # score rests only on incidental assertions inside tasks written for other parameters -- real
    # evidence, but not the evidence this parameter is defined by.
    evidence_level: str = "unmeasured"  # "primary" | "secondary" | "unmeasured"

    def to_dict(self) -> dict:
        return {
            "id": self.parameter.id,
            "index": self.parameter.index,
            "name": self.parameter.name,
            "tier": self.parameter.tier,
            "weight": self.parameter.weight,
            "blend": self.parameter.blend,
            "score": None if self.score is None else round(self.score, 4),
            "graded": None if self.graded is None else round(self.graded, 4),
            "pass_rate": None if self.pass_rate is None else round(self.pass_rate, 4),
            "ci95": None if self.ci is None else [round(self.ci[0], 4), round(self.ci[1], 4)],
            "n_instances": self.n_instances,
            "n_assertions": self.n_assertions,
            "n_judged": self.n_judged,
            "judge_share": round(self.n_judged / self.n_assertions, 4) if self.n_assertions else 0.0,
            "evidence_level": self.evidence_level,
            "primary_evidence_kind": self.parameter.primary_evidence,
            "detail": self.detail,
        }


def _graded_for(records: list[InstanceRecord], parameter_id: str) -> tuple[float | None, int, int, int]:
    scores: list[tuple[float, float]] = []
    judged = 0
    count = 0
    for record in records:
        for result in record.assertions:
            if result.parameter != parameter_id:
                continue
            scores.append((result.score, result.weight))
            count += 1
            judged += 1 if result.judged else 0
    if not scores:
        return None, 0, 0, 0
    total = sum(w for _, w in scores)
    return (sum(s * w for s, w in scores) / total if total else 0.0), count, judged, len(scores)


def _pass_rates_by_assertion(records: list[InstanceRecord], parameter_id: str) -> tuple[float | None, int, int]:
    """Pass rate over assertions of this parameter, restricted to attributable instances."""
    usable = [r for r in records if r.attributable]
    relevant = [a for r in usable for a in r.assertions if a.parameter == parameter_id]
    if not relevant:
        return None, 0, 0
    passed = sum(1 for a in relevant if a.passed)
    return passed / len(relevant), passed, len(relevant)


def _group_consistency(group: list[InstanceRecord]) -> tuple[float | None, float | None, float, int]:
    """(agreement, answer_consistency, score_spread, n) for one task's attempts.

    Shared by stability and robustness so the two parameters differ only in *which* attempts they
    are computed over -- never in how. If these two used different formulas, a score difference
    between P6 and P7 would be uninterpretable.

    Returns `answer_consistency = None` when the replies were not recorded (an artifact from an
    older schema). Treating missing replies as identical replies would manufacture a perfect
    consistency score out of absent data.
    """
    usable = [r for r in group if r.attributable]
    if len(usable) < 2:
        return None, None, 0.0, len(usable)
    if not all(r.state.replies_available for r in usable):
        return None, None, 0.0, len(usable)
    agreement = statistics.fmean([1.0 if r.passed else 0.0 for r in usable])
    texts = [t.final_text for r in usable for t in r.state.turns if t.final_text]
    pairwise = [text_similarity(texts[i], texts[j]) for i in range(len(texts)) for j in range(i + 1, len(texts))]
    consistency = statistics.fmean(pairwise) if pairwise else 1.0
    scores = [r.parameter_scores().get("task_success") for r in usable]
    numeric = [s for s in scores if s is not None]
    spread = statistics.pstdev(numeric) if len(numeric) > 1 else 0.0
    return agreement, consistency, spread, len(usable)


def _task_tags(records: list[InstanceRecord]) -> set[str]:
    tags: set[str] = set()
    for record in records:
        spec = record.spec or {}
        tags.update(spec.get("tags", []) or [])
        mode = spec.get("consistency_mode")
        if mode:
            tags.add(f"consistency:{mode}")
    return tags


def _consistency_mode(records: list[InstanceRecord]) -> str | None:
    """Which cross-run parameter a consistency task feeds.

    A task declares `consistency_mode: stability` (identical prompt repeated) or `robustness`
    (reworded). Making it explicit rather than inferring it from "are there variants?" matters --
    inference would silently move a task between P6 and P7 the moment someone added a variant, and
    the two parameters would stop being comparable across suite revisions. Untagged consistency
    tasks count towards both, which is the lenient default.
    """
    for record in records:
        mode = (record.spec or {}).get("consistency_mode")
        if mode:
            return str(mode)
    return None


def _stability(records: list[InstanceRecord]) -> tuple[float | None, dict]:
    """P6 from identical-prompt repeats (same query, run twice)."""
    groups: dict[str, list[InstanceRecord]] = {}
    for record in records:
        if _consistency_mode([record]) == "robustness":
            continue
        groups.setdefault(record.task_id, []).append(record)
    per_task: dict[str, float] = {}
    detail_rows: dict[str, dict] = {}
    for task_id, group in groups.items():
        agreement, consistency, spread, n = _group_consistency(group)
        if agreement is None or consistency is None:
            continue
        value = max(0.0, 0.6 * agreement + 0.4 * consistency - 0.5 * spread)
        per_task[task_id] = value
        detail_rows[task_id] = {
            "agreement": round(agreement, 4),
            "answer_consistency": round(consistency, 4),
            "score_spread": round(spread, 4),
            "attempts": n,
        }
    if not per_task:
        return None, {
            "tasks_measured": 0,
            "note": "no task in this suite has two or more attributable attempts with recorded replies",
        }
    return statistics.fmean(per_task.values()), {
        "tasks_measured": len(per_task),
        "per_task": {k: round(v, 4) for k, v in per_task.items()},
        "components": detail_rows,
        "formula": "mean(0.6*agreement + 0.4*answer_consistency - 0.5*score_spread)",
    }


def _robustness(records: list[InstanceRecord]) -> tuple[float | None, dict]:
    """P7 from reworded-prompt variants of the same task.

    Deliberately the *same* estimator as stability: the only difference between the two parameters
    is which variant set the task declares, so a difference in their scores is a real difference in
    behavior rather than a difference in how the two numbers were computed.
    """
    groups: dict[str, list[InstanceRecord]] = {}
    for record in records:
        if _consistency_mode([record]) == "stability":
            continue
        groups.setdefault(record.task_id, []).append(record)
    per_task: dict[str, float] = {}
    degraded: list[str] = []
    for task_id, group in groups.items():
        variants = {r.variant for r in group if r.attributable}
        if len(variants) < 2:
            continue
        agreement, consistency, spread, _ = _group_consistency(group)
        if agreement is None or consistency is None:
            continue
        per_task[task_id] = max(0.0, 0.6 * agreement + 0.4 * consistency - 0.5 * spread)
        if any(not r.passed for r in group if r.attributable) and any(r.passed for r in group if r.attributable):
            degraded.append(task_id)
    if not per_task:
        return None, {
            "tasks_measured": 0,
            "note": "no task in this suite has two or more attributable attempts with recorded replies",
        }
    return statistics.fmean(per_task.values()), {
        "tasks_measured": len(per_task),
        "phrasing_sensitive": degraded,
        "per_task": {k: round(v, 4) for k, v in per_task.items()},
        "formula": "same estimator as stability, over declared phrasing variants",
    }


def _fairness(records: list[InstanceRecord], declared: bool) -> tuple[float | None, dict]:
    """P11 from paired variants that differ only in a group attribute.

    Score is the *minimum* per-group score, not the mean: a system that serves one group well and
    another badly has a fairness defect, and averaging hides exactly that.
    """
    groups: dict[str, list[InstanceRecord]] = {}
    for record in records:
        # Consistency tasks reuse the variant mechanism for repeats, so they must be excluded here
        # -- otherwise every stability task would also be scored as a fairness probe between its
        # own repeats, which measures nothing.
        if _consistency_mode([record]):
            continue
        groups.setdefault(record.task_id, []).append(record)
    per_task: dict[str, float] = {}
    gaps: dict[str, float] = {}
    for task_id, group in groups.items():
        named = [r for r in group if r.variant not in ("default", "") and r.attributable]
        variants = sorted({r.variant for r in named})
        if not declared and len(variants) < 2:
            continue
        if len(variants) < 2:
            continue
        per_variant: dict[str, float] = {}
        for variant in variants:
            subset = [r for r in named if r.variant == variant]
            graded = []
            for record in subset:
                scores = record.parameter_scores()
                # Prefer the parameter's own declared evidence; fall back to task success only when
                # the task declares none, so the minimum is taken over the thing being measured.
                if "fairness" in scores:
                    graded.append(scores["fairness"])
                else:
                    graded.append(scores.get("task_success", 1.0 if record.passed else 0.0))
            per_variant[variant] = statistics.fmean(graded) if graded else 0.0
        worst, best = min(per_variant.values()), max(per_variant.values())
        per_task[task_id] = worst
        gaps[task_id] = round(best - worst, 4)
    if not per_task:
        return None, {"tasks_measured": 0}
    return statistics.fmean(per_task.values()), {
        "tasks_measured": len(per_task),
        "max_gap": max(gaps.values()) if gaps else 0.0,
        "per_task_gap": gaps,
    }


# ---------------------------------------------------------------------------------------------
# suite summary
# ---------------------------------------------------------------------------------------------
@dataclass
class SuiteSummary:
    label: str
    standard: str
    parameters: list[ParameterScore]
    axs: float | None
    coverage: float
    provisional: bool
    totals: dict
    task_table: list[dict]
    harness_reliability: dict
    manifest: dict
    warnings: list[str] = field(default_factory=list)
    # The same composite including secondary-only parameters. Always lower-confidence than `axs`
    # because those scores rest on incidental assertions rather than the parameter's own probe;
    # reported so the effect of thin evidence is visible instead of hidden inside one number.
    axs_inclusive: float | None = None
    secondary_only: list[str] = field(default_factory=list)
    # Per-difficulty-level pass rates: the capability curve. A model that clears L1/L2 and collapses
    # at L3 is a different proposition from one that is mediocre everywhere, and the aggregate score
    # cannot distinguish them.
    complexity_curve: dict[str, dict] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "standard": self.standard,
            "label": self.label,
            "axs": None if self.axs is None else round(self.axs, 2),
            "axs_inclusive": None if self.axs_inclusive is None else round(self.axs_inclusive, 2),
            "coverage": round(self.coverage, 4),
            "provisional": self.provisional,
            "secondary_only_parameters": self.secondary_only,
            "complexity_curve": self.complexity_curve,
            "parameters": [p.to_dict() for p in self.parameters],
            "totals": self.totals,
            "harness_reliability": self.harness_reliability,
            "task_table": self.task_table,
            "manifest": self.manifest,
            "warnings": self.warnings,
        }


def score_suite(
    records: list[InstanceRecord],
    *,
    label: str,
    manifest: dict | None = None,
    standard: str = "IQB-14@1.0.0",
) -> SuiteSummary:
    if not records:
        raise ValueError("score_suite() called with no instances")

    declared_fairness = any(
        a.parameter == "fairness" for r in records for a in r.assertions
    ) or any((r.spec.get("kind") == "fairness") for r in records)

    # -- cross-run parameters ---------------------------------------------------
    stability_score, stability_detail = _stability(records)
    robustness_score, robustness_detail = _robustness(records)
    fairness_score, fairness_detail = _fairness(records, declared_fairness)
    cross_run = {"stability": (stability_score, stability_detail), "robustness": (robustness_score, robustness_detail)}

    scores: dict[str, float | None] = {}
    parameter_scores: list[ParameterScore] = []

    for parameter in PARAMETERS:
        graded, n_assertions, n_judged, n_used = _graded_for(records, parameter.id)
        pass_rate, passed, total = _pass_rates_by_assertion(records, parameter.id)
        entry = ParameterScore(
            parameter=parameter,
            graded=graded,
            pass_rate=pass_rate,
            n_assertions=n_assertions,
            n_judged=n_judged,
            ci=wilson_interval(passed, total) if total else None,
        )

        if parameter.id in cross_run:
            value, detail = cross_run[parameter.id]
            entry.score = value
            entry.n_instances = int(detail.get("tasks_measured", 0))
            entry.detail = detail
        elif parameter.id == "fairness":
            # Fairness is a cross-run property by construction (it compares paired variants), so it
            # uses the paired estimator whenever one exists. A naive pass-rate blend would be
            # worse than useless here: it averages the two groups and hides the exact gap the
            # parameter exists to detect.
            if fairness_score is not None:
                entry.score = fairness_score
                entry.n_instances = int(fairness_detail.get("tasks_measured", 0))
                entry.detail = fairness_detail
            elif pass_rate is not None:
                entry.score = pass_rate
                entry.n_instances = n_used
                entry.detail = {"note": "no paired variants -- fell back to the assertion pass rate"}
            else:
                entry.score = graded
        elif graded is None:
            entry.score = None
            entry.detail = {"note": "no task in this suite produces evidence for this parameter"}
        elif parameter.blend == "pass_rate":
            # Headline is the hard pass rate; the graded number is kept for diagnosing partial work.
            entry.score = pass_rate if pass_rate is not None else graded
            entry.n_instances = n_used
        else:
            entry.score = graded
            entry.n_instances = n_used

        scores[parameter.id] = entry.score
        parameter_scores.append(entry)

    # Evidence level: did this parameter's own probe run, or is the score resting on incidental
    # assertions from tasks written for other parameters?
    for entry in parameter_scores:
        kind = entry.parameter.primary_evidence
        if kind is None:
            entry.evidence_level = "primary" if entry.n_assertions else "unmeasured"
            continue
        primary_ran = any(
            record.attributable
            and record.kind == kind
            and any(a.parameter == entry.parameter.id for a in record.assertions)
            for record in records
        )
        entry.evidence_level = "primary" if primary_ran else ("secondary" if entry.n_assertions else "unmeasured")

    # Attach the concrete failing assertions per parameter so the scorecard can name what broke
    # instead of only how much. Recorded for every parameter, not just failures, because "which
    # checks ran" is part of the evidence.
    for entry in parameter_scores:
        failures = []
        for record in records:
            if not record.attributable:
                continue
            for result in record.assertions:
                if result.parameter != entry.parameter.id or result.passed:
                    continue
                failures.append(
                    {
                        "task": record.task_id,
                        "attempt": record.attempt,
                        "variant": record.variant,
                        "type": result.type,
                        "evidence": result.evidence,
                        "critical": result.critical,
                    }
                )
        if failures:
            entry.detail = {**(entry.detail or {}), "failing_assertions": failures}

    attributable = [r for r in records if r.attributable]
    totals = {
        "instances": len(records),
        "attributable_instances": len(attributable),
        "passed_instances": sum(1 for r in attributable if r.passed),
        "total_tool_calls": sum(len(r.state.events) for r in records),
        "failed_tool_calls": sum(len(r.state.failed_tool_calls) for r in records),
        "total_input_tokens": sum(r.state.input_tokens for r in records),
        "total_output_tokens": sum(r.state.output_tokens for r in records),
        "total_cost_usd": _sum_cost(records),
        "wall_clock_s": round(sum(r.state.wall_clock_s for r in records), 1),
        "judged_assertions": sum(1 for r in records for a in r.assertions if a.judged),
        "total_assertions": sum(len(r.assertions) for r in records),
        "unique_tasks": len({r.task_id for r in records}),
    }
    cost = totals["total_cost_usd"]
    solved = totals["passed_instances"]
    totals["cost_per_solved_task_usd"] = round(cost / solved, 4) if (cost is not None and solved) else None

    harness_reliability = _harness_reliability(records)
    warnings = _warnings(totals, manifest or {}, records)

    # Strict composite: only parameters whose own primary probe produced a verdict. A parameter
    # resting on incidental assertions from other tasks is excluded from the headline and reported
    # as such, because including it lets an unrun probe read as a measured pass.
    primary_scores = {e.parameter.id: e.score for e in parameter_scores if e.evidence_level == "primary"}
    axs = axs_from_scores(primary_scores)
    cov = coverage(primary_scores)
    axs_inclusive = axs_from_scores(scores)
    secondary_only = [e.parameter.id for e in parameter_scores if e.evidence_level == "secondary"]
    if secondary_only:
        warnings.append(
            "these parameters have only incidental evidence (their own probe did not run) and are "
            "excluded from the headline AXS: " + ", ".join(secondary_only)
        )
    return SuiteSummary(
        label=label,
        standard=standard,
        parameters=parameter_scores,
        axs=axs,
        coverage=cov,
        provisional=cov < 0.999,
        axs_inclusive=axs_inclusive,
        secondary_only=secondary_only,
        complexity_curve=_complexity_curve(records),
        totals=totals,
        task_table=_task_table(records),
        harness_reliability=harness_reliability,
        manifest=manifest or {},
        warnings=warnings,
    )


def _complexity_curve(records: list[InstanceRecord]) -> dict[str, dict]:
    """Pass rate per difficulty level, from task specs recorded in the artifacts.

    Levels come from the stored spec rather than from a live task-file read, so re-scoring an old
    artifact after the suite changed still reports the levels that run actually used -- otherwise
    relabelling a task would silently rewrite historical results.
    """
    groups: dict[str, list[InstanceRecord]] = {}
    for record in records:
        level = str((record.spec or {}).get("complexity", "L2"))
        groups.setdefault(level, []).append(record)
    curve: dict[str, dict] = {}
    for level in sorted(groups):
        usable = [r for r in groups[level] if r.attributable]
        if not usable:
            curve[level] = {"tasks": len(groups[level]), "attributable": 0, "pass_rate": None}
            continue
        passed = sum(1 for r in usable if r.passed)
        ci = wilson_interval(passed, len(usable))
        curve[level] = {
            "tasks": len({r.task_id for r in groups[level]}),
            "instances": len(usable),
            "passed": passed,
            "pass_rate": round(passed / len(usable), 4),
            "ci95": None if ci is None else [round(ci[0], 3), round(ci[1], 3)],
        }
    return curve


def _sum_cost(records: list[InstanceRecord]) -> float | None:
    priced = [r.state.cost_usd for r in records if r.state.cost_usd is not None]
    if not priced:
        return None
    return round(sum(priced), 6)


def _harness_reliability(records: list[InstanceRecord]) -> dict:
    counts: dict[str, int] = {}
    for record in records:
        counts[record.state.status] = counts.get(record.state.status, 0) + 1
    attributable = sum(v for k, v in counts.items() if k not in NON_ATTRIBUTABLE)
    return {
        "status_counts": counts,
        "attributable_rate": round(attributable / len(records), 4) if records else 0.0,
        "note": (
            "harness_error/env_error instances are excluded from capability scores -- they measure "
            "the benchmark, not the agent"
        ),
    }


def _task_table(records: list[InstanceRecord]) -> list[dict]:
    groups: dict[str, list[InstanceRecord]] = {}
    for record in records:
        groups.setdefault(record.task_id, []).append(record)
    table = []
    for task_id, group in sorted(groups.items()):
        usable = [r for r in group if r.attributable]
        n = len(usable)
        c = sum(1 for r in usable if r.passed)
        ci = wilson_interval(c, n)
        costs = [r.state.cost_usd for r in usable if r.state.cost_usd is not None]
        table.append(
            {
                "task_id": task_id,
                "kind": group[0].kind,
                "attempts": len(group),
                "attributable": n,
                "passed": c,
                "pass_rate": round(c / n, 4) if n else None,
                "ci95": None if ci is None else [round(ci[0], 3), round(ci[1], 3)],
                "pass@k_2": None if n < 2 else round(pass_at_k(n, c, 2), 4),
                "pass^k_2": None if n < 2 else round(pass_pow_k(n, c, 2), 4),
                "statuses": _status_counts(group),
                "mean_tool_calls": round(statistics.fmean([len(r.state.events) for r in usable]), 1) if usable else None,
                "mean_seconds": round(statistics.fmean([r.state.wall_clock_s for r in usable]), 1) if usable else None,
                "cost_usd": round(sum(costs), 5) if costs else None,
            }
        )
    return table


def _status_counts(group: list[InstanceRecord]) -> dict[str, int]:
    out: dict[str, int] = {}
    for record in group:
        out[record.state.status] = out.get(record.state.status, 0) + 1
    return out


def _warnings(totals: dict, manifest: dict, records: list[InstanceRecord] | None = None) -> list[str]:
    warn: list[str] = []
    if records and any(not r.state.replies_available for r in records):
        warn.append(
            "some artifacts predate reply persistence -- answer-based parameters (P6/P7/P8) are "
            "unmeasurable from them and are reported as n/a rather than assumed consistent"
        )
    if totals["judged_assertions"]:
        share = totals["judged_assertions"] / max(1, totals["total_assertions"])
        warn.append(f"{share:.0%} of assertions were model-judged (non-deterministic) -- judge model: {manifest.get('judge_model', 'unset')}")
    if totals["total_cost_usd"] is None:
        warn.append("no price declared for this model -- cost metrics are unavailable, not zero")
    if totals["unique_tasks"] < 14:
        warn.append(f"only {totals['unique_tasks']} unique tasks -- parameter coverage is partial")
    unpriced = [p for p in (manifest.get("excluded_parameters") or [])]
    if unpriced:
        warn.append(f"parameters with no evidence: {', '.join(unpriced)}")
    if manifest.get("budget_enforced") is False:
        warn.append("budget enforcement could not be installed for this provider -- step/time/cost ceilings were NOT applied")
    return warn


def parameter_table(summary: SuiteSummary) -> list[dict]:
    """Flat table for CSV/plots: one row per parameter, both blends, always in standard order."""
    rows = []
    for entry in summary.parameters:
        rows.append(entry.to_dict())
    assert [r["id"] for r in rows] == list(PARAMETER_IDS)
    return rows


def weakest(summary: SuiteSummary, n: int = 3) -> list[str]:
    scored = [(e.score, e.parameter.id) for e in summary.parameters if e.score is not None]
    scored.sort(key=lambda pair: pair[0])
    return [pid for _, pid in scored[:n]]


__all__ = [
    "InstanceRecord",
    "ParameterScore",
    "SuiteSummary",
    "score_suite",
    "parameter_table",
    "weakest",
    "pass_at_k",
    "pass_pow_k",
    "wilson_interval",
    "BY_ID",
]
