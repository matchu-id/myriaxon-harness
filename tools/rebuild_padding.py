#!/usr/bin/env python3
"""Rebuild a level's distractor padding from the pristine text, then balance it in one pass.

Why this exists: `balance_lengths.py` can only EXTEND a distractor, never shorten it, because trimming
would change meaning. So running it twice does not converge to the same result as running it once --
the second run sees already-padded text and pads relative to that. Measured: after two passes the
correct option was the SHORTEST in 57% of questions, having been the longest in 71% before, which is
trading one tell for another rather than removing it.

The fix is to strip neutral padding first, restoring the authored text, then apply a single balancing
pass. Stripping is safe because the padding clauses are a fixed, known set that this tool added.

Usage:  python3 rebuild_padding.py <level.json> [--write]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Every `balance_lengths` PADDING entry, longest first so a repeated clause peels off one at a time
# rather than leaving a fragment behind. Kept in step with that file by hand: if the two lists drift,
# stripping silently stops working and every level accumulates padding on each rebuild, which is the
# failure this module exists to reverse.
CLAUSES = (
    " under every condition measured so far",
    ", though it is sometimes assumed to be true",
    " regardless of how the data is prepared",
    ", which does not hold here",
    " in this situation",
)


def strip_padding(text: str) -> tuple[str, int]:
    """Remove trailing padding clauses, returning the original text and how many were removed.

    The loop repeats until a full pass finds nothing. A single pass over the clause list is not enough:
    measured, distractors accumulated `in this situation` twice because each rebuild removed one copy of
    one clause and the next balancing pass added another. Stripping to a fixed point is what makes this
    step idempotent, and idempotence here is what stops the padding compounding across rebuilds.
    """
    removed = 0
    changed = True
    while changed:
        changed = False
        for clause in CLAUSES:
            if text.endswith(clause):
                text = text[: -len(clause)].rstrip()
                removed += 1
                changed = True
    return text, removed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("path")
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()

    path = Path(args.path)
    data = json.loads(path.read_text(encoding="utf-8"))
    stripped = 0

    for question in data.get("questions") or []:
        for option in question.get("options") or []:
            text = str(option.get("text", ""))
            cleaned, removed = strip_padding(text)
            if removed:
                option["text"] = cleaned
                stripped += removed

    print(f"  {path.name}: stripped {stripped} padding clause(s)")
    if args.write:
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                        encoding="utf-8", newline="\n")
        print(f"    written to {path}")
    else:
        print("    dry run -- pass --write to apply")
    return 0


if __name__ == "__main__":
    sys.exit(main())
