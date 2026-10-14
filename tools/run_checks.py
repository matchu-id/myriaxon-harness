#!/usr/bin/env python3
"""Run every Myriaxon check, each in its own throw-away sandbox, and print one verdict.

    python tools/run_checks.py                 # all verifiers + the pytest suite
    python tools/run_checks.py --list          # what would run
    python tools/run_checks.py --only plan_mode,guardrails
    python tools/run_checks.py --no-pytest --jobs 8
    python tools/run_checks.py --json report.json

What it does, and why it is built this way
------------------------------------------
Myriaxon has two kinds of checks: `tests/` (pytest) and `tools/verify_*.py` (self-contained scripts that print
"N passed, M failed" and exit non-zero on a failure). This runs both and totals them.

Every check gets its OWN home directory and its OWN temp directory, created under one throw-away root. That matters
for two reasons, and the second one is how this project once lost its working tree:

* a check that writes settings, memory or logs writes them into the sandbox, never into your real ~/.myriaxon;
* a check that cleans up after itself can only ever reach its own temp directory. Provider keys and tokens are removed
  from the environment, so no check can call a real model by accident.

The temp directory handed to a check is a SUBfolder of the sandbox, so even a script that deletes "the parent of
my temp folder" (the bug in the old tools/verify_packaging.py) deletes only its own sandbox.

Exit status: 0 when nothing failed, 1 when a check failed or timed out, 2 for a usage problem.
"""

from __future__ import annotations

import argparse
import concurrent.futures as futures
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"

# `tools/` also holds scripts for another project that shares this folder (the Certivo content checks). They need
# that project's files and a database, so they are not part of Myriaxon's own verdict.
NOT_MYRIAXON = {
    "verify_all.py", "verify_apply_localisation.py", "verify_badges.py", "verify_code_questions.py",
    "verify_live_content_is_rework.py", "verify_objective.py", "verify_py_questions.py", "verify_ts_questions.py",
}

SUMMARY = re.compile(r"(\d+)\s+passed,\s+(\d+)\s+failed")
PYTEST_SUMMARY = re.compile(r"(?:(\d+)\s+failed)?[, ]*(?:(\d+)\s+passed)?[, ]*(?:(\d+)\s+skipped)?[, ]*(?:(\d+)\s+error)?", re.I)
SECRETISH = re.compile(r"(API_?KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)", re.I)


def verifier_names() -> list[str]:
    return sorted(p.name for p in TOOLS.glob("verify_*.py") if p.name not in NOT_MYRIAXON)


def sandbox_env(base: Path, name: str) -> tuple[dict[str, str], Path]:
    """A private home and temp directory for one check, and an environment that points at them."""
    box = base / name
    home, temp = box / "home", box / "t"
    home.mkdir(parents=True, exist_ok=True)
    temp.mkdir(parents=True, exist_ok=True)
    # PYTHONPATH is removed, not extended: every verifier puts the checkout on sys.path itself, and a PYTHONPATH that
    # contains the checkout makes `pip install <wheel>` in a clean venv decide that myriaxon is "already installed"
    # (it finds the checkout's egg-info), so the packaging checks then fail to find the installed console script.
    env = {k: v for k, v in os.environ.items() if not SECRETISH.search(k) and k.upper() != "PYTHONPATH"}
    env.update({
        "HOME": str(home),
        "TMP": str(temp), "TEMP": str(temp), "TMPDIR": str(temp),
        "MYRIAXON_CONFIG_DIR": str(home / ".myriaxon"),
        "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1",
        "NO_COLOR": "1", "CI": "1",
    })
    # USERPROFILE is only redirected off Windows. On Windows the Microsoft Store Python resolves its base interpreter
    # through the user profile: with a fake one, `sys._base_executable` fell back to the virtualenv's own python, every
    # venv the packaging checks created was nested in the outer one, and pip reported "myriaxon is already
    # installed". Config and state are isolated by MYRIAXON_CONFIG_DIR, which is what the package reads.
    if os.name != "nt":
        env["USERPROFILE"] = str(home)
    return env, box


def kill_tree(proc: subprocess.Popen) -> None:
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, timeout=30)
        else:
            proc.kill()
    except Exception:  # noqa: BLE001
        pass


def run_one(args: list[str], base: Path, name: str, timeout: int) -> dict:
    env, _box = sandbox_env(base, name)
    started = time.time()
    proc = subprocess.Popen(args, cwd=str(ROOT), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace")
    timed_out = False
    try:
        output, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        kill_tree(proc)
        try:
            output, _ = proc.communicate(timeout=30)
        except Exception:  # noqa: BLE001
            output = ""
    return {"name": name, "code": proc.returncode, "timed_out": timed_out, "seconds": round(time.time() - started, 1),
            "output": output or ""}


def judge_verifier(res: dict) -> dict:
    found = SUMMARY.findall(res["output"])
    passed, failed = (int(found[-1][0]), int(found[-1][1])) if found else (None, None)
    if res["timed_out"]:
        status = "TIMEOUT"
    elif res["code"] != 0:
        status = "FAIL" if (failed or passed is not None) else "ERROR"
    elif failed:
        status = "FAIL"
    elif passed is None:
        status = "ok?"  # exited 0 but printed no summary line
    else:
        status = "ok"
    res.update(status=status, passed=passed, failed=failed)
    return res


def judge_pytest(res: dict) -> dict:
    tail = [ln for ln in res["output"].strip().splitlines() if ln.strip()][-3:]
    line = next((ln for ln in reversed(tail) if re.search(r"\b(passed|failed|error)", ln)), "")
    counts = {k: int(v) for v, k in re.findall(r"(\d+)\s+(passed|failed|skipped|errors?|xfailed|xpassed)", line)}
    failed = counts.get("failed", 0) + counts.get("error", 0) + counts.get("errors", 0)
    if res["timed_out"]:
        status = "TIMEOUT"
    elif res["code"] == 0:
        status = "ok"
    elif "No module named pytest" in res["output"]:
        status = "SKIP"
    else:
        status = "FAIL" if counts else "ERROR"
    res.update(status=status, passed=counts.get("passed"), failed=failed, summary=line.strip())
    return res


def main() -> int:
    ap = argparse.ArgumentParser(description="Run every Myriaxon check in isolation and print one verdict.")
    ap.add_argument("--list", action="store_true", help="list the checks and exit")
    ap.add_argument("--only", default="", help="comma-separated names, with or without the verify_ prefix")
    ap.add_argument("--skip", default="", help="comma-separated names to leave out")
    ap.add_argument("--no-pytest", action="store_true", help="run only the verify_*.py scripts")
    ap.add_argument("--pytest-only", action="store_true", help="run only the pytest suite")
    ap.add_argument("--jobs", type=int, default=4, help="how many verifiers to run at once (default 4)")
    ap.add_argument("--timeout", type=int, default=900, help="seconds allowed per verifier (default 900)")
    ap.add_argument("--json", default="", metavar="PATH", help="also write a machine-readable report")
    ap.add_argument("--show-failures", action="store_true", help="print the last lines of every failing check")
    opts = ap.parse_args()

    def norm(csv: str) -> set[str]:
        return {(n if n.startswith("verify_") else "verify_" + n).removesuffix(".py") + ".py" for n in csv.split(",") if n.strip()}

    names = verifier_names()
    if opts.only:
        wanted = norm(opts.only)
        missing = sorted(wanted - set(names))
        if missing:
            print("no such verifier:", ", ".join(missing), file=sys.stderr)
            return 2
        names = [n for n in names if n in wanted]
    names = [n for n in names if n not in norm(opts.skip)]
    if opts.pytest_only:
        names = []

    if opts.list:
        for n in names:
            print(n)
        if not opts.no_pytest:
            print("pytest (tests/)")
        return 0

    sandbox_root = Path(tempfile.mkdtemp(prefix="myx-checks-"))
    started = time.time()
    results: list[dict] = []
    print(f"Running {len(names)} verifier(s){'' if opts.no_pytest else ' and the pytest suite'} in sandboxes under {sandbox_root}\n")

    def report(res: dict) -> None:
        results.append(res)
        p = "-" if res["passed"] is None else res["passed"]
        f = "-" if res["failed"] is None else res["failed"]
        print(f"  {res['status']:<8} {res['name']:<38} passed {p!s:>5}  failed {f!s:>4}  {res['seconds']:>6.1f}s", flush=True)

    try:
        with futures.ThreadPoolExecutor(max_workers=max(1, opts.jobs)) as pool:
            jobs = {pool.submit(run_one, [sys.executable, "-X", "utf8", str(TOOLS / n)], sandbox_root, n[:-3], opts.timeout): n
                    for n in names}
            for fut in futures.as_completed(jobs):
                report(judge_verifier(fut.result()))
        if not opts.no_pytest:
            print("\n  running pytest ...", flush=True)
            res = judge_pytest(run_one([sys.executable, "-X", "utf8", "-m", "pytest", "tests", "-q", "-p", "no:cacheprovider"],
                                       sandbox_root, "pytest", max(opts.timeout, 1800)))
            res["is_pytest"] = True
            results.append(res)
            print(f"  {res['status']:<8} pytest  {res.get('summary', '')}  ({res['seconds']}s)")
    finally:
        shutil.rmtree(sandbox_root, ignore_errors=True)  # the sandbox root itself, nothing above it

    bad = [r for r in results if r["status"] in ("FAIL", "TIMEOUT", "ERROR")]
    unsure = [r for r in results if r["status"] in ("ok?", "SKIP")]
    checks_passed = sum(r["passed"] or 0 for r in results if not r.get("is_pytest"))
    checks_failed = sum(r["failed"] or 0 for r in results if not r.get("is_pytest"))
    print("\n" + "=" * 78)
    print(f"verifiers: {sum(1 for r in results if not r.get('is_pytest'))} run, "
          f"{checks_passed} checks passed, {checks_failed} failed")
    py = next((r for r in results if r.get("is_pytest")), None)
    if py:
        print(f"pytest:    {py['status']}  {py.get('summary', '')}")
    if unsure:
        print("not conclusive: " + ", ".join(f"{r['name']} ({r['status']})" for r in unsure))
    if bad:
        print("FAILED: " + ", ".join(f"{r['name']} ({r['status']})" for r in sorted(bad, key=lambda r: r["name"])))
        if opts.show_failures:
            for r in sorted(bad, key=lambda r: r["name"]):
                print(f"\n--- {r['name']} ({r['status']}) last lines ---")
                print("\n".join(r["output"].strip().splitlines()[-12:]))
    print(f"total time {time.time() - started:.0f}s")
    if opts.json:
        slim = [{k: v for k, v in r.items() if k != "output"} | {"tail": r["output"].strip().splitlines()[-6:]} for r in results]
        Path(opts.json).write_text(json.dumps(slim, indent=1, ensure_ascii=False), encoding="utf-8")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
