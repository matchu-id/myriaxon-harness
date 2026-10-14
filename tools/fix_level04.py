#!/usr/bin/env python3
"""Correct the category-stats expectations in level 04.

I wrote `[{tents,2,150}, {boots,1,50}]` twice, but the question says "include only categories with at
least 2 valid orders" and boots has exactly one valid order -- its other row is the -1 sentinel. So boots
must be absent. The verifier reported the reference query's actual output, which is what exposed it.

Authoring both the test name and the expectation from the same wrong mental model is exactly why the
expectation has to be produced by running the query rather than reasoned about.
"""

from __future__ import annotations

import json
from pathlib import Path

PATH = Path(r"C:\Users\arian\OneDrive\ドキュメント\Myriaxon\certivo-rework\ai-ml-engineer\levels\04.json")
CORRECT = [{"category": "tents", "n": 2, "avg_value": 150.0}]


def main() -> int:
    data = json.loads(PATH.read_text(encoding="utf-8"))
    fixed = 0

    for question in data.get("questions") or []:
        spec = question.get("code") or {}
        # Select on the fixture, not on the starter code. An earlier version of this script matched
        # `"avg_value" in starterCode`, which was false for this very question -- the starter code only
        # mentions `n` -- so it silently corrected nothing and reported success.
        if "'tents',100" not in (spec.get("setupSql") or ""):
            continue
        spec["tests"][0]["name"] = "tents averages 150 over two valid orders; boots is excluded for having one"
        spec["tests"][1]["name"] = "hidden: a category whose only remaining row is the -1 sentinel is dropped"
        for test in spec["tests"]:
            test["expected"] = json.loads(json.dumps(CORRECT))
            fixed += 1

    PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8", newline="\n")
    print(f"  corrected {fixed} expectation(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
