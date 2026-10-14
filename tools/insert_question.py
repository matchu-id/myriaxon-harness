#!/usr/bin/env python3
"""Insert one multiple-choice question into a level, before its code questions.

Every level needs 25 questions and I keep authoring 20 multiple-choice plus 4 code = 24. Rather than
keep discovering the shortfall at validation time, this appends a prepared question and reports the new
count, so the two steps happen together.

The question is passed as a JSON file so the text can be authored normally and this stays a mechanical
insert. Position matters: it goes before the first `code` question, keeping the reading order
prose-then-practice that every level uses.

Usage:  python3 insert_question.py <level.json> <question.json> [--after-skill SLUG]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import validate_level  # noqa: F401  (kept importable next to this tool for the pipeline's sake)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("level")
    parser.add_argument("question")
    parser.add_argument("--after-skill", default="",
                        help="insert after the last question with this skill, instead of before code")
    args = parser.parse_args()

    level_path = Path(args.level)
    data = json.loads(level_path.read_text(encoding="utf-8"))
    question = json.loads(Path(args.question).read_text(encoding="utf-8"))
    questions = data["questions"]

    if "options" not in question:
        print("  refusing: the inserted question must be multiple choice")
        return 1
    if sum(1 for o in question["options"] if o.get("correct") is True) != 1:
        print("  refusing: the question must have exactly one correct option")
        return 1
    # A duplicate prompt means the file already received this question, so a second run is a no-op
    # rather than a silent duplicate.
    if any(q.get("prompt") == question["prompt"] for q in questions):
        print("  already present, nothing inserted")
        return 0

    if args.after_skill:
        position = max(
            (i for i, q in enumerate(questions) if q.get("skill") == args.after_skill),
            default=len(questions) - 1,
        ) + 1
    else:
        position = next((i for i, q in enumerate(questions) if "code" in q), len(questions))

    questions.insert(position, question)
    level_path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                          encoding="utf-8", newline="\n")

    mc = sum(1 for q in questions if "options" in q)
    code = sum(1 for q in questions if "code" in q)
    essay = sum(1 for q in questions if "essay" in q)
    print(f"  inserted at position {position + 1}")
    print(f"  now: {len(questions)} questions (mc={mc} code={code} essay={essay})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
