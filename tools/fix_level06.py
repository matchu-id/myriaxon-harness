#!/usr/bin/env python3
"""Correct level 06's interaction-feature expectations.

The question keeps only category-season combinations with **at least 2 orders**, and the fixture gives
winter/jackets 2 and winter/boots 2, while summer/jackets and summer/boots have 1 each. So the result is
two rows, not four -- I wrote all four, contradicting the rule stated in the question's own prompt.

This is the same class of mistake as level 04's excluded category: the expectation was written from the
shape of the fixture rather than from the rule, and the rule is what the row filter follows. Running the
reference query is what exposes it, which is why the expectation is never typed by hand any more.
"""

from __future__ import annotations

import json
from pathlib import Path

PATH = Path(r"C:\Users\arian\OneDrive\ドキュメント\Myriaxon\certivo-rework\ai-ml-engineer\levels\06.json")

CORRECT = [
    {"category": "boots", "season": "winter", "n": 2, "return_pct": 0.0},
    {"category": "jackets", "season": "winter", "n": 2, "return_pct": 100.0},
]


def main() -> int:
    data = json.loads(PATH.read_text(encoding="utf-8"))
    fixed = 0

    for question in data.get("questions") or []:
        spec = question.get("code") or {}
        setup = spec.get("setupSql") or ""
        # Select on the fixture's distinctive season values, not on the starter code.
        if "'winter',1" not in setup:
            continue
        spec["tests"][0]["name"] = (
            "only the two winter combinations clear the size rule; jackets at 100% against boots at 0%"
        )
        spec["tests"][1]["name"] = (
            "hidden: each summer combination has one order and is dropped, however striking its rate"
        )
        for test in spec["tests"]:
            test["expected"] = json.loads(json.dumps(CORRECT))
            fixed += 1

    PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8", newline="\n")
    print(f"  corrected {fixed} expectation(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
