"""Execute the comparison suite against a harness, and score the result.

## Why a runner rather than a human with a stopwatch

The suite's whole value is that it is **reproducible**. A result typed into a document by hand is a claim; a result
this script prints from a recorded JSON file is evidence. So every run writes a result file, and the scoreboard is
generated from those files rather than from anyone's memory.

## How a harness is invoked

Each harness is described by the argv template needed to run one prompt non-interactively in the current directory.
Myriaxon's is known and verified: `cli.main(['-p', PROMPT, '-y'])`. The others are **not** wired yet -- see
`adapters()` for exactly what each needs, which is the honest state rather than a guess dressed up as support.

A harness whose adapter is missing is reported as **NOT RUN**, never as a zero. Scoring an unrun harness as zero
would produce a ranking that looks like a measurement and is not.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

ROOT = Path(r"C:\Users\arian\OneDrive\ドキュメント\Myriaxon")
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import compare_suite as suite  # noqa: E402

RESULTS_DIR = ROOT / "docs" / "comparison-runs"
VENV_PY = ROOT / ".venv" / "Scripts" / "python.exe"

# Per-task wall-clock ceiling. A harness that needs longer than this fails the task on time rather than being waited
# on indefinitely -- and the timeout is recorded as its own outcome so it is not confused with a wrong answer.
TASK_TIMEOUT_SECONDS = 420


@dataclass
class TaskOutcome:
    task: str
    passed: bool
    seconds: float
    evidence: str
    timed_out: bool = False


@dataclass
class RunResult:
    harness: str
    ran: bool
    reason: str = ""
    outcomes: list[TaskOutcome] = field(default_factory=list)
    started_at: float = 0.0
    finished_at: float = 0.0
    version: str = ""

    @property
    def passed(self) -> int:
        return sum(1 for o in self.outcomes if o.passed)

    @property
    def total(self) -> int:
        return len(self.outcomes)

    def render(self) -> str:
        if not self.ran:
            return f"{self.harness}: NOT RUN -- {self.reason}"
        head = f"{self.harness}: {self.passed}/{self.total} in {self.finished_at - self.started_at:.0f}s"
        lines = [head]
        for o in self.outcomes:
            mark = "PASS" if o.passed else ("TIMEOUT" if o.timed_out else "FAIL")
            lines.append(f"  [{mark:<7}] {o.task:<20} {o.seconds:>5.1f}s  {o.evidence[:88]}")
        return "\n".join(lines)


def myriaxon_command(prompt: str) -> list[str]:
    """Myriaxon, non-interactively, with the prompt as an argument.

    `-y` skips confirmation. Without it every file write blocks on a prompt that nothing will answer, and the
    harness would fail every task for a reason that has nothing to do with its capability.
    """
    return [
        str(VENV_PY), "-c",
        "import sys; sys.path.insert(0, r'%s'); from myriaxon.cli import main; "
        "sys.exit(main(sys.argv[1:]))" % ROOT,
        "-p", prompt, "-y",
    ]


def adapters() -> dict:
    """Every harness, and the exact command needed to run one prompt.

    The five rivals are `None` **on purpose and with the reason recorded**, because the alternative -- pretending a
    command exists -- produces a scoreboard built on argv templates nobody has executed.

    ## The install blocker, and the two flags that get past it

    Recorded here because finding it cost most of a session, and it is not documented anywhere obvious.

    Myriaxon's author could not install any rival for several rounds: `npm`/`bun`/`pnpm` all failed with
    **`WinError 1314 -- a required privilege is not held by the client`** on every `node_modules` link. This account
    is **not elevated** and **Developer Mode cannot be enabled from here** (`HKLM\\...\\AppModelUnlock` needs admin),
    so symbolink creation is genuinely unavailable. Measured on this host: `SymbolicLink` FAILS, `Junction` succeeds,
    `HardLink` succeeds.

    Two flags change the picture, and the first is the real unlock:

      * **`bun install --backend=copyfile`** stops bun linking packages out of a global store, so the thousands of
        ordinary dependency links never happen. Proven: **pi-harness installed 889 packages with no admin**.
      * **`--ignore-scripts`** gets past a separate, already-known failure -- `spawn EPERM` (errno -4048) in npm
        postinstall on this host.

    **What still fails, and why**, so the next attempt does not re-derive it: a *workspace* monorepo needs
    workspace-to-workspace links, and bun emits those as symlinks regardless of `--backend`. Opencode has 32
    packages and fails with `ENOENT ... failed to symlink dependencies for package: @opencode-ai/client@workspace:...`
    even after `--backend=copyfile --ignore-scripts` on a clean tree. `--linker=hoisted` was the next thing to try.

    ## What each rival needs beyond that, established by inspecting its tree

      * Opencode / DeepSeek / OpenClaw / PiHarness -- all four ship **no built artifacts** (no `dist/`, no `out/`)
        and no `node_modules`, so each needs a dependency install and a build before it can run a single prompt.
      * PiHarness is an **Electron desktop app** (`main: ./out/main/index.js`): it has no headless entry point at
        all, so it cannot be scored by this runner without writing a driver for it. Its install now SUCCEEDS; its
        build is the remaining step.
      * OpenClaw exposes `bin.openclaw`, so once built it is the most likely of the four to be drivable.
      * DeepSeek and ClaudeCode are monorepos of packages rather than a single CLI, so a driver would have to pick
        the right package and entry point.
    """
    return {
        "Myriaxon": myriaxon_command,
        "Opencode": None,
        "OpenClaw": None,
        "DeepSeek": None,
        "PiHarness": None,
        "ClaudeCode": None,
    }


def run_one(harness: str, command_for, task: suite.Task, keep: bool = False) -> TaskOutcome:
    """Seed a workspace, run the harness on the task, and score with the task's own check."""
    root = suite.build_workspace(task)
    started = time.time()
    timed_out = False
    output = ""
    try:
        done = subprocess.run(
            command_for(task.prompt),
            cwd=str(root),
            capture_output=True,
            text=True,
            errors="replace",
            timeout=TASK_TIMEOUT_SECONDS,
        )
        output = ((done.stdout or "") + (done.stderr or ""))[-4000:]
    except subprocess.TimeoutExpired:
        timed_out = True
        output = f"the harness did not finish within {TASK_TIMEOUT_SECONDS}s"
    except OSError as exc:
        output = f"could not be started: {exc}"

    seconds = time.time() - started
    if timed_out:
        passed, evidence = False, output
    else:
        passed, evidence = suite.score(task, root)
        if not passed and not evidence:
            evidence = output[-200:] or "no output and no reason"
    if not keep:
        shutil.rmtree(root, ignore_errors=True)
    return TaskOutcome(task=task.name, passed=passed, seconds=seconds,
                       evidence=evidence.replace("\n", " ")[:200], timed_out=timed_out)


def run_harness(harness: str, tasks: list[suite.Task], keep: bool = False) -> RunResult:
    command_for = adapters().get(harness)
    result = RunResult(harness=harness, ran=command_for is not None)
    if command_for is None:
        result.reason = (
            "no adapter: the harness ships no built artifacts and no node_modules, so it needs an install and a "
            "build before it can run a prompt. Reported as NOT RUN rather than scored zero."
        )
        return result
    result.started_at = time.time()
    for task in tasks:
        outcome = run_one(harness, command_for, task, keep=keep)
        result.outcomes.append(outcome)
        print(f"    {task.name:<20} {'PASS' if outcome.passed else ('TIMEOUT' if outcome.timed_out else 'FAIL')}"
              f"  {outcome.seconds:>5.1f}s  {outcome.evidence[:70]}")
    result.finished_at = time.time()
    return result


def save(result: RunResult) -> Path:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    path = RESULTS_DIR / f"{result.harness.lower()}.json"
    payload = asdict(result)
    payload["passed"] = result.passed
    payload["total"] = result.total
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8", newline="\n")
    return path


def load_all() -> list[RunResult]:
    out = []
    if not RESULTS_DIR.is_dir():
        return out
    for path in sorted(RESULTS_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        result = RunResult(
            harness=data.get("harness", path.stem),
            ran=bool(data.get("ran")),
            reason=data.get("reason", ""),
            started_at=data.get("started_at", 0.0),
            finished_at=data.get("finished_at", 0.0),
        )
        result.outcomes = [TaskOutcome(**o) for o in data.get("outcomes", [])]
        out.append(result)
    return out


def scoreboard() -> str:
    """Build the table from the SAVED runs, so it cannot drift from what was executed."""
    results = load_all()
    if not results:
        return "No runs recorded yet. Run a harness first."
    lines = [
        f"{'harness':<12}{'passed':>8}{'total':>7}{'seconds':>9}   status",
        "-" * 66,
    ]
    for r in sorted(results, key=lambda x: (-x.passed, x.harness)):
        if not r.ran:
            lines.append(f"{r.harness:<12}{'--':>8}{'--':>7}{'--':>9}   NOT RUN")
        else:
            lines.append(f"{r.harness:<12}{r.passed:>8}{r.total:>7}"
                         f"{r.finished_at - r.started_at:>9.0f}   ran")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the harness comparison suite.")
    parser.add_argument("--harness", default="Myriaxon", help="Which harness to run.")
    parser.add_argument("--task", default="", help="Run only this task.")
    parser.add_argument("--keep", action="store_true", help="Keep the workspaces for inspection.")
    parser.add_argument("--scoreboard", action="store_true", help="Print the table from saved runs.")
    parser.add_argument("--self-test", action="store_true", help="Prove the checks discriminate first.")
    args = parser.parse_args()

    if args.self_test:
        return suite.self_test()
    if args.scoreboard:
        print(scoreboard())
        return 0

    tasks = [t for t in suite.TASKS if not args.task or t.name == args.task]
    if not tasks:
        print(f"  No task named {args.task!r}. Available: {', '.join(t.name for t in suite.TASKS)}")
        return 2

    print(f"  Running {len(tasks)} task(s) against {args.harness}")
    result = run_harness(args.harness, tasks, keep=args.keep)
    print()
    print(result.render())
    if result.ran:
        path = save(result)
        print(f"\n  saved -> {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
