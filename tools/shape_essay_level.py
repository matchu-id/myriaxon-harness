#!/usr/bin/env python3
"""Bring an essay level to its final shape: 18 multiple-choice + 4 code + 3 essay = 25.

Two things go wrong the same way on every essay level, so this handles both for any level rather than
being rewritten each time.

1. **Essays ship without `explanation`.** The question schema requires one on every question kind. The
   rubric is what the grader uses and the learner never sees it, so the explanation is a separate piece
   of writing: the reasoning the question exists to draw out, shown after the attempt. The texts live in
   `essay_explanations.json` beside this tool, keyed by a distinctive fragment of each prompt.

2. **The level arrives with too many questions.** The brief fixes 25 per level; with 4 code and 3 essay
   that leaves 18 multiple-choice. Any extra is dropped, and which to drop is a judgement: prefer the ones
   whose material the essays already cover in depth, since a multiple-choice question and an essay on the
   same topic duplicate each other and the slot is better spent elsewhere.

Usage:
  python3 shape_essay_level.py <level.json>            # report only
  python3 shape_essay_level.py <level.json> --drop A B C
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
EXPLANATIONS = TOOLS / "essay_explanations.json"

TARGET_MC = 18
TARGET_CODE = 4
TARGET_ESSAY = 3


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
    parser.add_argument("--drop", nargs="*", default=None,
                        help="prompt substrings identifying which multiple-choice questions to remove")
    args = parser.parse_args()

    path = Path(args.level)
    data = json.loads(path.read_text(encoding="utf-8"))
    questions = data["questions"]
    texts = json.loads(EXPLANATIONS.read_text(encoding="utf-8")) if EXPLANATIONS.exists() else {}

    filled, missing = 0, []
    for question in questions:
        if kind_of(question) != "essay" or question.get("explanation"):
            continue
        prompt = question.get("prompt", "")
        for fragment, explanation in texts.items():
            if fragment in prompt:
                question["explanation"] = explanation
                filled += 1
                break
        else:
            missing.append(prompt[:70])

    if args.drop:
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

    print(f"  explanations filled: {filled}")
    for prompt in missing:
        print(f"  NO EXPLANATION PREPARED: {prompt}...")
    print(f"  now: {len(questions)} questions  mc={counts['mc']} code={counts['code']} "
          f"essay={counts['essay']}")

    wanted = TARGET_MC + TARGET_CODE + TARGET_ESSAY
    if len(questions) != wanted or counts["mc"] != TARGET_MC or counts["code"] != TARGET_CODE \
            or counts["essay"] != TARGET_ESSAY:
        excess = counts["mc"] - TARGET_MC
        hint = (f"drop {excess} more multiple-choice question(s) with --drop"
                if excess > 0 else "add questions to reach the shape")
        print(f"  SHAPE NOT MET: target {TARGET_MC} mc + {TARGET_CODE} code + {TARGET_ESSAY} essay = "
              f"{wanted}; {hint}")
        return 1

    print("  shape OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
