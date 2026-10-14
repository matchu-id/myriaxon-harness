#!/usr/bin/env python3
"""Remove named multiple-choice questions from a level by prompt fragment.

`shape_essay_level.py` drops multiple-choice questions to reach the essay-level shape, but the no-essay
levels need the same operation for a different reason: the count overshot when the questions were
appended in a batch. This does that one job, and reports the resulting counts so the caller can see
whether the level is now the right size.

Usage:  python3 drop_questions.py <level.json> --drop "fragment" ["fragment" ...]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

TARGET = {"mc": 21, "code": 4, "essay": 0}


def kind_of(question: dict) -> str:
    if "options" in question:
        return "mc"
    if "code" in question:
        return "code"
    if "essay" in question:
        return "essay"
    return "?"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("level")
    parser.add_argument("--drop", nargs="*", required=True)
    parser.add_argument("--target-mc", type=int, default=TARGET["mc"])
    args = parser.parse_args()

    path = Path(args.level)
    data = json.loads(path.read_text(encoding="utf-8"))
    questions = data["questions"]

    for fragment in args.drop:
        matches = [q for q in questions if kind_of(q) == "mc" and fragment in q.get("prompt", "")]
        if not matches:
            print(f"  --drop {fragment!r} matched no multiple-choice question")
            continue
        for match in matches:
            questions.remove(match)
            print(f"  dropped: {match['prompt'][:70]}...")

    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8", newline="\n")

    counts = {"mc": 0, "code": 0, "essay": 0}
    for question in questions:
        counts[kind_of(question)] += 1
    print(f"  now: {len(questions)} questions  mc={counts['mc']} code={counts['code']} "
          f"essay={counts['essay']}")
    if counts["mc"] != args.target_mc:
        print(f"  SHAPE NOT MET: {counts['mc']} multiple-choice, target {args.target_mc}")
        return 1
    print("  shape OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
