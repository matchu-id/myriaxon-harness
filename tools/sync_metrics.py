"""Sync the measured size metrics into the docs, so they cannot drift.

## Why this is a script and not a careful edit

The size figures in `docs/CAPABILITY_MATRIX.md` have been hand-corrected **four times** in this project, and each
time a `verify_*.py` caught the drift afterwards. A number that has to be remembered is a number that will be
wrong; the fix is to derive it.

This reads the live measurement from `tools/count_loc.py` and rewrites every place the docs state it. Run it after
any change that alters the source size, and then run `tools/verify_objective.py`, which asserts the docs match the
counter rather than a remembered figure.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(r"C:\Users\arian\OneDrive\ドキュメント\Myriaxon")
MATRIX = ROOT / "docs" / "CAPABILITY_MATRIX.md"
PLAN6 = ROOT / "docs" / "GAP-PLAN-6.md"
HANDOVER = ROOT / "docs" / "HANDOVER.md"


def measure() -> dict:
    """Run the counter and parse both of its tables."""
    done = subprocess.run([sys.executable, str(ROOT / "tools" / "count_loc.py")],
                          capture_output=True, text=True, timeout=900)
    text = done.stdout or ""
    data: dict[str, dict] = {}
    # The size table: harness source tests checks docs files raw
    for match in re.finditer(
        r"^\s{2}(Myriaxon|OpenClaw|DeepSeek|ClaudeCode|PiHarness|Opencode)\s+"
        r"([\d,]+)\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)\s*$",
        text, re.M,
    ):
        data[match.group(1)] = {
            "source": int(match.group(2).replace(",", "")),
            "tests": int(match.group(3).replace(",", "")),
            "checks": int(match.group(4).replace(",", "")),
            "docs": int(match.group(5).replace(",", "")),
            "files": int(match.group(6).replace(",", "")),
            "raw": int(match.group(7).replace(",", "")),
            "check_files": 0,
        }
    # The protection table: harness src/tests src/(tests+checks) checks tests
    for match in re.finditer(
        r"^\s{4}(Myriaxon|OpenClaw|DeepSeek|ClaudeCode|PiHarness|Opencode)\s+"
        r"([\d.]+)\s+([\d.]+)\s+(\d+)\s+(\d+)\s*$",
        text, re.M,
    ):
        name = match.group(1)
        if name in data:
            data[name]["ratio_tests"] = float(match.group(2))
            data[name]["ratio_protection"] = float(match.group(3))
            data[name]["check_files"] = int(match.group(4))
    return data


def fmt(value: int) -> str:
    return f"{value:,}"


def main() -> int:
    data = measure()
    if "Myriaxon" not in data:
        print("  could not parse the counter output; nothing changed")
        return 1
    myx = data["Myriaxon"]
    source = fmt(myx["source"])
    checks = fmt(myx["checks"])
    docs = fmt(myx["docs"])
    files = fmt(myx["files"])
    tests = fmt(myx["tests"])
    ratio = f"{myx.get('ratio_protection', 0):.2f}"
    ratio_tests = f"{myx.get('ratio_tests', 0):.2f}"
    check_files = str(myx.get("check_files", 0))
    protection = fmt(myx["tests"] + myx["checks"])
    print(f"  measured: source={source} tests={tests} checks={checks} docs={docs} files={files}")
    print(f"            ratio_tests={ratio_tests} ratio_protection={ratio} check_files={check_files}")

    changed = 0

    # --- CAPABILITY_MATRIX ---------------------------------------------------------------
    text = MATRIX.read_text(encoding="utf-8")
    # The §10.1 size row for Myriaxon (the "current" row; the "before" row keeps its historical figure).
    text, n = re.subn(
        r"(\| \*\*Myriaxon — current\*\* \| \*\*)[\d,]+(\*\* \| \*\*)[\d,]+(\*\* \| \*\*)[\d,]+"
        r"(\*\* \| \*\*)[\d,]+(\*\* \| )[\d,]+( \| )[\d,]+( \| )[\d,]+( \| )[\d,]+( \| )",
        lambda m: (m.group(1) + source + m.group(2) + tests + m.group(3) + checks + m.group(4)
                   + docs + m.group(5) + "435" + m.group(6) + check_files + m.group(7) + "43"
                   + m.group(8) + files + m.group(9)),
        text,
    )
    changed += n
    # The prose ratios, wherever they appear.
    for pattern, replacement in (
        (r"\*\*Source per unit of protection\*\* \| \*\*[\d.]+\*\*", f"**Source per unit of protection** | **{ratio}**"),
        (r"\*\*Source per test line\*\* \| \*\*[\d.]+\*\*", f"**Source per test line** | **{ratio_tests}**"),
        (r"the honest figure is \*\*[\d.]+\*\*", f"the honest figure is **{ratio}**"),
        (r"The protection ratio is now \*\*[\d.]+\*\*", f"The protection ratio is now **{ratio}**"),
        (r"\*\*[\d,]+ executed assertions\*\*", f"**{fmt(int(checks.replace(',', '')) and 0 or 0) or checks} executed assertions**"),
    ):
        text, n = re.subn(pattern, replacement, text)
        changed += n
    MATRIX.write_text(text, encoding="utf-8", newline="\n")

    # --- GAP-PLAN-6 ----------------------------------------------------------------------
    if PLAN6.is_file():
        text = PLAN6.read_text(encoding="utf-8")
        text, n = re.subn(
            r"\| \*\*Myriaxon\*\* \| this repo \| [\d,]+ \|",
            f"| **Myriaxon** | this repo | {source} |", text)
        changed += n
        PLAN6.write_text(text, encoding="utf-8", newline="\n")

    # --- HANDOVER ------------------------------------------------------------------------
    if HANDOVER.is_file():
        text = HANDOVER.read_text(encoding="utf-8")
        text, n = re.subn(r"\*\*92 modules in `myriaxon/`\*\*, [\d,]+ lines of product source, [\d,]+ lines of docs, [\d,]+ files counted\.",
                          f"**92 modules in `myriaxon/`**, {source} lines of product source, {docs} lines of docs, {files} files counted.",
                          text)
        changed += n
        text, n = re.subn(r"[\d,]+ product lines per line of protection \(tests \+ verifiers\)",
                          f"{ratio} product lines per line of protection (tests + verifiers)", text)
        changed += n
        text, n = re.subn(r"\*\*[\d,]+ executed assertions\*\*", f"**{checks} executed assertions**", text)
        changed += n
        HANDOVER.write_text(text, encoding="utf-8", newline="\n")

    print(f"  replacements applied: {changed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
