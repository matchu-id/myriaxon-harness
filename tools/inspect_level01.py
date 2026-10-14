#!/usr/bin/env python3
"""Inspect ai-ml-engineer level 01 in detail: skills, case study, an option-length baseline, and
whether story groups are in use.

The option-length baseline matters because the fix for guessability is to bring the four options of
every question into a similar band. Knowing what band the current content sits in says how much
rewriting that requires.
"""

from __future__ import annotations

import json
from pathlib import Path

PATH = Path("/srv/certivo/content/tracks/ai-ml-engineer/levels/01.json")


def main() -> int:
    data = json.loads(PATH.read_text(encoding="utf-8"))
    questions = data.get("questions") or []

    print("=== exam block ===")
    print(" ", json.dumps(data.get("exam"), indent=2).replace("\n", "\n  "))
    print()
    print("=== case study ===")
    case = data.get("caseStudy") or {}
    print("  title:", case.get("title"))
    print("  scenario (first 700 chars):")
    print("   ", (case.get("scenarioMarkdown") or "")[:700].replace("\n", "\n    "))
    print()
    print("=== references ===")
    for ref in data.get("references") or []:
        print(f"  [{ref.get('resourceType')}] {ref.get('title')} -> {ref.get('url')}")
    print()
    print("=== story groups in use? ===")
    groups = {}
    for q in questions:
        sg = q.get("storyGroup")
        if sg:
            groups.setdefault(sg.get("id"), []).append(q)
    if groups:
        for gid, items in groups.items():
            print(f"  {gid}: {len(items)} question(s)")
    else:
        print("  none")
    print()
    print("=== all 20 current questions: skill, prompt, and option lengths ===")
    for i, q in enumerate(questions, 1):
        options = q.get("options") or []
        lengths = [len(o.get("text", "")) for o in options]
        correct = next((j for j, o in enumerate(options) if o.get("correct")), -1)
        letter = "ABCD"[correct] if 0 <= correct < 4 else "?"
        print(f"  {i:>2}. [{q.get('skill')}] correct={letter} lens={lengths}")
        print(f"      Q: {q.get('prompt', '')[:150]}")
        for j, o in enumerate(options):
            mark = "*" if o.get("correct") else " "
            print(f"      {mark}{'ABCD'[j]} ({len(o.get('text','')):>3}) {o.get('text','')[:110]}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
