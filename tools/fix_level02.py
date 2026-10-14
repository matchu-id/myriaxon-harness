#!/usr/bin/env python3
"""Repair level 02: correct the group-count expectations, add a tiebreaker, and fix the encoding.

Three defects, all found by running the verifier rather than by reading:

  1. The expectation said `j.smith@mail.com` had 2 orders. The setup data puts `J.Smith@Mail.com` on
     o1, o2 and o4, so the lower-cased group holds 3 -- the same as ana. The verifier reported the
     mismatch and the reference query's actual output, which is what made the arithmetic error visible.
  2. With both customers on 3, `ORDER BY n DESC` leaves the row order undetermined. SQLite happened to
     return j.smith first. An expectation that depends on an unspecified order is not a test, so the
     question now requires a secondary sort key and the prompt says so.
  3. The file was rewritten through PowerShell as cp1252, turning the em-dash into three mojibake
     characters. Every write from here goes through Python with an explicit UTF-8 encoding for that
     reason.
"""

from __future__ import annotations

import json
from pathlib import Path

PATH = Path(r"C:\Users\arian\OneDrive\ドキュメント\Myriaxon\certivo-rework\ai-ml-engineer\levels\02.json")


def fix_mojibake(text: str) -> str:
    """Undo a cp1252 round-trip: 'â€”' is an em-dash that was encoded, then mis-decoded."""
    for bad, good in (
        ("â€”", "—"), ("â€™", "’"), ("â€œ", "“"), ("â€\x9d", "”"), ("â€“", "–"),
    ):
        text = text.replace(bad, good)
    return text


def main() -> int:
    raw = PATH.read_text(encoding="utf-8", errors="replace")
    cleaned = fix_mojibake(raw)
    data = json.loads(cleaned)

    fixed = 0
    for question in data.get("questions") or []:
        spec = question.get("code") or {}
        setup = spec.get("setupSql") or ""
        if "customer_email" not in setup:
            continue

        question["prompt"] = (
            "Write a query returning `customer_email` in lower case together with that customer's "
            "order count, for addresses with more than one order, most orders first and ties broken "
            "alphabetically by address. The editor below shows the schema and the tests."
        )
        question["explanation"] = (
            "LOWER collapses the case variants so `J.Smith@Mail.com` and `j.smith@mail.com` become one "
            "group — grouping on the raw column would count the same person twice. GROUP BY then forms "
            "the groups and HAVING filters them by size, which WHERE cannot do because it runs before "
            "grouping. The second sort key matters whenever two customers share a count: without it the "
            "query does not determine the row order, so the result is not reproducible."
        )
        spec["instructionsMarkdown"] = (
            "The table is `orders(order_id, customer_email)`.\n\n"
            "Return one row per customer containing the address in **lower case** as `customer_email`, "
            "and `n`, the number of orders placed by that customer. Include only customers with **more "
            "than 1** order. Order by `n` descending, and break ties by ordering `customer_email` "
            "ascending.\n\nPress **Run tests** to execute this query in your browser through sql.js. "
            "Nothing runs on the server."
        )
        # Both customers genuinely hold three orders each: o1, o2 and o4 for j.smith; o3, o5 and o6
        # for ana. Equal counts are the point of the tiebreaker.
        expected = [
            {"customer_email": "ana@mail.com", "n": 3},
            {"customer_email": "j.smith@mail.com", "n": 3},
        ]
        for test in spec.get("tests") or []:
            test["expected"] = json.loads(json.dumps(expected))
            fixed += 1
        spec["tests"][0]["name"] = "ana and j.smith each hold three orders; the tie breaks alphabetically"
        spec["tests"][1]["name"] = (
            "hidden: bo is dropped for having one order, and both case variants count as one customer"
        )

    # Any other mojibake in the file is repaired by the same pass over the whole document.
    text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    before = sum(1 for ch in text if ch in "â€”â€™")
    PATH.write_text(text, encoding="utf-8", newline="\n")

    print(f"  repaired {fixed} expectation(s)")
    print(f"  residual mojibake characters: {before}")
    print(f"  written as UTF-8: {PATH.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
