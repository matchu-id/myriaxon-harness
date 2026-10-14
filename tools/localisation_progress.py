#!/usr/bin/env python3
"""Report Indonesian translation progress and verify every translated file that exists.

Two questions matter and they have different answers: how many files have been produced, and are the ones
that exist actually sound. A count of `.id.json` files says nothing about whether the guard would accept them,
and an agent reporting "20/20 done" is not evidence. So this re-runs the guard itself over every translated
file and reports coverage, rather than trusting any summary.

It also distinguishes a *filled* translation map from an extracted one: extraction produces a map whose
values are the English text, so a map that still equals its source means no translation has happened yet,
even though the file exists on disk and looks like progress.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(r"C:\Users\arian\OneDrive\ドキュメント\Myriaxon")
TRACKS = ("ai-ml-engineer", "fullstack-developer", "data-scientist")
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
GUARD = ROOT / "tools" / "guard_localisation.py"


def english_value(data: dict, path: str):
    """Resolve a dotted key path against a level file."""
    parts: list = []
    for part in path.split("."):
        parts.append(int(part) if part.isdigit() else part)
    node = data
    for part in parts:
        node = node[part]
    return node


def main() -> int:
    grand_files = grand_strings = grand_failed = 0
    for track in TRACKS:
        levels = ROOT / "certivo-rework" / track / "levels"
        maps = ROOT / "localisation"
        translated = []
        edited = 0
        for map_path in sorted(maps.glob(f"{track}-*.json")):
            source = levels / map_path.name.replace(f"{track}-", "")
            if not source.exists():
                continue
            mapping = json.loads(map_path.read_text(encoding="utf-8"))
            level = json.loads(source.read_text(encoding="utf-8"))
            changed = 0
            for path, value in mapping.items():
                try:
                    if english_value(level, path) != value:
                        changed += 1
                except (KeyError, IndexError, TypeError):
                    changed += 1
            if changed:
                edited += 1

        strings = 0
        failed: list[str] = []
        for produced in sorted(levels.glob("*.id.json")):
            original = levels / produced.name.replace(".id.json", ".json")
            run = subprocess.run(
                [str(PYTHON), str(GUARD), str(original), str(produced)],
                capture_output=True, text=True, encoding="utf-8", timeout=180,
            )
            translated.append(produced.name)
            if run.returncode == 0:
                for line in run.stdout.splitlines():
                    if "string(s) have" in line:
                        strings += int(line.strip().split("/")[1].split(" ")[0])
            else:
                failed.append(produced.name)

        grand_files += len(translated)
        grand_strings += strings
        grand_failed += len(failed)
        print(f"  {track}")
        print(f"    translation maps edited : {edited:>2}/20")
        print(f"    .id.json files verified : {len(translated):>2}/20"
              f"{'  (' + str(strings) + ' strings)' if strings else ''}")
        if failed:
            print(f"    GUARD FAILURES          : {', '.join(failed)}")

    print()
    print(f"  {grand_files}/60 file(s) verified by the guard, {grand_strings} string(s) covered, "
          f"{grand_failed} failure(s)")
    return 1 if grand_failed else 0


if __name__ == "__main__":
    sys.exit(main())
