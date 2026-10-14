#!/usr/bin/env python3
"""Report which questions are stuck at a length rank the balancer cannot move them off.

`balance_lengths.py` only ever pads distractors, because an earlier version that trimmed them also
trimmed authored text. The consequence is that an answer already shorter than every distractor cannot be
moved up the length order — there is nothing to shorten, and lengthening the answer is not something the
tool does.

This prints those questions with their option lengths, so the fix is made in the authoring rather than by
asking the balancer for something it cannot do: either lengthen the correct option or shorten a
distractor in the source.

Usage:  python3 report_stuck_ranks.py <level.json>
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

RANK_NAMES = ("longest", "2nd", "3rd", "shortest")


def main() -> int:
    path = Path(sys.argv[1])
    data = json.loads(path.read_text(encoding="utf-8"))

    for index, question in enumerate(data["questions"], 1):
        options = question.get("options")
        if not options:
            continue
        answer = next((j for j, o in enumerate(options) if o.get("correct")), -1)
        if answer < 0:
            continue
        lengths = [len(str(o.get("text", ""))) for o in options]
        ordered = sorted(lengths, reverse=True)
        rank = ordered.index(lengths[answer])
        if rank != len(RANK_NAMES) - 1:
            continue
        shortest_other = min(l for j, l in enumerate(lengths) if j != answer)
        print(f"  q{index} answer is shortest at {lengths[answer]} chars; "
              f"next shortest distractor is {shortest_other}")
        print(f"      answer: {str(options[answer]['text'])[:100]!r}")
        for j, option in enumerate(options):
            if j != answer:
                print(f"      {'ABCD'[j]} ({lengths[j]:>3}): {str(option['text'])[:90]!r}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
