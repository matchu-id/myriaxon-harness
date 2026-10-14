#!/usr/bin/env python3
"""Correct level 07's threshold expectations, which I derived by misreading my own fixture.

The fixture holds three genuine returns (o1 0.90, o2 0.80, o4 0.50) and an order scored 0.95 that was
NOT returned. At a threshold of 0.5 all four clear it, plus o3 at 0.60, so:

    flagged = {o1, o2, o3, o4, o8}   tp=3  fp=2  fn=0  tn=3
    precision = 3/5 = 60%            recall = 3/3 = 100%   f1 = 0.75

I had written tp=3 fp=1 fn=1 tn=3 with precision and recall both 75%, which double-counted nothing and
missed nothing but simply did not match the rows: o8 clears 0.5, and all three returns clear it too, so
there is no false negative at this threshold at all.

F1 came out at 0.75 either way -- the same value from different components, which is a good reminder that
a single number agreeing with expectation is not evidence that the reasoning behind it was right.
"""

from __future__ import annotations

import json
from pathlib import Path

PATH = Path(r"C:\Users\arian\OneDrive\ドキュメント\Myriaxon\certivo-rework\ai-ml-engineer\levels\07.json")

CORRECTIONS = {
    "probability >= 0.5`" : None,  # placeholder, real matching is on the fixture below
}

F1_EXPECTED = [{"precision_pct": 60.0, "recall_pct": 100.0, "f1": 0.75}]
COST_EXPECTED = [{"fp": 2, "total_cost": 10}]
MATRIX_EXPECTED = [{"tp": 3, "fp": 2, "fn": 0, "tn": 3}]


def main() -> int:
    data = json.loads(PATH.read_text(encoding="utf-8"))
    fixed = 0

    for question in data.get("questions") or []:
        spec = question.get("code") or {}
        prompt = question.get("prompt", "")
        if "three decimals" in prompt:
            expected, names = F1_EXPECTED, (
                "at 0.5 precision is 60% and recall 100%, and F1 lands at 0.75",
                "hidden: every genuine return clears 0.5, so recall is complete and F1 follows precision",
            )
        elif "false alarm costs 5" in prompt:
            expected, names = COST_EXPECTED, (
                "two of the five flags are false alarms, costing 10",
                "hidden: o8 clears 0.5 without being a return, so it is a false alarm",
            )
        elif "full confusion matrix at one operating point" in prompt:
            expected, names = MATRIX_EXPECTED, (
                "five orders clear 0.5: three true positives, two false alarms, no misses",
                "hidden: at this threshold no genuine return is missed, so fn is zero",
            )
        else:
            continue
        spec["tests"][0]["name"] = names[0]
        spec["tests"][1]["name"] = names[1]
        for test in spec["tests"]:
            test["expected"] = json.loads(json.dumps(expected))
            fixed += 1

    PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8", newline="\n")
    print(f"  corrected {fixed} expectation(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
