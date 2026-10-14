#!/usr/bin/env python3
"""Correct level 08's combination-effect expectations.

The question keeps only category and price-band pairs with **at least 3 orders**. The fixture gives
jackets/high 3, jackets/low 3, boots/high 3, and boots/low only 2 — so boots/low must be absent. I wrote
all four rows again, contradicting the rule stated in the same question.

That is now the fourth level in a row where a size filter was written into the prompt and then ignored in
the expectation. The pattern is consistent enough to name: I compose the fixture first and the rule second,
so the expectation follows the fixture's shape rather than the rule's. Running the reference query is the
only step that reliably catches it, which is why no `expected` value is ever typed by hand any more.
"""

from __future__ import annotations

import json
from pathlib import Path

PATH = Path(r"C:\Users\arian\OneDrive\ドキュメント\Myriaxon\certivo-rework\ai-ml-engineer\levels\08.json")

CORRECT = [
    {"category": "jackets", "price_band": "high", "n": 3, "return_pct": 100.0},
    {"category": "boots", "price_band": "high", "n": 3, "return_pct": 33.33},
    {"category": "jackets", "price_band": "low", "n": 3, "return_pct": 0.0},
]


def main() -> int:
    data = json.loads(PATH.read_text(encoding="utf-8"))
    fixed = 0

    for question in data.get("questions") or []:
        spec = question.get("code") or {}
        if "price_band" not in (spec.get("setupSql") or ""):
            continue
        spec["tests"][0]["name"] = (
            "high jackets return at 100% against high boots at 33.33%; both low bands are at zero"
        )
        spec["tests"][1]["name"] = (
            "hidden: boots in the low band has two orders and is dropped by the size rule"
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
