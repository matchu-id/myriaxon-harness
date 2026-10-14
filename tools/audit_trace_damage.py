#!/usr/bin/env python3
"""Report how much of each answer's text a buggy balancing pass removed.

An earlier `balance_lengths` applied `trim_padding` to the CORRECT answer as well as to distractors. The
padding clauses are ordinary English, so the trimming matched authored prose and deleted it -- silently,
on every run, from 21 of 21 questions. This measures the damage so the affected options can be restored
rather than guessed at.

It cannot repair anything: the removed words are gone from the file. It reports what is missing so the
text can be rewritten deliberately.

Usage:  python3 audit_trace_damage.py <level.json>
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> int:
    path = Path(sys.argv[1])
    data = json.loads(path.read_text(encoding="utf-8"))

    suspicious = 0
    for i, question in enumerate(data.get("questions") or [], 1):
        if "options" not in question:
            continue
        options = question["options"]
        index = next((j for j, o in enumerate(options) if o.get("correct")), -1)
        if index < 0:
            continue
        lengths = [len(str(o.get("text", ""))) for o in options]
        answer = str(options[index].get("text", ""))

        # A correct answer materially shorter than every distractor is the fingerprint of the bug: the
        # answer was trimmed while the distractors were padded.
        if len(answer) + 12 <= min(l for j, l in enumerate(lengths) if j != index):
            suspicious += 1
            print(f"  q{i} answer is {len(answer)} chars but distractors are {lengths}:")
            print(f"      answer : {answer!r}")
            for j, option in enumerate(options):
                if j != index:
                    print(f"      {'ABCD'[j]} ({lengths[j]:>3}) : {str(option.get('text',''))[:110]!r}")

    print()
    print(f"  {suspicious} question(s) show an answer shorter than every distractor")
    if suspicious:
        print("  This is the signature of the trim bug. The affected answers need their wording restored,")
        print("  which means rewriting the option text rather than patching it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
