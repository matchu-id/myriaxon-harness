"""Find a real interpreter, given that the obvious ones are often lies.

## The defect this fixes, measured

During the executed comparison run, Myriaxon was asked to verify a Python fix. It called `run_bash` three times
hunting for an interpreter, then reported to the user:

> "No Python installed on this machine."

That is **false**, and it is false in a way that is specific and common on Windows. `python` and `python3` are on
`PATH`, but both resolve to:

    C:\\Users\\<user>\\AppData\\Local\\Microsoft\\WindowsApps\\python.exe

which is an **App Execution Alias** -- a 0-byte reparse point that opens the Microsoft Store when run and does not
execute Python at all. There is no `py` launcher on this host either. The actual interpreter is the virtualenv's,
which the agent never thought to look for because the failing alias was already "the answer" to `which python`.

## Why this belongs in the harness rather than in a prompt

It cost the agent three tool calls and produced a **wrong conclusion reported as fact**. A harness whose model tells
the user "Python is not installed" when it is installing nothing has a correctness bug, not a documentation gap. And
the fix is decidable locally: run each candidate with `--version` and keep the ones that actually answer.

## The rule

A candidate counts only if it **executes and prints a Python version**. Existence is not enough -- that is exactly
the trap the alias sets -- so this runs each one and reads the output. The check is deliberately cheap (one short
subprocess per candidate) and cached, because `run_bash` calls it repeatedly within a turn.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

# Run for a short time: an alias that opens the Store can hang, and a candidate that takes longer than this is not
# an interpreter anyone wants to use.
PROBE_TIMEOUT_SECONDS = 8.0

# The directories a Windows Python install actually lands in. `py` is absent on this host, so the versioned
# directories are checked directly rather than relying on the launcher.
WINDOWS_ROOTS = (
    r"C:\Program Files",
    r"C:\Program Files (x86)",
    r"C:\Python*",
)

_cache: dict[str, "Interpreter | None"] = {}


@dataclass(frozen=True)
class Interpreter:
    """An interpreter that was RUN and answered with a version."""

    path: str
    version: str
    source: str = ""

    def render(self) -> str:
        return f"{self.path} ({self.version})"


def _probe(path: str) -> Interpreter | None:
    """Run `path --version`. Returns an Interpreter only if it really is one.

    This is the whole point of the module: a Windows App Execution Alias **exists** and is on `PATH`, so
    `shutil.which` returns it happily, but running it launches the Microsoft Store. Only executing distinguishes
    the two, so existence checks are deliberately not used as evidence.
    """
    try:
        done = subprocess.run(
            [path, "--version"],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=PROBE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    output = ((done.stdout or "") + (done.stderr or "")).strip()
    if done.returncode != 0 or "python" not in output.lower():
        # An alias typically exits non-zero with a Store message; anything that cannot say "Python" is not one.
        return None
    return Interpreter(path=path, version=output.splitlines()[0][:60], source="probed")


def candidates() -> list[tuple[str, str]]:
    """Every plausible interpreter, as `(path, where_it_came_from)`. Not yet verified."""
    found: list[tuple[str, str]] = []

    # 1. The interpreter RUNNING this code. Almost always correct, and free.
    if sys.executable:
        found.append((sys.executable, "the process running Myriaxon"))

    # 2. A sibling virtualenv, which is what a project on this host actually uses.
    for name in (".venv", "venv", "env"):
        for relative in (("Scripts", "python.exe"), ("bin", "python")):
            candidate = Path.cwd() / name / Path(*relative)
            if candidate.is_file():
                found.append((str(candidate), f"the project's {name}"))

    # 3. Whatever `PATH` offers -- INCLUDING the aliases, which `_probe` will reject.
    for name in ("python3", "python", "py"):
        located = shutil.which(name)
        if located:
            found.append((located, f"PATH ({name})"))

    # 4. Conventional install locations, since the alias can shadow every PATH entry.
    if os.name == "nt":
        for root in WINDOWS_ROOTS:
            base = Path(root)
            if not base.is_dir():
                continue
            if "*" in root:
                for match in sorted(base.parent.glob(base.name)):
                    exe = match / "python.exe"
                    if exe.is_file():
                        found.append((str(exe), f"installed at {match}"))
            else:
                for match in sorted(base.glob("Python*")):
                    exe = match / "python.exe"
                    if exe.is_file():
                        found.append((str(exe), f"installed at {match}"))

    # De-duplicate while keeping order: the first candidate is the most likely to be right.
    seen = set()
    unique = []
    for path, source in found:
        key = os.path.normcase(os.path.abspath(path))
        if key in seen:
            continue
        seen.add(key)
        unique.append((path, source))
    return unique


def find(refresh: bool = False) -> Interpreter | None:
    """The best interpreter available, or None if there genuinely is not one.

    `None` is a real answer here -- and now it means what it says, because every candidate was executed rather than
    looked up.
    """
    if not refresh and "best" in _cache:
        return _cache["best"]
    best: Interpreter | None = None
    for path, source in candidates():
        probed = _probe(path)
        if probed is None:
            continue
        best = Interpreter(path=probed.path, version=probed.version, source=source)
        break
    _cache["best"] = best
    return best


def rejected() -> list[tuple[str, str]]:
    """Candidates that were tried and did NOT work, with the reason.

    Reported rather than hidden: "python is on PATH but is a Microsoft Store alias" is the single most useful thing
    to tell a model that is about to conclude Python is not installed.
    """
    out: list[tuple[str, str]] = []
    for path, source in candidates():
        if _probe(path) is None:
            reason = "does not run as Python"
            if "WindowsApps" in path:
                reason = (
                    "is a Microsoft Store App Execution Alias: it exists on PATH but launches the Store instead "
                    "of running Python"
                )
            out.append((f"{path}  [{source}]", reason))
    return out


def describe() -> str:
    """A one-block report for a model or a doctor command."""
    best = find()
    lines: list[str] = []
    if best is None:
        lines.append("No working Python interpreter was found. These were tried and rejected:")
        for path, reason in rejected():
            lines.append(f"  {path} -- {reason}")
        lines.append(
            "If you need Python, install it, or point Myriaxon at one with the MYRIAXON_PYTHON environment variable."
        )
        return "\n".join(lines)

    lines.append(f"Python: {best.render()}")
    lines.append(f"  source: {best.source}")
    bad = rejected()
    if bad:
        # Named because a model that has just been told "python is not installed" needs to know why it thought so.
        lines.append("Rejected candidates (do not use these):")
        for path, reason in bad:
            lines.append(f"  {path} -- {reason}")
    return "\n".join(lines)


def command() -> str:
    """A shell-safe command for an interpreter, quoted.

    Quoted because every candidate path on this host contains a space or a non-ASCII directory name, and an unquoted
    path is the next bug after this one.
    """
    override = os.environ.get("MYRIAXON_PYTHON", "").strip()
    if override and _probe(override) is not None:
        return f'"{override}"'
    best = find()
    if best is None:
        return ""
    return f'"{best.path}"'
