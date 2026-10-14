"""promptfoo interop.

promptfoo is the strongest open tool for the adversarial half of agentic evaluation, and IQB-14
deliberately does not reimplement it. This module is the bridge in both directions:

* `export_promptfoo()` turns IQB adversarial/secret/advisory tasks into a promptfoo config whose
  provider is Myriaxon itself (`redteam/myriaxon_provider.py`), so the same task corpus drives both
  harnesses and the two sets of results can be cross-checked.
* `import_promptfoo()` reads a promptfoo JSON results file and reduces it into IQB evidence, so a
  red-team run in promptfoo lands in the same scorecard as everything else instead of in a
  separate spreadsheet.

Conventions taken from promptfoo on purpose: assertions are declarative and thresholded, results
carry `pass`/`score`/`reason` per assertion, and `--output` JSON is the interchange format. What is
deliberately NOT taken: promptfoo lets a single assertion carry a grading rubric written in prose
by the task author; IQB requires the rubric's judge model to be named in the manifest so the share
of judged scoring is always reportable.
"""
from __future__ import annotations

import json
from pathlib import Path

from .tasks import TaskSpec

_ADVERSARIAL_KINDS = ("adversarial", "secret_handling", "guardrail")


def _assertion_to_promptfoo(assertion: dict) -> dict | None:
    """Map an IQB assertion to the closest promptfoo assertion type.

    Only the subset with a genuine promptfoo equivalent is mapped; anything else is returned as
    None and reported by the caller, rather than being silently approximated into a check that
    does not mean the same thing in both harnesses.
    """
    atype = assertion.get("type")
    if atype == "contains":
        return {"type": "contains", "value": assertion.get("value")}
    if atype == "not-contains":
        return {"type": "not-contains", "value": assertion.get("value")}
    if atype == "regex":
        return {"type": "regex", "value": assertion.get("value")}
    if atype == "similar":
        return {"type": "similar", "value": assertion.get("value"), "threshold": assertion.get("threshold", 0.6)}
    if atype == "refuses":
        return {"type": "not-contains", "value": assertion.get("must_not_contain", [])}
    if atype == "secret-safe":
        return {"type": "not-contains", "value": assertion.get("value", [])}
    if atype == "llm-rubric":
        points = assertion.get("points", [])
        text = "; ".join(p.get("text", "") for p in points)
        return {"type": "llm-rubric", "value": text}
    return None


def export_promptfoo(
    suite: list[TaskSpec],
    out_path: Path,
    *,
    provider_script: str = "redteam/myriaxon_provider.py",
    kinds: tuple[str, ...] = _ADVERSARIAL_KINDS,
) -> dict:
    tests, unmapped = [], []
    for spec in suite:
        if spec.kind not in kinds:
            continue
        assertions = []
        for assertion in spec.assertions:
            mapped = _assertion_to_promptfoo(assertion)
            if mapped is None:
                unmapped.append({"task": spec.id, "type": assertion.get("type")})
            else:
                assertions.append(mapped)
        if not assertions:
            continue
        tests.append(
            {
                "description": f"{spec.id} ({spec.kind})",
                "vars": {"prompt": spec.prompt},
                "assert": assertions,
                "metadata": {"iqb_task": spec.id, "iqb_kind": spec.kind},
            }
        )

    config = {
        "description": "IQB-14 adversarial subset, exported for promptfoo",
        "prompts": ["{{prompt}}"],
        "providers": [{"id": f"exec:{provider_script}", "label": "myriaxon"}],
        "tests": tests,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    return {"exported_tests": len(tests), "unmapped_assertions": unmapped, "path": str(out_path)}


def import_promptfoo(results_path: Path, *, parameter: str = "adversarial_risk") -> list[dict]:
    """Reduce a promptfoo JSON results file into IQB per-instance evidence records.

    promptfoo's shape has moved between versions, so both the older `results.results[]` and the
    newer `results[]` layouts are accepted, and an unrecognized shape raises rather than returning
    an empty list -- a silent zero here would read as "the agent passed all red-team cases".
    """
    data = json.loads(Path(results_path).read_text(encoding="utf-8-sig"))
    rows = data.get("results", {}).get("results") if isinstance(data.get("results"), dict) else data.get("results")
    if rows is None:
        raise ValueError(f"unrecognized promptfoo results shape in {results_path}")
    out = []
    for row in rows:
        task_id = (row.get("metadata") or {}).get("iqb_task") or row.get("description") or "promptfoo-case"
        component_results = row.get("gradingResult", {}).get("componentResults") or []
        passed = bool(row.get("success", row.get("gradingResult", {}).get("pass", False)))
        score = row.get("score")
        if score is None:
            score = 1.0 if passed else 0.0
        out.append(
            {
                "task_id": task_id,
                "parameter": parameter,
                "passed": passed,
                "score": float(score),
                "weight": 1.0,
                "type": "promptfoo",
                "evidence": (row.get("response", {}) or {}).get("output", "")[:1000],
                "detail": {"components": len(component_results), "source": str(results_path)},
                "judged": bool(row.get("gradingResult", {}).get("componentResults")),
                "critical": True,
            }
        )
    return out
