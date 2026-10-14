"""Task specifications: portable, versioned, data-only.

A task is a directory containing `task.json` plus its fixture files. Nothing in a task file is
code except two explicitly-marked escape hatches (`script` assertions, which run a verifier in the
sandboxed workspace, and the `setup` command). Keeping the spec data-only is what makes the
standard enforceable: the suite can be linted, counted, subset, sharded, and re-scored from
artifacts by anyone, including a CI job with no API key.

Field reference (all optional unless stated):

    id            stable identifier, unique across the suite (required)
    kind          which IQB evidence family this task feeds (required)
    prompt        the instruction handed to the agent (required)
    files         {"relative/path": "content"} fixtures written before the run
    setup         shell command run in the workspace before the run (recorded in the manifest)
    assertions    list of assertion objects (see assertions.py for the type registry)
    budget        per-instance ceilings (see guard.Budget)
    repeats       number of attempts, used by consistent/at-least-k scoring
    variants      named prompt/assertion overrides, one attempt each, in order
    notes         free text explaining what failure this task is designed to catch

`kind` values line up with IQB parameter evidence: goal, tool_discipline, guardrail, plan_trace,
drift, consistency, golden, rubric, grounding, fairness, adversarial, abstention, secret_handling.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .guard import Budget

KINDS = (
    "goal",
    "tool_discipline",
    "guardrail",
    "plan_trace",
    "drift",
    "consistency",
    "golden",
    "rubric",
    "grounding",
    "fairness",
    "adversarial",
    "abstention",
    "secret_handling",
)

# Difficulty ladder. A single aggregate score hides the thing a user deciding whether to trust a
# small model actually needs: *where* it stops working. A model that passes every L1 task and no L3
# task is a very different proposition from one that passes half of each, even when the two have the
# same mean.
COMPLEXITY_LEVELS = {
    "L1": "single step, one file, one tool call",
    "L2": "a few steps, verification required",
    "L3": "multi-file, ordered investigation then change",
    "L4": "long horizon, drift/ambiguity, or adversarial pressure",
}

_UNSAFE_ID = set("\\/:*?\"<>| ")


@dataclass
class TaskSpec:
    id: str
    kind: str
    prompt: str
    path: Path
    files: dict[str, str] = field(default_factory=dict)
    setup: str | None = None
    assertions: list[dict] = field(default_factory=list)
    budget: Budget = field(default_factory=Budget)
    repeats: int = 1
    variants: list[dict] = field(default_factory=list)
    notes: str = ""
    tags: list[str] = field(default_factory=list)
    # For kind == "consistency" only: "stability" (identical prompt repeated) or "robustness"
    # (reworded prompt). Routing is explicit so a task can never drift between P6 and P7.
    consistency_mode: str | None = None
    # L1-L4 difficulty (see COMPLEXITY_LEVELS). Reported as a per-level curve so a capability cliff
    # is visible instead of being averaged away.
    complexity: str = "L2"

    def assertions_for_variant(self, index: int) -> list[dict]:
        if not self.variants or index >= len(self.variants):
            return self.assertions
        variant = self.variants[index]
        return list(self.assertions) + list(variant.get("assertions", []))

    def prompt_for_variant(self, index: int) -> str:
        if not self.variants or index >= len(self.variants):
            return self.prompt
        variant = self.variants[index]
        suffix = variant.get("prompt_suffix", "")
        return variant.get("prompt", self.prompt) + (("\n\n" + suffix) if suffix else "")

    def variant_name(self, index: int) -> str:
        if not self.variants or index >= len(self.variants):
            return "default"
        return self.variants[index].get("name", f"variant{index}")

    def attempt_plan(self) -> list[int]:
        """Which variant index each attempt uses.

        With declared variants, N attempts cycle through them in order -- that is what makes P6
        (same query twice) and P7 (reworded query) use *identical* machinery rather than two
        copy-pasted runners. Without variants, every attempt reuses the default prompt.
        """
        count = max(1, self.repeats)
        if not self.variants:
            return [0] * count
        return [i % len(self.variants) for i in range(count)]

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "prompt": self.prompt,
            "files": sorted(self.files),
            "setup": self.setup,
            "assertions": self.assertions,
            "budget": self.budget.to_dict(),
            "repeats": self.repeats,
            "variants": [v.get("name", f"variant{i}") for i, v in enumerate(self.variants)],
            "notes": self.notes,
            "tags": self.tags,
            "consistency_mode": self.consistency_mode,
            "complexity": self.complexity,
        }


class TaskError(ValueError):
    """A malformed task file. Raised at load time, before any API call is made."""


def _validate(spec: TaskSpec) -> None:
    if spec.kind not in KINDS:
        raise TaskError(f"{spec.id}: unknown kind {spec.kind!r} (expected one of {KINDS})")
    if not spec.prompt.strip():
        raise TaskError(f"{spec.id}: empty prompt")
    allowed = set(KINDS)
    if spec.kind not in allowed:
        raise TaskError(f"{spec.id}: kind not in KINDS")
    from .assertions import ALL_TYPES

    for assertion in spec.assertions:
        atype = assertion.get("type")
        if atype not in ALL_TYPES:
            raise TaskError(f"{spec.id}: unknown assertion type {atype!r}")
        if not assertion.get("parameter"):
            raise TaskError(f"{spec.id}: assertion {atype!r} is missing the required 'parameter' field")
    if spec.repeats < 1:
        raise TaskError(f"{spec.id}: repeats must be >= 1")


def load_task(path: Path) -> TaskSpec:
    task_file = path / "task.json" if path.is_dir() else path
    if not task_file.exists():
        raise TaskError(f"no task.json at {task_file}")
    try:
        raw = json.loads(task_file.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise TaskError(f"{task_file}: invalid JSON: {exc}") from exc

    for required in ("id", "kind", "prompt"):
        if required not in raw:
            raise TaskError(f"{task_file}: missing required field {required!r}")

    tid = str(raw["id"])
    if any(ch in _UNSAFE_ID for ch in tid):
        raise TaskError(f"{task_file}: id {tid!r} contains characters unsafe for a directory/artifact name")

    budget_raw = raw.get("budget", {})
    budget = Budget(
        max_steps=budget_raw.get("max_steps", 60),
        max_seconds=budget_raw.get("max_seconds", 900.0),
        max_cost_usd=budget_raw.get("max_cost_usd", 0.50),
        max_tokens_per_request=budget_raw.get("max_tokens_per_request"),
        max_subagent_depth=budget_raw.get("max_subagent_depth", 1),
    )

    files = raw.get("files", {})
    if not isinstance(files, dict):
        raise TaskError(f"{task_file}: 'files' must be an object of path -> content")

    consistency_mode = raw.get("consistency_mode")
    if consistency_mode is not None and consistency_mode not in ("stability", "robustness"):
        raise TaskError(f"{task_file}: consistency_mode must be 'stability' or 'robustness'")

    complexity = str(raw.get("complexity", "L2"))
    if complexity not in COMPLEXITY_LEVELS:
        raise TaskError(f"{task_file}: complexity must be one of {sorted(COMPLEXITY_LEVELS)}")

    spec = TaskSpec(
        id=tid,
        kind=str(raw["kind"]),
        prompt=str(raw["prompt"]),
        path=path if path.is_dir() else path.parent,
        files={str(k): str(v) for k, v in files.items()},
        setup=raw.get("setup"),
        assertions=list(raw.get("assertions", [])),
        budget=budget,
        repeats=int(raw.get("repeats", 1)),
        variants=list(raw.get("variants", [])),
        notes=str(raw.get("notes", "")),
        tags=list(raw.get("tags", [])),
        consistency_mode=consistency_mode,
        complexity=complexity,
    )
    _validate(spec)
    return spec


def load_suite(root: Path, *, subset: list[str] | None = None, kinds: list[str] | None = None) -> list[TaskSpec]:
    """Load every task under `root`, applying optional id/kind filters and reporting duplicates."""
    if not root.exists():
        raise TaskError(f"suite directory not found: {root}")
    candidates = sorted({p.parent for p in root.rglob("task.json")})
    suite: list[TaskSpec] = []
    seen: dict[str, Path] = {}
    for directory in candidates:
        spec = load_task(directory)
        if spec.id in seen:
            raise TaskError(f"duplicate task id {spec.id!r}: {seen[spec.id]} and {directory}")
        seen[spec.id] = directory
        if subset and spec.id not in subset:
            continue
        if kinds and spec.kind not in kinds:
            continue
        suite.append(spec)
    if not suite:
        raise TaskError(f"no tasks matched under {root} (subset={subset} kinds={kinds})")
    return suite


def materialize(spec: TaskSpec, workspace: Path) -> None:
    """Write fixtures into a fresh workspace. Fails loudly rather than partially -- an instance
    with missing fixtures produces a meaningless score, so it must become an env_error, not a fail."""
    for relative, content in spec.files.items():
        target = workspace / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
