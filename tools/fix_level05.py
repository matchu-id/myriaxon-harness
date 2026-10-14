#!/usr/bin/env python3
"""Correct level 05's threshold-sweep expectations, which I computed by hand and got wrong.

The sweep fixture is 8 orders with 3 genuine returns (o1 at 0.90, o2 at 0.80, o4 at 0.50). Hand-written
expectations said 6/4/3 flagged and recall 75/75/50; the reference query returns 7/5/3 with recall
100/100/67. Every figure in the original table was wrong, including the counts.

The corrected numbers demonstrate the tradeoff more sharply than the invented ones did: recall holds at
100% until the threshold passes the last genuine return, then falls to 67%, while precision climbs from
43% to 67%. That is exactly the mechanism the question exists to show, which is a good reminder that the
expectation has to come from running the query -- a plausible-looking table can be both wrong and duller
than the truth.
"""

from __future__ import annotations

import json
from pathlib import Path

PATH = Path(r"C:\Users\arian\OneDrive\ドキュメント\Myriaxon\certivo-rework\ai-ml-engineer\levels\05.json")

CORRECT = [
    {"threshold": 0.2, "n_flagged": 7, "precision_pct": 42.86, "recall_pct": 100.0},
    {"threshold": 0.5, "n_flagged": 5, "precision_pct": 60.0, "recall_pct": 100.0},
    {"threshold": 0.8, "n_flagged": 3, "precision_pct": 66.67, "recall_pct": 66.67},
]


def main() -> int:
    data = json.loads(PATH.read_text(encoding="utf-8"))
    fixed = 0

    for question in data.get("questions") or []:
        spec = question.get("code") or {}
        if "scores" not in (spec.get("setupSql") or ""):
            continue
        spec["tests"][0]["name"] = (
            "at 0.2 recall is 100% and precision 42.86%; by 0.8 recall has fallen to 66.67% "
            "while precision has risen"
        )
        spec["tests"][1]["name"] = (
            "hidden: raising the threshold trades recall away for precision, never both at once"
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
