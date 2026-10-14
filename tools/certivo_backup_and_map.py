#!/usr/bin/env python3
"""Back up the AI/ML track, then report its level structure.

The backup runs first because /srv/certivo is not a git repository: there is no undo, and a malformed
write would destroy 20 levels of hand-written content with nothing to restore from.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

TRACK = Path("/srv/certivo/content/tracks/ai-ml-engineer")
BACKUP_ROOT = Path("/srv/certivo/backups")


def kind_of(question: dict) -> str:
    if "options" in question:
        return "multiple"
    if "code" in question:
        return "code"
    if "essay" in question:
        return "essay"
    return "?"


def main() -> int:
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    dest = BACKUP_ROOT / f"ai-ml-engineer-{stamp}"
    try:
        dest.mkdir(parents=True, exist_ok=False)
        for item in TRACK.iterdir():
            target = dest / item.name
            if item.is_dir():
                shutil.copytree(item, target)
            else:
                shutil.copy2(item, target)
        files = sum(1 for _ in dest.rglob("*") if _.is_file())
        size = sum(f.stat().st_size for f in dest.rglob("*") if f.is_file())
        print(f"  BACKED UP: {dest}")
        print(f"    {files} file(s), {size/1024:.0f} KB")
    except Exception as exc:  # noqa: BLE001
        print(f"  BACKUP FAILED: {type(exc).__name__}: {exc}")
        return 1

    print()
    print("=== ai-ml-engineer level map ===")
    print(f"  {'lvl':>3} {'q':>3} {'mc':>3} {'code':>5} {'essay':>6} {'perAtt':>7} {'pass%':>6}  skills / case")
    for n in range(1, 21):
        path = TRACK / "levels" / f"{n:02d}.json"
        if not path.is_file():
            print(f"  {n:>3}  MISSING")
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        questions = data.get("questions") or []
        counts = Counter(kind_of(q) for q in questions)
        exam = data.get("exam") or {}
        skills = sorted({q.get("skill") for q in questions})
        case = (data.get("caseStudy") or {}).get("title", "")
        print(f"  {n:>3} {len(questions):>3} {counts.get('multiple',0):>3} {counts.get('code',0):>5} "
              f"{counts.get('essay',0):>6} {exam.get('questionsPerAttempt','?'):>7} "
              f"{exam.get('passThresholdPct','?'):>5}%  {case[:40]}")
        print(f"      {', '.join(skills)}")

    print()
    print("=== track.json skills (the 8 the track is built from) ===")
    meta = json.loads((TRACK / "track.json").read_text(encoding="utf-8"))
    for skill in meta.get("skills") or []:
        print(f"  {skill['slug']:26} {skill['name']}")
    print(f"  status: {meta.get('status')}")

    print()
    print("=== focus skills and target increases, per level ===")
    for level in meta.get("levels") or []:
        focuses = ", ".join(level.get("focusSkills") or [])
        print(f"  {level['number']:>3}  {level.get('title','')[:44]:46} focuses: {focuses}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
