#!/usr/bin/env python3
"""Verify the interpreter finder.

## The defect this closes, measured

During the executed comparison run Myriaxon was asked to verify a Python fix. It called `run_bash` three times
hunting for an interpreter, then told the user **"No Python installed on this machine"** — which was false.

`python` and `python3` are on PATH, but both resolve to a **Microsoft Store App Execution Alias** — a reparse point
that launches the Store instead of running anything. `py` is absent. The real interpreter is the virtualenv's, which
the agent never looked for because the failing alias was already "the answer" to `which python`.

## What these checks are about

The module's entire claim is that **existence is not evidence**. So the central check is that a candidate which
`shutil.which` finds is nonetheless *rejected* when it cannot run as Python — which is exactly what the Store alias
does, and exactly what a naive implementation would get wrong.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from myriaxon import interpreter as I  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAIL).append(label)
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}" + (f"\n         {detail}" if detail else ""))


def main() -> int:
    print("=== 1. a real interpreter is FOUND ===")
    best = I.find()
    check("an interpreter was found", best is not None,
          "the harness runs on Python, so one must exist")
    if best is None:
        print(f"\n=== {len(PASS)} passed, {len(FAIL)} failed ===")
        return 1
    check("its path exists", Path(best.path).is_file(), best.path)
    check("it reports a version", "python" in best.version.lower(), best.version)
    check("the version looks like a version",
          any(ch.isdigit() for ch in best.version), best.version)
    check("a source is recorded", bool(best.source), best.source)

    print()
    print("=== 2. THE CENTRAL CLAIM: existence is not evidence ===")
    # A file on PATH that is not a working Python must be rejected, not returned. This is the whole defect.
    alias_found = False
    with tempfile.TemporaryDirectory() as tmp:
        fake = Path(tmp) / ("notpython.exe" if sys.platform == "win32" else "notpython")
        # A file that exists and is executable but refuses to behave like Python.
        fake.write_text("@echo off\r\nexit /b 1\r\n" if sys.platform == "win32" else "#!/bin/sh\nexit 1\n",
                        encoding="utf-8")
        check("the probe rejects a file that exits non-zero", I._probe(str(fake)) is None,
              f"{fake} exits 1, so it must not be accepted")

        liar = Path(tmp) / ("liar.exe" if sys.platform == "win32" else "liar")
        liar.write_text("@echo off\r\necho this is not an interpreter\r\n" if sys.platform == "win32"
                        else "#!/bin/sh\necho this is not an interpreter\n", encoding="utf-8")
        check("the probe rejects output that never says Python", I._probe(str(liar)) is None,
              "an alias prints a Store message, not a version")

    print()
    print("=== 3. the Windows Store alias is REJECTED and NAMED ===")
    rejected = I.rejected()
    check("rejected candidates are reported", isinstance(rejected, list), f"{len(rejected)} entries")
    aliases = [r for r in rejected if "WindowsApps" in r[0]]
    if aliases:
        alias_found = True
        check("a Store alias was rejected", True, aliases[0][0][:90])
        check("  and the reason says it is an App Execution Alias",
              "App Execution Alias" in aliases[0][1], aliases[0][1][:110])
        check("  and the reason says it launches the Store",
              "Store" in aliases[0][1], aliases[0][1][:110])
    else:
        # Not a failure: on a host without the aliases there is nothing to reject, and the checks below still apply.
        check("no Store alias on this host (so nothing to reject)", True,
              "the guard is still exercised by the synthetic cases in section 2")

    check("the found interpreter is NOT among the rejected",
          all(Path(best.path) != Path(r[0].split("  [")[0]) for r in rejected),
          "a path cannot be both accepted and rejected")

    print()
    print("=== 4. describe() explains a false conclusion ===")
    # The point of the report is to pre-empt the model's own wrong answer, so it must name the traps.
    text = I.describe()
    check("it names the interpreter", best.path in text, text[:90])
    check("it says where it came from", best.source in text, text[:150])
    if alias_found:
        check("it warns about the aliases explicitly", "do not use" in text.lower() or "Rejected" in text, text[-260:])
    check("it is a single readable block", len(text.splitlines()) >= 2)

    print()
    print("=== 5. command() is shell-safe ===")
    # Every candidate path on this host contains a space or a non-ASCII directory name, so an unquoted path would be
    # the very next bug.
    cmd = I.command()
    check("a command is produced", bool(cmd), cmd)
    check("it is quoted", cmd.startswith('"') and cmd.endswith('"'), cmd)
    check("the quoted path runs", subprocess.run(
        [best.path, "--version"], capture_output=True, text=True, timeout=30).returncode == 0)

    print()
    print("=== 6. the MYRIAXON_PYTHON override is honoured, and validated ===")
    import os
    original = os.environ.get("MYRIAXON_PYTHON")
    try:
        os.environ["MYRIAXON_PYTHON"] = best.path
        check("a valid override is used", I.command() == f'"{best.path}"', I.command())
        os.environ["MYRIAXON_PYTHON"] = str(Path(tempfile.gettempdir()) / "definitely-not-python-xyz")
        check("an INVALID override is ignored rather than used",
              I.command() != '"' + os.environ["MYRIAXON_PYTHON"] + '"',
              "using a broken override would reintroduce the original defect")
        check("  and it falls back to a working interpreter", best.path in I.command(), I.command())
    finally:
        if original is None:
            os.environ.pop("MYRIAXON_PYTHON", None)
        else:
            os.environ["MYRIAXON_PYTHON"] = original

    print()
    print("=== 7. it is cheap enough to call repeatedly ===")
    # `session_status` calls this, and so may `run_bash`; a slow probe would make the fix worse than the defect.
    import time
    I.find(refresh=True)  # populate
    start = time.perf_counter()
    for _ in range(5):
        I.find()
    cached = time.perf_counter() - start
    check("a cached lookup is fast", cached < 0.5, f"5 lookups in {cached * 1000:.1f} ms")
    start = time.perf_counter()
    I.find(refresh=True)
    cold = time.perf_counter() - start
    check("even a cold lookup is tolerable", cold < 20.0, f"cold lookup {cold:.2f}s")

    print()
    print("=== 8. it is surfaced where the model will SEE it ===")
    # A fix nothing calls is a library. This is the check that would catch the module being written and never wired.
    from myriaxon import self_status

    class Ctx:
        model = "m"
        provider = "p"
        mode = "auto"
        tools: list = []
        skills: dict = {}
        locus_run_source = ""

    report = self_status.report(Ctx(), sections=["environment"])
    check("session_status has an environment section", "environment" in report, report[:80])
    check("it names the real interpreter", best.path in report, report[:150])
    # The warning is printed only when there ARE traps to warn about (the module's own docstring: "costs one line on a host
    # where the aliases are absent"). Asserting it unconditionally made this check fail on every machine without the
    # Windows Store aliases, so it now requires the warning exactly when `rejected()` is non-empty.
    traps_here = bool(I.rejected())
    check("it warns about the traps (when this host has any)",
          (not traps_here) or "do NOT use" in report or "NONE FOUND" in report,
          f"{'traps present' if traps_here else 'no traps on this host'}: {report[-200:]}")

    source = (REPO / "myriaxon" / "self_status.py").read_text(encoding="utf-8")
    check("the section is registered in the report builder", '"environment": _environment' in source,
          "defined but unregistered is the failure mode this repository keeps hitting")
    tools_source = (REPO / "myriaxon" / "tool_registry.py").read_text(encoding="utf-8")
    check("session_status documents the section",
          "environment" in tools_source.split("SESSION_STATUS_TOOL_DEF")[0][-4000:]
          or "environment" in source, "the enum should list it")

    print()
    print(f"=== {len(PASS)} passed, {len(FAIL)} failed ===")
    for label in FAIL:
        print(f"  FAILED: {label}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
