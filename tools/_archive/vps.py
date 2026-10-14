#!/usr/bin/env python3
"""Run code on the VPS or inside a container without fighting shell quoting.

Three quoting layers stand between this machine and a command on the VPS: PowerShell's, the local `ssh`
argument, and the remote shell's. Every failure in this project that looked like "the tool did nothing" traced
back to one of them -- a PowerShell here-string adding a trailing `\\r` (`Cannot find module
'/app/content/scripts/build-seed.ts%0D'`), an escaped `"` collapsing inside `bash -c`, or a `|` being eaten
before it reached `psql`.

So this never puts code in a command line. Scripts are piped to the remote interpreter's stdin, and SQL is
piped to `psql`'s. A caller passes Python or SQL as a string and gets stdout back.

    python tools/vps.py sql "SELECT 1"
    python tools/vps.py node "console.log(process.version)"
    python tools/vps.py py "print(open('/app/x.txt').read()[:100])"
    python tools/vps.py shell "ls /srv/certivo | wc -l"
"""

from __future__ import annotations

import argparse
import subprocess
import sys

HOST = "admin-certivo@139.190.97.11"
APP = "certivo-app-1"
DB = "certivo-db-1"

# Each target is (how to reach the interpreter, whether it reads the program from stdin).
TARGETS = {
    # `psql ... -f -` reads the script from stdin; `-t -A` gives tab-separated output with no padding.
    "sql": (f"docker exec -i {DB} psql -U certivo -d certivo -t -A -F'|'", True),
    # `python3 -` reads the program from stdin. The container has Node but NOT Python, which is why an early
    # probe that used python3 inside it reported "0 files" and looked like an empty track.
    "py": (f"docker exec -i {APP} node -e 'process.exit(0)' >/dev/null 2>&1; "
           f"python3 -", True),
    "node": (f"docker exec -i {APP} node -", True),
    "pyhost": ("python3 -", True),
    "shell": ("bash -s", True),
}


def run(kind: str, program: str, timeout: int = 900) -> subprocess.CompletedProcess:
    command, _ = TARGETS[kind]
    if kind == "py":
        # The app container has no python3; fall back to the host interpreter if it is absent.
        probe = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=25", HOST,
             f"docker exec {APP} which python3 >/dev/null 2>&1 && echo yes || echo no"],
            capture_output=True, text=True, encoding="utf-8", timeout=120,
        )
        if probe.stdout.strip() != "yes":
            command = "python3 -"
    return subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=25", HOST, command],
        input=program.encode("utf-8"), capture_output=True, timeout=timeout,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Run code on the VPS without quoting problems.")
    parser.add_argument("kind", choices=sorted(TARGETS))
    parser.add_argument("program", nargs="?", default="-",
                        help="the code to pipe in; omit or use '-' to read it from stdin")
    parser.add_argument("--timeout", type=int, default=900)
    args = parser.parse_args()

    # Defaulting to stdin matters: passing a multi-line PowerShell here-string as an argument lets PowerShell
    # split it on whitespace, so `stat -c "%y %s"` arrived as three arguments and argparse rejected the call.
    program = sys.stdin.read() if args.program == "-" else args.program
    result = run(args.kind, program, args.timeout)
    out = (result.stdout or b"").decode("utf-8", "replace")
    err = (result.stderr or b"").decode("utf-8", "replace")
    if out:
        print(out, end="" if out.endswith("\n") else "\n")
    if err.strip():
        print(err, file=sys.stderr, end="" if err.endswith("\n") else "\n")
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
