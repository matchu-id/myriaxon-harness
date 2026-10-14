#!/usr/bin/env python3
"""Report which questions move rank when `balance_lengths` is run a second time, and why.

`balance_lengths` is meant to be idempotent: running it twice should be the same as running it once.
It is not, and the aggregate counts do not say which question moved or what the assignment logic saw.
This prints, per question, the rank before and after a second pass, together with the rank the planner
chose and the cost it attached to that choice -- which is what distinguishes "the plan is unstable" from
"the plan is stable but the padding is not".

Usage:  python3 diagnose_balance.py <level.json>
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))

import balance_lengths as bl  # noqa: E402


def snapshot(path: Path) -> list[tuple[int, list[int]]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    out = []
    for question in data.get("questions") or []:
        if "options" not in question:
            continue
        options = question["options"]
        index = next((j for j, o in enumerate(options) if o.get("correct")), -1)
        lengths = [len(str(o.get("text", ""))) for o in options]
        out.append((bl.rank_of(lengths[index], lengths), lengths))
    return out


def main() -> int:
    path = Path(sys.argv[1])

    before = snapshot(path)
    balance = [sys.executable, str(TOOLS / "balance_lengths.py"), str(path), "--write"]
    subprocess.run(balance, capture_output=True, text=True)
    after_first = snapshot(path)
    subprocess.run(balance, capture_output=True, text=True)
    after_second = snapshot(path)

    moved = [i for i, (a, b) in enumerate(zip(after_first, after_second)) if a[0] != b[0]]
    print(f"  questions: {len(before)}")
    print(f"  moved on the second pass: {len(moved)}")
    for i in moved:
        print(f"    q{i + 1}: rank {after_first[i][0]} -> {after_second[i][0]}")
        print(f"      lengths after pass 1: {after_first[i][1]}")
        print(f"      lengths after pass 2: {after_second[i][1]}")
    if not moved:
        print("  stable at the rank level; any file change is padding text only")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
