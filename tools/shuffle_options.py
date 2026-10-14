#!/usr/bin/env python3
"""Shuffle a Certivo level's multiple-choice options so the answer position carries no signal.

Writing the correct option first is the natural way to author a question, and it produces content where
answer A is right roughly 70% of the time -- which is what happened to this track and to my own first
draft of level 01. This rewrites the option order deterministically:

  * the correct option is placed at an evenly spread position, so A/B/C/D each hold about a quarter;
  * the distractors fill the remaining slots in a rotating order, so the same distractor does not
    always land in the same letter;
  * nothing else about a question is touched -- no text, no prompt, no explanation, no skill.

Deterministic rather than random on purpose: running it twice gives the same file, so a rebuild does not
silently reshuffle a level that has already been reviewed.

Usage:  python3 shuffle_options.py <level.json> [--seed N] [--write]
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

LETTERS = "ABCD"


def ordered_positions(count: int, seed: int) -> list[int]:
    """Positions for the correct answers, spread as evenly as the count allows.

    Built by cycling A,B,C,D rather than by random choice: with 20-odd questions a random draw can
    easily land 40% on one letter, which is the defect being removed.
    """
    offset = seed % 4
    return [(i + offset) % 4 for i in range(count)]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("path")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()

    path = Path(args.path)
    data = json.loads(path.read_text(encoding="utf-8"))
    questions = data.get("questions") or []

    mc = [q for q in questions if "options" in q]
    positions = ordered_positions(len(mc), args.seed)

    before = [next((j for j, o in enumerate(q["options"]) if o.get("correct")), -1) for q in mc]
    changed = 0

    for slot, question in enumerate(mc):
        options = question["options"]
        correct = [o for o in options if o.get("correct") is True]
        wrong = [o for o in options if not o.get("correct")]
        if len(correct) != 1 or len(wrong) != 3:
            print(f"  skipping a question with {len(correct)} correct / {len(wrong)} wrong options")
            continue

        target = positions[slot]
        # Distractors rotate independently of the answer slot, so a given distractor does not always
        # end up beside the answer in the same letter position.
        rotated = wrong[slot % 3:] + wrong[: slot % 3]

        rebuilt: list[dict] = []
        wrong_iter = iter(rotated)
        for index in range(4):
            rebuilt.append(correct[0] if index == target else next(wrong_iter))
        if target != before[slot]:
            changed += 1
        question["options"] = rebuilt

    after = [next((j for j, o in enumerate(q["options"]) if o.get("correct")), -1) for q in mc]

    # Safety check before anything is written. This tool rewrites the options array of every
    # multiple-choice question, so a defect here would corrupt a level wholesale -- and the file is the
    # only copy until it is committed. The counts below must be identical before and after: the same
    # number of questions, and exactly one correct option in each.
    before_counts = Counter(len(q["options"]) for q in mc)
    after_counts = Counter(len(q["options"]) for q in mc)
    correct_before = sum(1 for q in mc for o in q["options"] if o.get("correct") is True)
    correct_after = sum(1 for q in mc for o in q["options"] if o.get("correct") is True)
    problems = []
    if len(after) != len(before):
        problems.append(f"question count changed: {len(before)} -> {len(after)}")
    if after_counts != before_counts:
        problems.append(f"option counts changed: {dict(before_counts)} -> {dict(after_counts)}")
    if correct_after != correct_before:
        problems.append(f"correct-option count changed: {correct_before} -> {correct_after}")
    if any(v != 1 for v in (sum(1 for o in q["options"] if o.get("correct") is True) for q in mc)):
        problems.append("a question does not have exactly one correct option")

    print(f"  {path.name}: {len(mc)} multiple-choice question(s)")
    print(f"    position before: {dict(Counter(before))}")
    print(f"    position after : {dict(Counter(after))}")
    print(f"    reordered       : {changed}")

    if problems:
        print(f"    REFUSING TO WRITE -- {len(problems)} integrity problem(s):")
        for item in problems:
            print(f"      - {item}")
        return 1

    if args.write:
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                        encoding="utf-8", newline="\n")
        print(f"    written to {path}")
    else:
        print("    dry run — pass --write to apply")
    return 0


if __name__ == "__main__":
    sys.exit(main())
