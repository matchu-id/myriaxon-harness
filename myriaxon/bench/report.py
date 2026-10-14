"""Reporting: the part that decides whether anyone trusts the numbers.

Every report this module emits carries four things that a bare score table omits and that a
benchmark is worthless without:

1. **Confidence intervals.** A pass rate from 8 instances and one from 800 are different claims.
2. **Coverage.** Which of the 14 parameters had any evidence at all, and an explicit "provisional"
   label when coverage is incomplete, so a partial suite can never be quoted as an IQB-14 score.
3. **Harness reliability.** How many instances were lost to harness/credential/network problems.
   Those are excluded from capability scoring but printed, because a run that lost a third of its
   instances has not measured what it claims to have measured.
4. **Provenance.** Model, sampling parameters, budgets, price-table date, and which scores came
   from a model judge.
"""
from __future__ import annotations

from pathlib import Path

from .scoring import SuiteSummary

_BAR = "\u2588"
_EMPTY = "\u00b7"


def score_bar(score: float | None, width: int = 10) -> str:
    if score is None:
        return _EMPTY * width
    filled = int(round(max(0.0, min(1.0, score)) * width))
    return _BAR * filled + _EMPTY * (width - filled)


def scorecard(summary: SuiteSummary, *, top_n_failures: int = 10) -> str:
    m = summary.manifest
    target = m.get("target", {})
    totals = summary.totals
    lines: list[str] = []
    add = lines.append

    add(f"# {summary.standard} scorecard -- {summary.label}")
    add("")
    axs = "n/a" if summary.axs is None else f"{summary.axs:.1f}"
    provisional = " **(PROVISIONAL)**" if summary.provisional else ""
    add(f"**AXS (Agentic eXcellence Score): {axs} / 100**{provisional}  ")
    add(f"Evidence coverage: {summary.coverage:.0%} of IQB-14 weight (primary probes only)")
    if summary.axs_inclusive is not None and summary.axs is not None and abs(summary.axs_inclusive - summary.axs) > 0.05:
        add(f"Including secondary-only evidence: {summary.axs_inclusive:.1f} / 100")
    add("")
    add("| | |")
    add("|---|---|")
    add(f"| Target | `{target.get('name', '?')}` |")
    add(f"| Provider / model | `{target.get('provider', '?')}` / `{target.get('model', '?')}` |")
    add(f"| Sampling | temperature={target.get('temperature')} seed={target.get('seed')} effort={target.get('effort')} |")
    add(f"| Instances | {totals['instances']} ({totals['unique_tasks']} tasks, {totals['attributable_instances']} attributable) |")
    add(f"| Tool calls | {totals['total_tool_calls']} total, {totals['failed_tool_calls']} errored |")
    add(f"| Tokens | {totals['total_input_tokens']:,} in / {totals['total_output_tokens']:,} out |")
    cost = totals["total_cost_usd"]
    add(f"| Cost | {'unavailable (no price for this model)' if cost is None else f'${cost:.4f}'} |")
    if totals.get("cost_per_solved_task_usd") is not None:
        add(f"| Cost per solved instance | ${totals['cost_per_solved_task_usd']:.4f} |")
    add(f"| Wall clock | {totals['wall_clock_s']:.0f}s |")
    add(f"| Judge-derived assertions | {totals['judged_assertions']}/{totals['total_assertions']} |")
    add("")
    add("## The 14 parameters")
    add("")
    add("| # | Parameter | Score | Pass rate (95% CI) | Graded | Evidence | Verdict |")
    add("|---|---|---|---|---|---|---|")
    for entry in sorted(summary.parameters, key=lambda e: e.parameter.index):
        p = entry.parameter
        score = "\u2014" if entry.score is None else f"{entry.score:.3f}"
        bar = score_bar(entry.score)
        if entry.pass_rate is None:
            rate = "n/a"
        elif entry.ci:
            rate = f"{entry.pass_rate:.2f} [{entry.ci[0]:.2f}, {entry.ci[1]:.2f}]"
        else:
            rate = f"{entry.pass_rate:.2f}"
        graded = "\u2014" if entry.graded is None else f"{entry.graded:.3f}"
        if entry.n_assertions == 0:
            evidence = "none"
        else:
            evidence = f"{entry.n_assertions} assertions"
            if entry.n_judged:
                evidence += f" ({entry.n_judged} judged)"
        verdict = {"primary": "measured", "secondary": "**incidental only**", "unmeasured": "not measured"}[
            entry.evidence_level
        ]
        if entry.evidence_level == "secondary":
            score += " *"
        add(f"| {p.index} | {p.name} `{bar}` | {score} | {rate} | {graded} | {evidence} | {verdict} |")
    add("")
    add("_Score is the parameter's headline number: pass rate for OUTCOME/ASSURANCE parameters, "
        "graded evidence for BEHAVIOR parameters (see docs/IQB-14.md). `n/a` means no task produced "
        "evidence. `**incidental only**` (\\*) means the parameter's own probe did not produce a "
        "verdict and the score rests on assertions inside tasks written for other parameters -- such "
        "parameters are excluded from the headline AXS._")
    add("")

    if summary.complexity_curve:
        add("## Capability curve by difficulty")
        add("")
        add("| Level | Meaning | Tasks | Pass rate (95% CI) |")
        add("|---|---|---|---|")
        from .tasks import COMPLEXITY_LEVELS

        for level, row in sorted(summary.complexity_curve.items()):
            rate = "n/a" if row.get("pass_rate") is None else f"{row['pass_rate']:.2f}"
            if row.get("ci95"):
                rate += f" [{row['ci95'][0]:.2f}, {row['ci95'][1]:.2f}]"
            add(f"| {level} | {COMPLEXITY_LEVELS.get(level, '')} | {row.get('tasks', 0)} | {rate} |")
        add("")
        add("_A drop between adjacent levels is a capability cliff: the harness can do the easier tier "
            "and not the next one. An aggregate score cannot show this._")
        add("")

    add("## Per-task detail")
    add("")
    add("| Task | Kind | Att. | Passed | Rate | pass@2 | pass^2 | Steps | Sec | Cost |")
    add("|---|---|---|---|---|---|---|---|---|---|")
    for row in summary.task_table:
        add(
            f"| {row['task_id']} | {row['kind']} | {row['attributable']}/{row['attempts']} | {row['passed']} | "
            f"{_fmt(row['pass_rate'])} | {_fmt(row['pass@k_2'])} | {_fmt(row['pass^k_2'])} | "
            f"{_fmt(row['mean_tool_calls'])} | {_fmt(row['mean_seconds'])} | {_fmt(row['cost_usd'])} |"
        )
    add("")

    failures = _failure_lines(summary)
    if failures:
        add(f"## Failures ({len(failures)}; first {min(top_n_failures, len(failures))})")
        add("")
        for line in failures[:top_n_failures]:
            add(f"- {line}")
        add("")

    detail = _assertion_failures(summary)
    if detail:
        add(f"## Failing assertions ({len(detail)})")
        add("")
        add("| Task | Assertion | Parameter | Evidence |")
        add("|---|---|---|---|")
        for row in detail[:40]:
            add(f"| {row['task']} | `{row['type']}` | {row['parameter']} | {row['evidence'][:220]} |")
        add("")

    add("## Harness reliability")
    add("")
    add(f"- Attributable instances: {summary.harness_reliability['attributable_rate']:.0%}")
    for status, count in sorted(summary.harness_reliability["status_counts"].items()):
        add(f"- `{status}`: {count}")
    add("")
    add("_`harness_error` and `env_error` instances are excluded from capability scores -- they "
        "measure the benchmark, not the agent. `budget_exceeded` and `timeout` ARE attributed to "
        "the agent: the budget was declared in advance and is part of the task._")
    add("")

    if summary.warnings:
        add("## Warnings")
        add("")
        for warning in summary.warnings:
            add(f"- {warning}")
        add("")

    add("## Provenance")
    add("")
    add("```json")
    import json

    add(json.dumps({k: v for k, v in m.items() if k not in ("task_ids",)}, indent=2, default=str))
    add("```")
    add("")
    return "\n".join(lines)


def _fmt(value) -> str:
    if value is None:
        return "\u2014"
    if isinstance(value, float):
        return f"{value:.3f}" if abs(value) < 100 else f"{value:.1f}"
    return str(value)


def _assertion_failures(summary: SuiteSummary) -> list[dict]:
    """Per-assertion failure rows, read from the same artifacts the scorecard was built from.

    A scorecard that says "P12 = 0.00" without saying *which assertion* failed and on what evidence
    is not auditable. This is what makes a failure actionable without opening the JSON.
    """
    rows: list[dict] = []
    for entry in summary.parameters:
        for failure in (entry.detail or {}).get("failing_assertions", []):
            rows.append({"parameter": entry.parameter.name, **failure})
    rows.sort(key=lambda r: (r["task"], r["type"]))
    return rows


def _failure_lines(summary: SuiteSummary) -> list[str]:
    """Read failures out of the task table plus the parameter detail, so the report answers
    'what actually broke' and not only 'how did it score'."""
    lines: list[str] = []
    for row in summary.task_table:
        if row["pass_rate"] is not None and row["pass_rate"] < 1.0:
            bad = {k: v for k, v in row["statuses"].items() if k != "completed"}
            suffix = f" statuses={bad}" if bad else ""
            lines.append(f"`{row['task_id']}` ({row['kind']}) passed {row['passed']}/{row['attributable']}{suffix}")
    for entry in summary.parameters:
        detail = entry.detail or {}
        if entry.parameter.id == "robustness" and detail.get("phrasing_sensitive"):
            lines.append(f"P7 phrasing-sensitive tasks: {', '.join(detail['phrasing_sensitive'])}")
        if entry.parameter.id == "fairness" and detail.get("per_task_gap"):
            gaps = {k: v for k, v in detail["per_task_gap"].items() if v and v > 0}
            if gaps:
                lines.append(f"P11 group gaps: {gaps}")
        if entry.parameter.id == "stability" and detail.get("components"):
            unstable = {k: v for k, v in detail["components"].items() if v["answer_consistency"] < 0.9}
            if unstable:
                lines.append(f"P6 low answer-consistency tasks: {sorted(unstable)}")
    return lines


def compare_markdown(base: SuiteSummary, other: SuiteSummary) -> str:
    """Two-run comparison. This is the shape every real benchmarking question takes ("did the
    change help?"), so it is a first-class output rather than something re-derived by hand."""
    lines = [f"# {other.standard} comparison", "", f"- **A (base):** {base.label}", f"- **B (new):** {other.label}", ""]
    axs_a, axs_b = base.axs, other.axs
    if axs_a is not None and axs_b is not None:
        delta = axs_b - axs_a
        arrow = "\u2191" if delta > 0 else ("\u2193" if delta < 0 else "=")
        lines += [f"**AXS: {axs_a:.1f} -> {axs_b:.1f} ({arrow} {delta:+.1f})**", ""]
    lines += ["| # | Parameter | A | B | Delta |", "|---|---|---|---|---|"]
    by_id = {e.parameter.id: e for e in other.parameters}
    for entry in sorted(base.parameters, key=lambda e: e.parameter.index):
        other_entry = by_id.get(entry.parameter.id)
        a, b = entry.score, (other_entry.score if other_entry else None)
        if a is None or b is None:
            delta_text = "n/a"
        else:
            d = b - a
            delta_text = f"{d:+.3f}" + (" \u2191" if d > 0.0005 else (" \u2193" if d < -0.0005 else ""))
        # Kept out of the f-string's own expression nesting on purpose: PEP 701 (nested same-type
        # quotes inside an f-string) only became legal in Python 3.12, and this package declares
        # requires-python >= 3.10.
        a_cell = "\u2014" if a is None else f"{a:.3f}"
        b_cell = "\u2014" if b is None else f"{b:.3f}"
        lines.append(
            f"| {entry.parameter.index} | {entry.parameter.name} | {a_cell} | {b_cell} | {delta_text} |"
        )
    lines.append("")
    lines += ["## Totals", "", "| Metric | A | B |", "|---|---|---|"]
    for key in (
        "instances",
        "attributable_instances",
        "total_tool_calls",
        "failed_tool_calls",
        "total_input_tokens",
        "total_output_tokens",
        "total_cost_usd",
        "cost_per_solved_task_usd",
        "wall_clock_s",
    ):
        lines.append(f"| {key} | {_fmt(base.totals.get(key))} | {_fmt(other.totals.get(key))} |")
    lines.append("")
    regressions = [
        e.parameter.name
        for e in base.parameters
        if e.score is not None
        and by_id.get(e.parameter.id) is not None
        and by_id[e.parameter.id].score is not None
        and by_id[e.parameter.id].score < e.score - 0.02
    ]
    if regressions:
        lines += [f"**Regressions (>2 points):** {', '.join(regressions)}", ""]
    return "\n".join(lines)


def write_report(summary: SuiteSummary, out_dir: Path, *, stem: str | None = None) -> tuple[Path, Path]:
    import json

    out_dir.mkdir(parents=True, exist_ok=True)
    name = stem or _slug(summary.label)
    json_path = out_dir / f"{name}.scorecard.json"
    md_path = out_dir / f"{name}.scorecard.md"
    json_path.write_text(json.dumps(summary.to_dict(), indent=2, default=str), encoding="utf-8")
    md_path.write_text(scorecard(summary), encoding="utf-8")
    return json_path, md_path


def _slug(text: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in text)[:80].strip("-") or "run"
