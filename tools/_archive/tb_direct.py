"""Run Terminal-Bench tasks directly, without Harbor.

Why this exists
---------------
Harbor is the official runner, and its one irreplaceable feature is comparability: a score produced by
it can sit beside published Terminal-Bench numbers. Everything else it does -- starting the container,
running the agent, executing the verifier, collecting a reward -- is a few dozen lines, and its own
abstraction cost far more time than it saved. Measured: three Harbor runs scored 0.0 while the same
task, run by hand against the same image, produced a correct regex. The failures were all in Harbor's
plumbing (an invalid wheel filename, a config path that does not cross into WSL, a TCP bridge that the
Hyper-V firewall blocks), none of which said anything about Myriaxon.

So this runner uses the *official artifacts* without the official orchestrator:

  * the task definitions, checked out from `laude-institute/terminal-bench-2` at a pinned commit;
  * the task's own prebuilt image from `[environment] docker_image`;
  * the task's own `tests/test.sh`, which is what decides pass or fail;
  * the task's own `tests/test_outputs.py`, whose reward lands in `/logs/verifier/reward.txt`.

What that does and does not buy
-------------------------------
It answers "how many of these tasks does Myriaxon solve" honestly. It does **not** produce a number
comparable to a published leaderboard, because the sandbox setup, timeouts and resource limits are
Harbor's job and reimplementing them here would be a different experiment. Results from this runner
must be reported as "N of M Terminal-Bench tasks solved, using their images and their verifiers".

Usage:
    python tools/tb_direct.py --list
    python tools/tb_direct.py regex-log
    python tools/tb_direct.py regex-log mailman --timeout 900
    python tools/tb_direct.py --all --jobs 2
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TASKS_DIR = REPO / ".uv" / "tb20" / "terminal-bench"
WHEEL_DIR = REPO / ".uv" / "wheel"
CONFIG_DIR = "/tmp/myriaxon-config"
VENV = "/opt/myriaxon-venv"
# The verifier's own convention: `tests/test.sh` writes the score here.
REWARD_PATH = "/logs/verifier/reward.txt"


@dataclass
class Task:
    name: str
    path: Path
    image: str = ""
    agent_timeout: float = 900.0
    verifier_timeout: float = 900.0
    test_script: str = ""
    instruction: str = ""
    cpus: str = ""
    memory: str = ""


@dataclass
class Outcome:
    name: str
    passed: bool = False
    reward: str = ""
    detail: str = ""
    seconds: float = 0.0
    agent_tail: str = ""
    notes: list[str] = field(default_factory=list)


def load_task(name: str) -> Task | None:
    """Read a task's own metadata. Never guesses: a missing image is reported, not defaulted."""
    path = TASKS_DIR / name
    if not path.is_dir():
        return None
    task = Task(name=name, path=path)

    toml_path = path / "task.toml"
    if toml_path.is_file():
        text = toml_path.read_text(encoding="utf-8", errors="replace")
        match = re.search(r'^\s*docker_image\s*=\s*"([^"]+)"', text, re.MULTILINE)
        if match:
            task.image = match.group(1)
        # The two sections carry the same key name, so they are read in order rather than by one regex.
        for section, attr in (("agent", "agent_timeout"), ("verifier", "verifier_timeout")):
            block = re.search(rf"\[{section}\](.*?)(?=\n\[|\Z)", text, re.DOTALL)
            if block:
                seconds = re.search(r"timeout_sec\s*=\s*([\d.]+)", block.group(1))
                if seconds:
                    setattr(task, attr, float(seconds.group(1)))
        for key, attr in (("cpus", "cpus"), ("memory", "memory")):
            found = re.search(rf'^\s*{key}\s*=\s*"?([^"\n]+)"?', text, re.MULTILINE)
            if found:
                setattr(task, attr, found.group(1).strip())

    instruction = path / "instruction.md"
    if instruction.is_file():
        task.instruction = instruction.read_text(encoding="utf-8", errors="replace").strip()

    test_script = path / "tests" / "test.sh"
    if test_script.is_file():
        task.test_script = test_script.read_text(encoding="utf-8", errors="replace")
    return task


def docker(*args: str, timeout: float = 600.0, input_text: str | None = None) -> tuple[int, str]:
    """Run a docker command. Returns (code, combined output)."""
    try:
        result = subprocess.run(
            ["docker", *args], capture_output=True, text=True, timeout=timeout, input=input_text
        )
        return result.returncode, (result.stdout or "") + (result.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, f"docker {' '.join(args[:2])} timed out after {timeout:.0f}s"
    except FileNotFoundError:
        return 127, "docker is not on PATH"


def build_wheel() -> Path | None:
    WHEEL_DIR.mkdir(parents=True, exist_ok=True)
    wheels = sorted(WHEEL_DIR.glob("myriaxon-*.whl"))
    if wheels:
        return wheels[0]
    print("  building the wheel (first run only) ...")
    result = subprocess.run(
        [sys.executable, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation",
         "--no-cache-dir", "-w", str(WHEEL_DIR), str(REPO)],
        capture_output=True, text=True, timeout=1200, cwd=str(REPO),
    )
    if result.returncode != 0:
        print((result.stderr or result.stdout or "")[-1500:])
        return None
    wheels = sorted(WHEEL_DIR.glob("myriaxon-*.whl"))
    return wheels[0] if wheels else None


def model_config() -> dict | None:
    """The provider settings the container should use, from Myriaxon's own config."""
    os.environ.setdefault("MYRIAXON_CONFIG_DIR", str(REPO / ".myriaxon"))
    sys.path.insert(0, str(REPO))
    try:
        from myriaxon.config import load_config
    except Exception as exc:  # noqa: BLE001
        print(f"  cannot import Myriaxon's config: {exc}")
        return None
    config = load_config()
    if config is None or not config.api_key:
        return None
    return {
        "provider": "openai_compat",
        "api_key": config.api_key,
        "base_url": config.base_url,
        "model": config.model,
    }


def run_task(task: Task, wheel: Path, config: dict, keep: bool, timeout_scale: float) -> Outcome:
    outcome = Outcome(name=task.name)
    started = time.perf_counter()
    container = f"tbd-{task.name[:40]}-{os.getpid()}"

    if not task.image:
        outcome.detail = "no docker_image in task.toml"
        return outcome
    if not task.instruction:
        outcome.detail = "no instruction.md"
        return outcome

    docker("rm", "-f", container, timeout=60)

    # Start the container exactly as the task describes. `-w /app` is the working directory the task's
    # own verifier expects its artifacts in -- the first Harbor attempt scored 0.0 because the agent
    # ran in `/` instead.
    args = ["run", "-d", "--name", container, "-w", "/app"]
    if task.cpus:
        args += ["--cpus", task.cpus]
    if task.memory:
        args += ["--memory", task.memory]
    args += [task.image, "sleep", str(int(max(task.agent_timeout, 600) * timeout_scale) + 1800)]
    code, out = docker(*args, timeout=600)
    if code != 0:
        outcome.detail = f"could not start container: {out.strip()[:200]}"
        return outcome
    outcome.notes.append("container started")

    try:
        # 1. python and the wheel. The wheel keeps its real filename: pip validates the name, and
        #    copying it to `m.whl` failed with "not a valid wheel filename" while the surrounding
        #    steps still reported success.
        docker("exec", container, "sh", "-c",
               "apt-get update -qq && apt-get install -y -qq python3 python3-venv python3-pip git",
               timeout=900, input_text=None)
        code, out = docker("exec", container, "sh", "-c", f"python3 -m venv {VENV}", timeout=300)
        if code != 0:
            outcome.detail = f"venv failed: {out.strip()[:200]}"
            return outcome

        code, out = docker("cp", str(wheel), f"{container}:/tmp/{wheel.name}", timeout=300)
        if code != 0:
            outcome.detail = f"wheel upload failed: {out.strip()[:200]}"
            return outcome
        code, out = docker("exec", container, f"{VENV}/bin/pip", "install", "--no-cache-dir",
                           f"/tmp/{wheel.name}", timeout=1200)
        if code != 0:
            outcome.detail = f"install failed: {out.strip()[-300:]}"
            return outcome

        # A silent install failure is the trap that cost two Harbor runs, so the module is imported
        # before anything depends on it.
        code, out = docker("exec", container, f"{VENV}/bin/python", "-m", "myriaxon", "--version",
                           timeout=120)
        if code != 0:
            outcome.detail = f"myriaxon does not run after install: {out.strip()[:200]}"
            return outcome
        outcome.notes.append(f"installed ({out.strip()[:40]})")

        # 2. the model settings, uploaded as a file rather than echoed by a command -- Harbor logs
        #    every command it runs, and a live key written that way landed in trial.log.
        staging = Path(tempfile.mkdtemp(prefix="tb-config-"))
        try:
            local = staging / "config.json"
            local.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8", newline="\n")
            docker("exec", container, "sh", "-c", f"mkdir -p {CONFIG_DIR} && chmod 700 {CONFIG_DIR}",
                   timeout=120)
            docker("cp", str(local), f"{container}:{CONFIG_DIR}/config.json", timeout=120)
            docker("exec", container, "sh", "-c", f"chmod 600 {CONFIG_DIR}/config.json", timeout=120)
        finally:
            shutil.rmtree(staging, ignore_errors=True)

        # 3. the instruction, placed in the working directory the way the task images expect. The
        #    downloaded copy is used rather than a fresh checkout so a run is reproducible offline.
        staged_instruction = staging / "instruction.md"
        staged_instruction.write_text(task.instruction + "\n", encoding="utf-8", newline="\n")
        docker("cp", str(staged_instruction), f"{container}:/app/instruction.md", timeout=120)

        # 4. the turn. The instruction goes through a file, not an argument: Terminal-Bench prompts
        #    contain quotes, backticks and newlines, and one of them already needed shell-quoting in
        #    the Harbor adapter.
        docker("exec", container, "sh", "-c", "cat /app/instruction.md > /tmp/prompt.txt")
        turn_cmd = (
            f"cd /app && MYRIAXON_CONFIG_DIR={CONFIG_DIR} "
            f"{VENV}/bin/python -m myriaxon --mode auto -p \"$(cat /tmp/prompt.txt)\""
        )
        turn_started = time.perf_counter()
        code, out = docker("exec", container, "sh", "-c", turn_cmd,
                           timeout=task.agent_timeout * timeout_scale)
        outcome.seconds = time.perf_counter() - turn_started
        outcome.agent_tail = out.strip()[-1200:]
        # A non-zero exit is recorded, not treated as failure: the verifier decides, and an agent that
        # errored can still have left the right artifact on disk.
        if code != 0:
            outcome.notes.append(f"agent exit {code}")

        # 5. the task's own verifier. Its script is used verbatim -- rewriting it would measure a
        #    different benchmark.
        docker("exec", container, "sh", "-c", "mkdir -p /logs/verifier", timeout=60)
        verifier_staging = staging / "test.sh"
        verifier_staging.write_text(task.test_script or "#!/bin/sh\nexit 1\n",
                                    encoding="utf-8", newline="\n")
        docker("cp", str(verifier_staging), f"{container}:/tmp/verify.sh", timeout=120)
        docker("exec", container, "chmod", "+x", "/tmp/verify.sh", timeout=60)
        code, out = docker("exec", container, "sh", "-c", "cd /app && sh /tmp/verify.sh",
                           timeout=task.verifier_timeout * timeout_scale)
        outcome.notes.append(f"verifier exit {code}")

        code, reward = docker("exec", container, "sh", "-c",
                              f"cat {REWARD_PATH} 2>/dev/null || echo MISSING", timeout=60)
        outcome.reward = reward.strip().splitlines()[-1].strip() if reward.strip() else "MISSING"
        outcome.passed = outcome.reward not in ("", "0", "0.0", "MISSING")
        if not outcome.passed and outcome.reward == "MISSING":
            outcome.detail = "the verifier wrote no reward file"
            outcome.detail += f"; verifier output: {out.strip()[-300:]}"
    finally:
        if keep:
            outcome.notes.append(f"container kept: {container}")
        else:
            docker("rm", "-f", container, timeout=120)

    outcome.seconds = time.perf_counter() - started if not outcome.seconds else outcome.seconds
    return outcome


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("tasks", nargs="*", help="task names; omit with --all")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--keep", action="store_true", help="leave containers running for inspection")
    parser.add_argument("--timeout-scale", type=float, default=1.0)
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    if not TASKS_DIR.is_dir():
        print(f"no task set at {TASKS_DIR}")
        return 2

    available = sorted(p.name for p in TASKS_DIR.iterdir() if p.is_dir())
    if args.list:
        for name in available:
            task = load_task(name)
            mark = "ok " if task and task.image and task.test_script else "?? "
            print(f"  {mark}{name:44} {task.image if task else ''}")
        print(f"\n  {len(available)} task(s)")
        return 0

    names = available if args.all else args.tasks
    if not names:
        print("nothing selected: name tasks or pass --all")
        return 2
    if args.limit:
        names = names[: args.limit]

    wheel = build_wheel()
    if wheel is None:
        print("could not build the Myriaxon wheel")
        return 2
    print(f"  wheel   : {wheel.name}")

    config = model_config()
    if config is None:
        print("no usable model config; run `myriaxon auth` first")
        return 2
    print(f"  model   : {config['model']} via {config['base_url']}")
    print(f"  tasks   : {len(names)}")
    print()

    results: list[Outcome] = []
    for index, name in enumerate(names, 1):
        task = load_task(name)
        if task is None:
            print(f"  [{index}/{len(names)}] {name}: NOT FOUND")
            continue
        print(f"  [{index}/{len(names)}] {name} ... ", end="", flush=True)
        outcome = run_task(task, wheel, config, args.keep, args.timeout_scale)
        results.append(outcome)
        mark = "PASS" if outcome.passed else "fail"
        print(f"{mark}  reward={outcome.reward}  {outcome.seconds:.0f}s"
              + (f"  {outcome.detail[:90]}" if outcome.detail else ""))

    print()
    passed = sum(1 for r in results if r.passed)
    print(f"=== {passed}/{len(results)} solved ===")
    if results and passed < len(results):
        print("failures:")
        for r in results:
            if not r.passed:
                print(f"  {r.name:44} reward={r.reward} {r.detail[:70]}")
    if results and any(r.notes for r in results):
        print("\nnotes:")
        for r in results:
            if r.notes:
                print(f"  {r.name:44} {', '.join(r.notes)}")

    report = REPO / "bench-out" / "tb-direct.json"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(
        json.dumps(
            [
                {
                    "name": r.name, "passed": r.passed, "reward": r.reward,
                    "seconds": round(r.seconds, 1), "detail": r.detail,
                    "notes": r.notes, "agent_tail": r.agent_tail,
                }
                for r in results
            ],
            indent=2,
        ) + "\n",
        encoding="utf-8", newline="\n",
    )
    print(f"\nwritten: {report.relative_to(REPO)}")
    return 0 if passed == len(results) and results else 1


if __name__ == "__main__":
    sys.exit(main())
