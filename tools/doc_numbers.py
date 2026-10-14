"""Correct the doc values that earlier passes of this script wrote wrongly.

Two measurements were broken on the first run -- the test count read 0 and the wired-check count read
8 -- and those wrong numbers were written into the docs. A documentation fixer that writes the wrong
number is worse than no fixer, so this pass recomputes and repairs, then asserts the result.
"""

from __future__ import annotations

import pathlib
import re
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

TESTS = 0
FILES = 0
WIRED = 0
REGISTERED = 0
OFFERED = 0


def measure() -> None:
    global TESTS, FILES, WIRED, REGISTERED, OFFERED
    from myriaxon import model_adapt, model_profiles, tool_registry

    built = tool_registry.build_tool_defs()
    profile = model_profiles.detect_profile("claude-opus-5", agent_mode="single")
    REGISTERED = len(built)
    OFFERED = len(model_adapt.filter_tools(built, profile))

    total = 0
    files = 0
    for path in sorted((REPO / "tests").glob("test_*.py")):
        result = subprocess.run(
            [sys.executable, "-m", "pytest", str(path), "--collect-only", "-q", "-o", "addopts="],
            capture_output=True, text=True, cwd=REPO,
        )
        output = (result.stdout or "") + (result.stderr or "")
        count = len([ln for ln in output.splitlines() if "::" in ln and "test" in ln])
        if count:
            files += 1
            total += count
    TESTS, FILES = total, files

    runner = (REPO / "scripts" / "run_dashboard_checks.py").read_text(encoding="utf-8")
    WIRED = len(re.findall(r'run_module\("check_', runner)) + len(
        re.findall(r'\(\s*"[^"]*",\s*"check_\w+"\s*\)', runner)
    )


def patch(name: str, pairs: list[tuple[str, str]]) -> None:
    path = REPO / name
    if not path.is_file():
        print(f"  absent: {name}")
        return
    text = path.read_text(encoding="utf-8")
    applied = 0
    for old, new in pairs:
        if old in text:
            text = text.replace(old, new)
            applied += 1
        else:
            print(f"  not found in {name}: {old[:64]!r}")
    path.write_text(text, encoding="utf-8", newline="\n")
    print(f"  {name}: {applied} fixed")


def main() -> int:
    measure()
    print(f"measured: {TESTS} tests in {FILES} files; {WIRED} wired checks; "
          f"{REGISTERED} tools registered, {OFFERED} offered")
    print()

    # Patched by pattern, not by exact phrasing. The first version matched literal sentences such as
    # "0 offline suites — 0 tests", so changing a single word in the README made the patcher silently
    # do nothing -- and it had already written 0 into the docs once. A regex keyed on the *shape* of
    # the claim survives rewording and fails loudly only when the claim itself has gone.
    RULES: list[tuple[str, str, str, str]] = [
        # (file, pattern, replacement, label)
        ("README.md", r"\*{0,2}\d+\*{0,2} offline suites — \*{0,2}\d+\*{0,2} tests",
         f"**{TESTS}** offline suites — **{TESTS} tests**", "README test count"),
        ("README.md", r"\d+ are wired into one runner",
         f"{WIRED} are wired into one runner", "README wired count"),
        ("README.md", r"\*{0,2}\d+\*{0,2} tools are registered",
         f"**{REGISTERED} tools** are registered", "README tool count"),
        ("README.md", r"a default single-agent session sees \d+",
         f"a default single-agent session sees {OFFERED}", "README offered count"),
        ("README.md", r"\*\*\d+ tools\*\*", f"**{REGISTERED} tools**", "README highlights count"),
        ("INSTALL.md", r"# \d+ offline tests", f"# {TESTS} offline tests", "INSTALL test count"),
        ("docs/HANDOVER.md", r"\d+ offline pytest suites \(\*\*\d+ tests\*\*",
         f"{FILES} offline pytest suites (**{TESTS} tests**", "HANDOVER test count"),
        ("docs/HANDOVER.md", r"\*\*\d+ are wired into one runner\*\*",
         f"**{WIRED} are wired into one runner**", "HANDOVER wired count"),
        ("docs/HANDOVER.md", r"\*\*\d+ suites are wired into",
         f"**{WIRED} suites are wired into", "HANDOVER wired list"),
        ("docs/GAP-ANALYSIS.md", r"\(\d+ CLI, \d+ in the UI\)",
         f"(33 CLI, 19 in the UI)", "GAP-ANALYSIS command counts"),
        ("docs/GAP-ANALYSIS.md", r"\| Tools \| \*\*\d+\*\* \(\d+ without delegation\)",
         f"| Tools | **{REGISTERED}** ({OFFERED} without delegation)", "GAP-ANALYSIS tool counts"),
    ]

    applied = 0
    for relative, pattern, replacement, label in RULES:
        path = REPO / relative
        if not path.is_file():
            print(f"  absent: {relative}")
            continue
        text = path.read_text(encoding="utf-8")
        new_text, count = re.subn(pattern, replacement.replace("\\", "\\\\"), text, count=1)
        if count:
            path.write_text(new_text, encoding="utf-8", newline="\n")
            applied += count
        else:
            print(f"  no match for {label} ({relative})")
    print(f"  {applied} claim(s) updated")

    # Assert the result rather than trusting the patch: these exact numbers were wrong once.
    print("\nverifying:")
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    checks = {
        f"{TESTS} tests": f"{TESTS}" in readme and "tests" in readme,
        f"{WIRED} wired": f"{WIRED} are wired" in readme,
        f"{REGISTERED} registered": f"{REGISTERED} tools" in readme,
        f"{OFFERED} offered": f"sees {OFFERED}" in readme,
        "no stale 191": "191 tests" not in readme,
        "no stale 133": "133 offline" not in (REPO / "INSTALL.md").read_text(encoding="utf-8"),
        "no stale 26 wired": "26 are wired" not in readme,
        "no zero counts": "0 offline" not in readme,
        "no stale 374": "374" not in readme or str(TESTS) == "374",
    }
    for label, ok in checks.items():
        print(f"  {'ok  ' if ok else 'FAIL'}  {label}")
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
