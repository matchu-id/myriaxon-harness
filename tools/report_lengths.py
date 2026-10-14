#!/usr/bin/env python3
"""Report which multiple-choice questions are still guessable by option length.

The shuffle fixed position. Length is the other half: if the correct option is consistently the longest
or the shortest, a learner who never reads the prompt still scores well. This lists every question whose
four options are not in a comparable band, worst first, so the distractors that need rewriting are named
rather than guessed at.

Usage:  python3 report_lengths.py <level.json>
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

LETTERS = "ABCD"
# An option band this wide is visibly different on screen: the longest is more than half again the
# shortest. Below it, a reader cannot reliably pick the answer by shape.
TOLERANCE = 1.5


def main() -> int:
    path = Path(sys.argv[1])
    data = json.loads(path.read_text(encoding="utf-8"))
    graded = []

    for i, question in enumerate(data.get("questions") or [], 1):
        if "options" not in question:
            continue
        options = question["options"]
        lengths = [len(str(o.get("text", ""))) for o in options]
        index = next((j for j, o in enumerate(options) if o.get("correct")), -1)
        if index < 0:
            continue
        lo, hi = min(lengths), max(lengths)
        ratio = hi / lo if lo else 99.0
        graded.append((ratio, i, question, lengths, index))

    graded.sort(reverse=True)
    print(f"  {path.name}: {len(graded)} multiple-choice question(s)")
    longest_correct = sum(1 for _, _, _, L, i in graded if L[i] == max(L))
    shortest_correct = sum(1 for _, _, _, L, i in graded if L[i] == min(L))
    print(f"  correct is longest : {longest_correct}/{len(graded)}")
    print(f"  correct is shortest: {shortest_correct}/{len(graded)}")
    print()

    bad = [g for g in graded if g[0] >= TOLERANCE]
    print(f"  --- {len(bad)} question(s) with a length ratio >= {TOLERANCE} ---")
    for ratio, i, question, lengths, index in bad:
        print(f"\n  q{i}  ratio {ratio:.2f}  lengths={lengths}  correct={'ABCD'[index]}")
        print(f"    Q: {question.get('prompt','')[:140]}")
        for j, option in enumerate(question["options"]):
            mark = "*" if j == index else " "
            print(f"    {mark}{LETTERS[j]} ({lengths[j]:>3}) {option.get('text','')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
