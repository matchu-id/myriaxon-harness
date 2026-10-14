"""Benchmark a self-directed Locus, not a session agent.

The existing IQB-14 bench measures the SESSION agent: a prompt goes in, a transcript comes out, the
assertions grade the reply. A Locus is a different subject with a different failure mode, and grading
it with the session harness would answer the wrong question.

What actually matters for an autonomous agent, and what this measures:

  * does it produce the ARTIFACT, on disk, without a human in the loop?
  * does it stay inside its workspace while doing so?
  * does it invent work, or fabricate results, when it cannot do the thing?
  * how many steps and how much money does one task cost?

The assertions are the SAME engine the session bench uses (`bench.assertions.run_assertions`), so a
task can state its success criteria without learning a second language and the two suites cannot
disagree about what "passing" means.

Deliberately NOT included: an LLM judge. Every task here is graded by a deterministic check -- does
the file parse, does the script print OK, is the number right. A judge would add cost and noise to
questions that have exact answers, and the point of this suite is to be cheap enough to run often.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

TASK_DIR = Path(__file__).resolve().parent / "locus_tasks"


@dataclass
class LocusTaskResult:
    task_id: str
    passed: bool
    steps_used: int = 0
    failures: int = 0
    seconds: float = 0.0
    cost_usd: float = 0.0
    assertions: list[dict] = field(default_factory=list)
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "task": self.task_id,
            "passed": self.passed,
            "steps_used": self.steps_used,
            "failures": self.failures,
            "seconds": round(self.seconds, 1),
            "cost_usd": round(self.cost_usd, 6),
            "error": self.error,
            "assertions": self.assertions,
        }


def discover_tasks() -> list[dict]:
    """Every locus task on disk, sorted so a run is reproducible."""
    if not TASK_DIR.exists():
        return []
    found = []
    for path in sorted(TASK_DIR.glob("*/task.json")):
        try:
            task = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        task.setdefault("id", path.parent.name)
        task["_dir"] = str(path.parent)
        found.append(task)
    return found


def seed_workspace(task: dict, workspace: Path) -> None:
    """Write the task's fixture files, so a Locus starts with the same inputs every run."""
    for name, content in (task.get("files") or {}).items():
        target = workspace / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    for name, content in (task.get("files_binary_hex") or {}).items():
        target = workspace / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(bytes.fromhex(content))


def grade(task: dict, workspace: Path, *, final_text: str = "") -> list[dict]:
    """Run the task's assertions through the shared engine and return plain dicts."""
    from . import assertions as bench_assertions

    # A minimal state object: the locus suite grades ARTIFACTS on disk, so the tool-call assertions
    # are not used here and a real RunState (which records every call) would be cost without purpose.
    class _State:
        def __init__(self) -> None:
            self.calls: list[dict] = []
            self.final_text = final_text

    ctx = bench_assertions.AssertionContext(
        workspace=Path(workspace),
        state=_State(),          # type: ignore[arg-type]
        task=task,
        final_text=final_text,
    )
    try:
        results = bench_assertions.run_assertions(task.get("assertions") or [], ctx)
    except Exception as exc:  # noqa: BLE001 - a harness error must be visible, not a silent fail
        return [{"type": "harness", "passed": False, "evidence": f"grader raised {exc!r}"}]
    out = []
    for result in results:
        out.append({
            "type": getattr(result, "assertion_type", ""),
            "parameter": getattr(result, "parameter", ""),
            "passed": bool(getattr(result, "passed", False)),
            "evidence": str(getattr(result, "evidence", ""))[:200],
            "critical": bool(getattr(result, "critical", True)),
        })
    return out


def run_task(
    task: dict,
    *,
    workspace: Path,
    agent_name: str = "bench",
    steps: int = 6,
    interval_seconds: int = 900,
    timeout: float = 1800.0,
) -> LocusTaskResult:
    """Give one task to a fresh Locus, run it, and grade whatever landed on disk.

    A FRESH locus per task, and that is the whole design of the suite: a benchmark that reuses an
    agent measures the agent's accumulated history as much as its ability, and a failure becomes
    unreproducible the moment it is investigated. Each task gets a clean identity, clean workspace and
    clean objectives.
    """
    from .. import loci as loci_module
    from .. import usage as usage_module

    workspace = Path(workspace)
    result = LocusTaskResult(task_id=str(task.get("id", "?")), passed=False)

    if workspace.exists():
        shutil.rmtree(workspace, ignore_errors=True)
    workspace.mkdir(parents=True, exist_ok=True)
    seed_workspace(task, workspace)

    registry = loci_module.get_registry()
    locus = registry.create(
        agent_name,
        role=str(task.get("role") or "generalist"),
        instructions=str(task.get("instructions") or ""),
        goals=[str(task["prompt"])],
    )
    locus.autonomy = "self_directed"
    locus.enabled = True
    locus.proactive = True
    locus.max_steps_per_run = steps
    locus.last_run_at = 0.0
    locus.interval_seconds = interval_seconds
    # The workspace IS the task fixture directory. Pointing the Locus elsewhere would grade an empty
    # folder and report a failure the agent never had a chance to avoid.
    locus.workspace = str(workspace)
    registry.persist(locus)

    before = usage_module.ledger_summary()
    started = time.time()
    try:
        from ..cli import _run_one_dot
        from ..config import resolve_config
        from ..theme import get_console

        console = get_console(quiet=True)
        config = resolve_config(console, model="")
        if config is None:
            result.error = "no provider configured"
            return result

        from ..cli import _build_agent_for_command
        from argparse import Namespace

        args = Namespace(
            model="", yolo=True, no_sandbox=False, thinking=False, effort=None,
            mode="auto_edit", no_compact=False, profile=None, agent_mode="single",
            json=True, locus=locus.name, sandbox=True,
        )
        agent = _build_agent_for_command(config, console, args)
        if agent is None or agent.tool_context is None:
            result.error = "could not build an agent for the locus run"
            return result

        ran, failed = _run_one_dot(locus, agent, console, args)
        result.steps_used = int(getattr(locus, "steps_this_run", 0) or 0)
        result.failures = 1 if failed else 0
        if not ran:
            result.error = "the locus did not run (not due, or refused by preflight)"
    except Exception as exc:  # noqa: BLE001
        result.error = f"{type(exc).__name__}: {exc}"
    finally:
        result.seconds = time.time() - started
        after = usage_module.ledger_summary()
        result.cost_usd = float(after.get("cost_usd", 0.0)) - float(before.get("cost_usd", 0.0))

    result.assertions = grade(task, workspace, final_text="")
    result.passed = bool(result.assertions) and all(
        a["passed"] for a in result.assertions if a.get("critical", True)
    )

    # Remove the synthetic locus, so a benchmark run does not pollute the owner's Loci list with
    # agents named after the suite.
    try:
        registry.delete(locus.id)
    except Exception:  # noqa: BLE001
        pass
    return result


def run_suite(*, root: Path, tasks: list[dict] | None = None, steps: int = 6,
              only: str = "") -> list[LocusTaskResult]:
    """Run every task, each in its own workspace, and return the results in order."""
    selected = tasks if tasks is not None else discover_tasks()
    if only:
        selected = [t for t in selected if only in str(t.get("id", ""))]
    results = []
    for task in selected:
        workspace = Path(root) / str(task.get("id", "task"))
        results.append(run_task(task, workspace=workspace, steps=steps))
    return results


def summarise(results: list[LocusTaskResult]) -> dict:
    """Aggregate: pass rate, and the cost of ONE task done well. Never a bare score."""
    total = len(results)
    passed = sum(1 for r in results if r.passed)
    return {
        "tasks": total,
        "passed": passed,
        "pass_rate": (passed / total) if total else 0.0,
        "failures": sum(r.failures for r in results),
        "errors": sum(1 for r in results if r.error),
        "total_cost_usd": sum(r.cost_usd for r in results),
        "total_seconds": round(sum(r.seconds for r in results), 1),
        "per_task": [r.to_dict() for r in results],
    }


def format_report(summary: dict) -> str:
    lines = [
        f"Locus benchmark: {summary['passed']}/{summary['tasks']} passed "
        f"({summary['pass_rate']:.0%})",
        f"  cost  ${summary['total_cost_usd']:.4f}   time {summary['total_seconds']:.0f}s",
    ]
    for row in summary["per_task"]:
        mark = "PASS" if row["passed"] else "FAIL"
        detail = ""
        if not row["passed"]:
            failed = [a for a in row["assertions"] if not a["passed"]]
            detail = "  <- " + "; ".join(a["evidence"][:70] for a in failed[:2])
        if row["error"]:
            detail = f"  <- {row['error'][:90]}"
        lines.append(
            f"  [{mark}] {row['task']:34} {row['seconds']:5.0f}s  ${row['cost_usd']:.4f}{detail}"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="myriaxon.bench.locus_bench",
        description="Measure a self-directed Locus on tasks with deterministic success criteria.",
    )
    parser.add_argument("--list", action="store_true", help="List tasks and exit.")
    parser.add_argument("--only", default="", help="Run only tasks whose id contains this text.")
    parser.add_argument("--steps", type=int, default=6, help="Step budget per task (default 6).")
    parser.add_argument("--root", default="", help="Where workspaces are created (default: a temp dir).")
    parser.add_argument("--json", action="store_true", help="Machine-readable output.")
    args = parser.parse_args(argv)

    tasks = discover_tasks()
    if not tasks:
        print(f"no tasks found under {TASK_DIR}")
        return 1

    if args.list:
        for task in tasks:
            checks = len(task.get("assertions") or [])
            print(f"  {task['id']:34} role={task.get('role', 'generalist'):12} {checks} assertion(s)")
        return 0

    if args.only:
        tasks = [t for t in tasks if args.only in str(t.get("id", ""))]
        if not tasks:
            print(f"no task matches {args.only!r}")
            return 1

    root = Path(args.root) if args.root else Path(tempfile.mkdtemp(prefix="locus-bench-"))
    print(f"workspaces under {root}\n")

    results = run_suite(root=root, tasks=tasks, steps=args.steps)
    summary = summarise(results)

    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print(format_report(summary))
    # Exit non-zero when a task fails, so this is usable as a gate rather than only as a report.
    return 0 if summary["passed"] == summary["tasks"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))


