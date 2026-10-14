#!/usr/bin/env python3
"""Print a translated level's Indonesian beside its English, for human review.

The guard proves a translation changed nothing but the `Id` fields, and `review_localisation.py` checks
markdown parity and register mechanically. Neither can tell whether the Indonesian reads well or uses the
right vocabulary for the domain, which is what a reviewer has to judge — and the user asked to review one
level before the remaining sixty are produced. This prints samples in the order a reviewer wants them:
the case study first, then a multiple-choice question with its options, then a code question's instructions.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="Show a translation beside its original.")
    parser.add_argument("path", type=Path)
    parser.add_argument("--width", type=int, default=200)
    parser.add_argument("--question", type=int, default=0, help="0-based index of an MC question to show")
    args = parser.parse_args()

    data = json.loads(args.path.read_text(encoding="utf-8"))
    case = data.get("caseStudy", {})

    print("  === CASE STUDY ===")
    print(f"  EN: {case.get('title')}")
    print(f"  ID: {case.get('titleId')}")
    print()
    print("  --- scenario ---")
    print(f"  EN: {str(case.get('scenarioMarkdown'))[:args.width]}")
    print()
    print(f"  ID: {str(case.get('scenarioMarkdownId'))[:args.width]}")
    print()

    multiple_choice = [q for q in data["questions"] if "options" in q]
    if multiple_choice:
        index = min(args.question, len(multiple_choice) - 1)
        question = multiple_choice[index]
        print(f"  === MULTIPLE CHOICE (index {index}) ===")
        print(f"  EN: {question['prompt'][:args.width]}")
        print(f"  ID: {question['promptId'][:args.width]}")
        print()
        for option in question["options"]:
            mark = "*" if option.get("correct") else " "
            print(f"   {mark} EN: {option['text'][:args.width]}")
            print(f"     ID: {option['textId'][:args.width]}")
        print()
        print("  --- explanation ---")
        print(f"  EN: {question['explanation'][:args.width]}")
        print()
        print(f"  ID: {question['explanationId'][:args.width]}")
        print()

    code = [q for q in data["questions"] if "code" in q]
    if code:
        spec = code[0]["code"]
        print("  === CODE INSTRUCTIONS ===")
        print(f"  EN: {spec['instructionsMarkdown'][:args.width]}")
        print()
        print(f"  ID: {spec.get('instructionsMarkdownId', '<missing>')[:args.width]}")
        print()

    essay = [q for q in data["questions"] if "essay" in q]
    if essay:
        rubric = essay[0]["essay"]
        print("  === ESSAY RUBRIC ===")
        print(f"  EN: {rubric['rubricMarkdown'][:args.width]}")
        print()
        print(f"  ID: {rubric.get('rubricMarkdownId', '<missing>')[:args.width]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
