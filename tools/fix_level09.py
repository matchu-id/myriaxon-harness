#!/usr/bin/env python3
"""Correct level 09's fold expectations.

Both fold questions carry their own worked example in the test name, and both names contradicted the
fixture. I had written that the positives cluster in folds 3-5, giving a tidy picture where three folds
hold one return each. The reference query returns something more uneven and more useful:

  q21 return_pct per fold : 0.00, 66.67, 33.33, 0.00, 0.00
  q24 n_pos per fold      : 0, 0, 1, 2, 0

Two of the five folds hold no positive case at all, one holds two, and the rates range from 0% to 66.67%
against a true base rate of 20%. That is a sharper demonstration of why an unstratified split misleads
than the balanced-looking arrangement I had described, and the test names now say what the data shows.

Note the two fixtures differ by one row value, so their distributions differ too -- q21 has returns at
o03, o07, o12 and q24 at o03, o04, o09. Reading a name off one and applying it to the other is part of
how this happened.
"""

from __future__ import annotations

import json
from pathlib import Path

PATH = Path(r"C:\Users\arian\OneDrive\ドキュメント\Myriaxon\certivo-rework\ai-ml-engineer\levels\09.json")

Q21 = [
    {"fold": 1, "n": 3, "return_pct": 0.0},
    {"fold": 2, "n": 3, "return_pct": 66.67},
    {"fold": 3, "n": 3, "return_pct": 33.33},
    {"fold": 4, "n": 3, "return_pct": 0.0},
    {"fold": 5, "n": 3, "return_pct": 0.0},
]
Q24 = [
    {"fold": 1, "n_pos": 0, "n_neg": 3},
    {"fold": 2, "n_pos": 0, "n_neg": 3},
    {"fold": 3, "n_pos": 1, "n_neg": 2},
    {"fold": 4, "n_pos": 2, "n_neg": 1},
    {"fold": 5, "n_pos": 0, "n_neg": 3},
]


def main() -> int:
    data = json.loads(PATH.read_text(encoding="utf-8"))
    fixed = 0

    for question in data.get("questions") or []:
        spec = question.get("code") or {}
        prompt = question.get("prompt", "")
        if "class balance of each fold can be inspected" in prompt:
            expected, names = Q21, (
                "fold rates run 0%, 66.67%, 33.33%, 0%, 0% against a true base rate of 20%",
                "hidden: three folds hold no return at all, so their scores measure the split, not the model",
            )
        elif "why an unstratified split is a problem" in prompt:
            expected, names = Q24, (
                "the three positives land two in fold 4 and one in fold 3, leaving three folds empty",
                "hidden: two of the five folds contain no positive case whatsoever",
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
