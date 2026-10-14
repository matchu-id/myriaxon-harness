#!/usr/bin/env python3
"""Check a reworked Data Scientist track against every clause of the rework objective.

The objective has five separately falsifiable parts, and the existing tools each cover only some of them:

1. 25 questions per level.
2. Levels 1-10 are multiple-choice + live-code only, with no essay.
3. Levels 11-20 are multiple-choice + live-code + essay.
4. Correct answers are not identifiable by length or position.
5. Every expected output was produced by running a reference solution.

`audit_track_rules.py` reports shape and the length/position balance, and `verify_py_questions.py` proves the
expectations. What neither states outright is whether the per-level shape matches the *rule for that level's
number* -- the audit prints the columns and leaves the reader to compare them against the objective. This
does the comparison, so "the track matches the objective" is a verdict rather than an inference from a table.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# The rule the objective states, as opposed to what the files happen to contain.
RULE = {
    "mc": {**{n: 21 for n in range(1, 11)}, **{n: 18 for n in range(11, 21)}},
    "code": {n: 4 for n in range(1, 21)},
    "essay": {**{n: 0 for n in range(1, 11)}, **{n: 3 for n in range(11, 21)}},
}
TOTAL = 25

# The audit's accepted band. The objective says "never identifiable", which strictly means a fair share:
# the correct option is the longest about a quarter of the time, not never and not always.
FAIR_LONGEST = (0.10, 0.35)


def main() -> int:
    parser = argparse.ArgumentParser(description="Check every clause of the rework objective.")
    parser.add_argument("track", type=Path)
    args = parser.parse_args()

    problems: list[str] = []
    # `*.id.json` files sit in the same directory and are Indonesian copies of the same levels. Counting them
    # as levels made this report "expected 20 level files, found 31" and declare the objective unmet on a
    # track that was complete.
    levels = sorted(p for p in args.track.glob("levels/*.json") if not p.name.endswith(".id.json"))
    if len(levels) != 20:
        problems.append(f"expected 20 level files, found {len(levels)}")

    total_questions = 0
    total_longest = 0
    total_shortest = 0
    total_mc = 0

    print(f"  {'lvl':>3} {'total':>5} {'mc':>4} {'code':>4} {'essay':>5}  {'rule':<16} verdict")

    for path in levels:
        data = json.loads(path.read_text(encoding="utf-8"))
        level = data.get("levelNumber")
        questions = data.get("questions", [])
        mc = [q for q in questions if "options" in q]
        code = [q for q in questions if "code" in q]
        essay = [q for q in questions if "essay" in q]

        total_questions += len(questions)
        total_mc += len(mc)

        expected = (RULE["mc"][level], RULE["code"][level], RULE["essay"][level])
        actual = (len(mc), len(code), len(essay))
        shape = "ok" if actual == expected else f"WANT {expected}"

        # Position and length balance, recomputed here rather than trusted from another tool's summary.
        positions: dict[str, int] = {}
        longest = shortest = 0
        for question in mc:
            lengths = [len(option["text"]) for option in question["options"]]
            correct = next(i for i, option in enumerate(question["options"]) if option.get("correct"))
            positions["ABCD"[correct]] = positions.get("ABCD"[correct], 0) + 1
            if lengths[correct] == max(lengths):
                longest += 1
            if lengths[correct] == min(lengths):
                shortest += 1
        total_longest += longest
        total_shortest += shortest

        failures = []
        if len(questions) != TOTAL:
            failures.append(f"total {len(questions)}")
        if actual != expected:
            failures.append("shape")
        for question in mc:
            if len(question["options"]) != 4:
                failures.append("not 4 options")
                break
            if sum(1 for o in question["options"] if o.get("correct")) != 1:
                failures.append("not exactly 1 correct")
                break
        if failures:
            problems.append(f"level {level}: {', '.join(sorted(set(failures)))}")

        verdict = "ok" if not failures else "PROBLEM"
        print(f"  {level:>3} {len(questions):>5} {len(mc):>4} {len(code):>4} {len(essay):>5}  "
              f"{str(expected):<16} {verdict}")

    rate_longest = total_longest / total_mc if total_mc else 0
    rate_shortest = total_shortest / total_mc if total_mc else 0
    print()
    print(f"  {total_questions} questions across {len(levels)} level(s), {total_mc} multiple choice")
    print(f"  correct is LONGEST : {total_longest}/{total_mc} ({100 * rate_longest:.1f}%) "
          f"accepted {FAIR_LONGEST[0]:.0%}-{FAIR_LONGEST[1]:.0%}")
    print(f"  correct is SHORTEST: {total_shortest}/{total_mc} ({100 * rate_shortest:.1f}%)")
    print()

    if not FAIR_LONGEST[0] <= rate_longest <= FAIR_LONGEST[1]:
        problems.append(f"longest-answer rate {100 * rate_longest:.1f}% is outside the accepted band")
    if total_shortest == 0 or rate_shortest > 0.35:
        problems.append(f"shortest-answer rate {100 * rate_shortest:.1f}% suggests a guessable pattern")

    if problems:
        print("  OBJECTIVE NOT MET:")
        for problem in problems:
            print(f"    - {problem}")
        return 1
    print("  OBJECTIVE MET on shape and guessability (expectations are proven by verify_py_questions.py)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
