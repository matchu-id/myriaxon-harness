#!/usr/bin/env python3
"""Audit one Certivo track for the defects the rework is meant to remove.

Three things make a multiple-choice question guessable without knowing the answer:

  1. the correct option is the longest (or shortest) one, so length alone gives it away;
  2. the correct option is always first, so position gives it away;
  3. distractors are obviously wrong on sight.

This reports on the first two, which are measurable, and on the current shape of every level: how many
questions, of which kinds, and which languages the code questions use. The third is a judgement call
and is left to the rework, but the length spread is reported because a single option far longer than
the rest is the usual cause.

Usage:  python3 audit_track.py <track-dir>
"""

from __future__ import annotations

import json
import statistics
import sys
from collections import Counter
from pathlib import Path

LETTERS = "ABCDEFGH"


def option_text(option: object) -> str:
    if isinstance(option, str):
        return option
    if isinstance(option, dict):
        for key in ("text", "label", "option", "value", "title"):
            if isinstance(option.get(key), str):
                return option[key]
    return ""


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: audit_track.py <track-dir>")
        return 2

    track = Path(sys.argv[1])
    levels = sorted(track.glob("levels/*.json"))
    if not levels:
        print(f"no level files under {track}")
        return 2

    print(f"=== {track.name}: {len(levels)} level(s) ===")
    print()

    kinds: Counter[str] = Counter()
    languages: Counter[str] = Counter()
    answer_positions: Counter[int] = Counter()
    longest_is_correct = 0
    shortest_is_correct = 0
    graded = 0
    per_level: list[tuple[str, int, int, int]] = []

    for path in levels:
        data = json.loads(path.read_text(encoding="utf-8"))
        questions = data.get("questions") or []

        level_kinds: Counter[str] = Counter()
        for question in questions:
            kind = str(question.get("kind") or question.get("type") or "?")
            kinds[kind] += 1
            level_kinds[kind] += 1
            if kind == "code":
                languages[str(question.get("language") or "?")] += 1

            options = question.get("options")
            if kind != "multiple" and kind != "multiple_choice":
                continue
            if not isinstance(options, list) or len(options) < 2:
                continue

            texts = [option_text(o) for o in options]
            # The correct index, however this schema spells it.
            correct = question.get("correctOptionId") or question.get("correct") or question.get("answer")
            index = None
            if isinstance(correct, int):
                index = correct
            elif isinstance(correct, str):
                for i, option in enumerate(options):
                    if isinstance(option, dict) and option.get("id") == correct:
                        index = i
                        break
                if index is None and len(correct) == 1 and correct.upper() in LETTERS:
                    index = LETTERS.index(correct.upper())
                if index is None:
                    for i, text in enumerate(texts):
                        if text.strip() == correct.strip():
                            index = i
                            break
            if index is None or not (0 <= index < len(texts)):
                continue

            graded += 1
            answer_positions[index] += 1
            lengths = [len(t) for t in texts]
            if lengths[index] == max(lengths) and lengths.count(max(lengths)) == 1:
                longest_is_correct += 1
            if lengths[index] == min(lengths) and lengths.count(min(lengths)) == 1:
                shortest_is_correct += 1

        counts = ", ".join(f"{k}={v}" for k, v in sorted(level_kinds.items()))
        per_level.append((path.stem, len(questions), level_kinds.get("code", 0),
                          level_kinds.get("multiple", 0) + level_kinds.get("multiple_choice", 0)))

    print("  --- per level ---")
    print(f"  {'lvl':>4}  {'total':>6}  {'code':>5}  {'mc':>4}")
    for name, total, code, mc in per_level:
        print(f"  {name:>4}  {total:>6}  {code:>5}  {mc:>4}")
    print()
    print(f"  question kinds : {dict(kinds)}")
    print(f"  code languages : {dict(languages)}")
    print()
    print(f"  --- guessability, over {graded} graded question(s) ---")
    if graded:
        spread = ", ".join(f"{LETTERS[i]}={answer_positions.get(i, 0)}" for i in range(4))
        print(f"  answer position: {spread}")
        pct_long = 100.0 * longest_is_correct / graded
        pct_short = 100.0 * shortest_is_correct / graded
        print(f"  correct is the LONGEST  option: {longest_is_correct:>4}  ({pct_long:.1f}%)")
        print(f"  correct is the SHORTEST option: {shortest_is_correct:>4}  ({pct_short:.1f}%)")
        print()
        # A fair question sits near 25% on each. 50%+ means length alone is a coin flip.
        if pct_long > 40:
            print(f"  >> DEFECT: length gives away the answer on {pct_long:.0f}% of questions")
        if pct_short > 40:
            print(f"  >> DEFECT: shortness gives away the answer on {pct_short:.0f}% of questions")
        expected = graded / 4
        worst = max(answer_positions.get(i, 0) for i in range(4))
        if expected and worst > expected * 2:
            print(f"  >> DEFECT: position gives it away -- one letter holds {worst} of {graded}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
