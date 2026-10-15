#!/usr/bin/env python3
"""Is the installed launcher genuine? Ask Windows, and refuse to guess.

WHY THIS IS A TOOL AND NOT A PARAGRAPH IN A README

When an antivirus flags a file, the useful response is evidence, not reassurance. "Trust us" from the people who
shipped the file is worth nothing, so this prints what Windows itself reports, along with the numbers somebody
needs to file a detection report with a vendor: the path, the size, the SHA-256, the signature state, and every
version field.

It also reports the two facts that explain a detection, rather than hiding them:

  * the launcher is **NotSigned** -- code signing needs a certificate from a certificate authority, and nothing
    in this repository can substitute for one;
  * Windows sees **no version information at all** -- no company, no product, no description, because pip's
    launcher template carries none that the loader can read.

Neither is a defect in this project's code. Both are why a scanner may flag it. Saying so plainly is the point.

USAGE

    python tools/verify_binary_identity.py                 # find the launcher automatically
    python tools/verify_binary_identity.py path/to/file    # check a specific file
    python tools/verify_binary_identity.py --json          # machine-readable, for a bug report
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

# The size band a pip console-script launcher falls in. Measured across five installs on one machine:
# 108,345 / 108,347 / 108,353 / 108,357 / 108,408 bytes. The spread comes from the embedded install path.
TEMPLATE_MIN = 100_000
TEMPLATE_MAX = 120_000

VERDICT_OK = "GENUINE"
VERDICT_WRONG_SIZE = "UNEXPECTED SIZE"
VERDICT_NOT_PE = "NOT A WINDOWS EXECUTABLE"
VERDICT_MISSING = "NOT FOUND"


def find_launchers() -> list[Path]:
    """Every installed launcher this interpreter or the repo knows about."""
    found: list[Path] = []
    scripts = Path(sys.executable).parent
    for name in ("myriaxon.exe", "myriaxon-harness.exe"):
        candidate = scripts / name
        if candidate.is_file():
            found.append(candidate)
    # Any venv beside the repository, which is how a developer installs it.
    for parent in (REPO, REPO.parent):
        for venv in ("venv", ".venv"):
            candidate = parent / venv / ("Scripts" if sys.platform == "win32" else "bin") / "myriaxon.exe"
            if candidate.is_file() and candidate not in found:
                found.append(candidate)
    return found


def inspect(path: Path) -> dict:
    from myriaxon import launcher_identity as identity

    report = identity.describe(path)
    report["sibling_launchers"] = []
    if path.is_file():
        # The siblings are the strongest evidence there is, and it is one line of output: pip writes one of
        # these per command, so a directory full of same-sized launchers is the template, not a payload.
        for sibling in sorted(path.parent.glob("*.exe"))[:12]:
            if sibling.name == path.name:
                continue
            try:
                report["sibling_launchers"].append({"name": sibling.name, "size": sibling.stat().st_size})
            except OSError:
                continue
    return report


def verdict_for(report: dict) -> tuple[str, list[str]]:
    notes: list[str] = []
    if not report.get("exists"):
        return VERDICT_MISSING, ["the file does not exist, so nothing can be verified"]
    if not report.get("is_pe"):
        return VERDICT_NOT_PE, [f"not a PE image: {report.get('error', 'unknown')}"]
    size = report.get("size", 0)
    if not (TEMPLATE_MIN <= size <= TEMPLATE_MAX):
        notes.append(
            f"size {size:,} bytes is outside the {TEMPLATE_MIN:,}-{TEMPLATE_MAX:,} band a pip launcher falls in"
        )
        return VERDICT_WRONG_SIZE, notes
    notes.append(f"size {size:,} bytes is in the range pip's launcher template produces")
    if report.get("signed") == "NotSigned":
        notes.append("unsigned -- expected for a pip launcher, and the reason a scanner may flag it")
    if report.get("no_identity_at_all"):
        notes.append("Windows reports no version information at all -- also expected, also why it gets flagged")
    elif report.get("carries_project_identity"):
        notes.append("carries this project's identity")
    return VERDICT_OK, notes


def show(report: dict) -> None:
    path = report.get("path", "?")
    print(f"  file        : {path}")
    if not report.get("exists"):
        print("  result      : NOT FOUND")
        return
    print(f"  size        : {report.get('size', 0):,} bytes")
    print(f"  sha256      : {report.get('sha256', '')}")
    print(f"  signature   : {report.get('signed')}")
    print(f"  PE image    : {report.get('is_pe')}")
    print(f"  resource ids: {report.get('resource_types')}")

    version = report.get("version") or {}
    if version and "error" not in version:
        print("  Windows reports:")
        for key, value in version.items():
            print(f"      {key:17}: {value}")
    elif version.get("error"):
        print(f"  Windows read failed: {version['error']}")
    else:
        print("  Windows reports NO version information at all:")
        for field in ("CompanyName", "ProductName", "FileDescription", "FileVersion",
                      "LegalCopyright", "OriginalFilename"):
            print(f"      {field:17}: (empty)")

    siblings = report.get("sibling_launchers") or []
    if siblings:
        print(f"  sibling launchers in the same directory ({len(siblings)}):")
        for item in siblings[:6]:
            print(f"      {item['size']:>7,} bytes  {item['name']}")

    verdict, notes = verdict_for(report)
    print()
    print(f"  VERDICT     : {verdict}")
    for note in notes:
        print(f"      - {note}")
    print()
    print("  This file is generated by pip, not committed to the repository. Verify that with:")
    print("      git ls-files | grep -i '\\.exe$'      # no output")
    print("  Full explanation: docs/FALSE-POSITIVE.md")


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify the installed launcher is genuine.")
    parser.add_argument("path", nargs="?", help="a specific file; otherwise the launchers are found automatically")
    parser.add_argument("--json", action="store_true", help="machine-readable, for a bug report")
    args = parser.parse_args()

    targets = [Path(args.path)] if args.path else find_launchers()
    if not targets:
        # Not a failure. On a machine where only `python -m myriaxon` is used there IS no launcher, and
        # reporting that as an error would teach somebody to ignore this tool.
        print("  no myriaxon launcher found.")
        print("  That is fine: `python -m myriaxon` is the same program and needs no launcher.")
        print(f"  Looked beside {Path(sys.executable).parent}")
        return 0

    reports = [inspect(target) for target in targets]
    if args.json:
        print(json.dumps(reports, indent=2, ensure_ascii=False, default=str))
    else:
        for index, report in enumerate(reports):
            if index:
                print()
            show(report)

    problems = [r for r in reports if verdict_for(r)[0] != VERDICT_OK]
    print()
    print("=" * 78)
    print(f"  {len(reports) - len(problems)}/{len(reports)} launcher(s) look like the pip template")
    if problems:
        for report in problems:
            print(f"    {verdict_for(report)[0]}: {report.get('path')}")
        print("  A launcher that is NOT the template deserves investigation -- see docs/FALSE-POSITIVE.md")
    print("=" * 78)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
