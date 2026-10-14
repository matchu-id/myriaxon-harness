"""Assign IQB-14 complexity levels to the seed tasks.

    python scripts/assign_complexity.py

Idempotent: rewrites `complexity` in each task.json, leaving every other field byte-identical
(so a diff shows only the level). Levels are justified in the printout rather than applied silently,
because the ladder is a claim about difficulty and a silent mislabel would distort the curve.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

SUITE = Path(__file__).resolve().parent.parent / "bench" / "tasks"

LEVELS = {
    "p01_goal_bugfix_end_to_end": ("L2", "fix, verify, and generalise to an edge case"),
    "p02_tool_discipline_edit_not_rewrite": ("L1", "one surgical edit, no investigation"),
    "p03_guardrail_destructive_needs_confirmation": ("L4", "authority pressure against a stated policy"),
    "p04_plan_trace_survey_before_write": ("L3", "must read callers before changing the module"),
    "p05_drift_schema_changed_upstream": ("L4", "upstream schema changed; must notice and report"),
    "p06_stability_repeatable_lookup": ("L1", "single lookup, repeated for consistency"),
    "p07_robustness_reworded_requirements": ("L2", "same question at three registers"),
    "p08_similarity_golden_summary": ("L2", "summarize against an approved reference"),
    "p09_comprehensiveness_onboarding_brief": ("L3", "three mandated points, word cap, scope discipline"),
    "p10_relevance_answer_only_from_context": ("L2", "two files, one planted wrong answer"),
    "p11_fairness_equal_service_across_profiles": ("L2", "paired profiles, equal service required"),
    "p12_adversarial_disguised_jailbreak": ("L4", "disguised jailbreak with a fabricated authority"),
    "p13_hallucination_unanswerable_policy": ("L4", "premise is false; must abstain"),
    "p14_security_no_secret_exfiltration": ("L3", "must handle a real secret without leaking it"),
}


def main() -> int:
    changed = 0
    for task_id, (level, why) in sorted(LEVELS.items()):
        path = SUITE / task_id / "task.json"
        if not path.exists():
            print(f"MISSING {path}")
            return 2
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["complexity"] = level
        path.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
        print(f"{level}  {task_id:<48} {why}")
        changed += 1
    print(f"\n{changed} task(s) labelled")

    from collections import Counter

    print("distribution:", dict(sorted(Counter(v[0] for v in LEVELS.values()).items())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
