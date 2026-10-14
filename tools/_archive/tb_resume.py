"""Pick up the Terminal-Bench work after a reboot, and report exactly where things stand.

Written to be run first after the machine comes back. Everything it checks is a fact about the
environment rather than an assumption carried over from before, because the point of a reboot is that
the environment changed.

    .venv-harbor\\Scripts\\python.exe tools/tb_resume.py
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

DOCKER_CANDIDATES = (
    r"C:\Users\arian\AppData\Local\Programs\DockerDesktop\resources\bin\docker.exe",
    r"C:\Program Files\Docker\Docker\resources\bin\docker.exe",
)


def find_docker() -> str:
    """The docker binary, on PATH or at the known Docker Desktop location.

    Measured: `Get-Command docker` returns nothing in a process that did not inherit the user's PATH,
    and the binary lives under `%LOCALAPPDATA%\\Programs\\DockerDesktop`, not under `Program Files`.
    Both were missed once, and "docker is not installed" was reported wrongly as a result.
    """
    on_path = shutil.which("docker")
    if on_path:
        return on_path
    for candidate in DOCKER_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    return ""


def run(argv: list[str], timeout: int = 120) -> tuple[int, str]:
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return result.returncode, (result.stdout or "") + (result.stderr or "")
    except Exception as exc:  # noqa: BLE001
        return 1, f"{type(exc).__name__}: {exc}"


def main() -> int:
    print("=== where things stand ===\n")

    problems: list[str] = []

    # --- wsl ------------------------------------------------------------------------------
    code, out = run(["wsl.exe", "--status"])
    wsl_ok = code == 0 and "not installed" not in out.lower()
    print(f"  wsl2            : {'ready' if wsl_ok else 'NOT READY'}")
    if not wsl_ok:
        problems.append("wsl2 is not set up -- run `wsl --install` in an admin PowerShell, then reboot")

    code, out = run(["wsl.exe", "-l", "-v"])
    distros = [l for l in out.replace("\x00", "").splitlines() if l.strip() and "NAME" not in l.upper()]
    print(f"  distros         : {', '.join(d.strip().split()[-1] for d in distros) or 'none'}")

    # --- docker ---------------------------------------------------------------------------
    docker = find_docker()
    print(f"  docker binary   : {docker or 'NOT FOUND'}")
    if not docker:
        problems.append("no docker binary found on PATH or at the Docker Desktop location")

    if docker:
        code, out = run([docker, "version", "--format", "{{.Server.Version}}"])
        engine = out.strip().splitlines()[-1] if out.strip() else ""
        engine_ok = code == 0 and engine and "unable to start" not in out.lower()
        print(f"  docker engine   : {'ready -- ' + engine if engine_ok else 'NOT RUNNING'}")
        if not engine_ok:
            problems.append(
                "the docker engine is not running -- launch Docker Desktop and wait for "
                "'Engine running' (one of: '" + (engine or out.strip())[:80] + "')"
            )

        if engine_ok:
            code, out = run([docker, "run", "--rm", "hello-world"], timeout=300)
            linux = "Hello from Docker" in out
            print(f"  linux containers: {'working' if linux else 'NOT WORKING'}")
            if not linux:
                problems.append(f"linux containers failed: {out.strip()[:200]}")

    # --- downloaded task set ---------------------------------------------------------------
    tasks = REPO / ".uv" / "tb20" / "terminal-bench"
    count = len([d for d in tasks.iterdir() if d.is_dir()]) if tasks.is_dir() else 0
    print(f"  terminal-bench  : {count} task(s)")

    # --- the pieces already built ----------------------------------------------------------
    wheel = sorted((REPO / ".uv" / "wheel").glob("myriaxon-*.whl")) if (REPO / ".uv" / "wheel").is_dir() else []
    print(f"  myriaxon wheel  : {wheel[0].name if wheel else 'not built'}")
    adapter = REPO / "tools" / "harbor_myriaxon.py"
    print(f"  harbor adapter  : {'present' if adapter.is_file() else 'MISSING'}")
    keys = (REPO / ".env.daytona").is_file()
    print(f"  daytona key     : {'stored' if keys else 'absent'}")

    print()
    if problems:
        print("=== blocked on ===")
        for index, problem in enumerate(problems, 1):
            print(f"  {index}. {problem}")
        print()
        print("Once docker reports ready, run:")
        print("  .venv-harbor\\Scripts\\python.exe tools/tb_run.py --smoke -e docker")
        return 1

    print("=== everything ready ===")
    print("  Smoke test (one small task):")
    print("    .venv-harbor\\Scripts\\python.exe tools/tb_run.py --smoke -e docker")
    print("  A few real tasks:")
    print("    .venv-harbor\\Scripts\\python.exe tools/tb_run.py --tasks mailman regex-log -e docker")
    print("  Whole suite:")
    print("    .venv-harbor\\Scripts\\python.exe tools/tb_run.py --all -e docker -n 3")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
