#!/usr/bin/env python3
"""Rewrite level 6's two update questions to use RETURNING, since two statements cannot run.

The browser runner prepares a single statement, and this verifier's `execute()` enforces the same limit --
`You can only execute one statement at a time`. So a question cannot ask for an update followed by a read;
it has to ask for one statement that updates and reports.

`UPDATE ... RETURNING` does exactly that, and it is the better question as a result: when the guard fails,
no row is returned at all, so the absence of a result *is* the signal that nothing was written. That is
closer to how the application would see it than a separate `changes()` call would be.
"""

from __future__ import annotations

import json
from pathlib import Path

PATH = Path(r"C:\Users\arian\OneDrive\ドキュメント\Myriaxon\certivo-rework\fullstack-developer\levels\06.json")

REWRITES = [
    {
        "marker": "the atomic stock decrement checkout needs",
        "prompt": "Write the guarded decrement checkout needs as one statement: reduce `stock` by 1 for the "
                  "product with `id` 1, only while there is stock left, and return the new value as "
                  "`stock_after`. The editor below shows the schema and the tests.",
        "explanation": "Putting the stock check in the `WHERE` clause makes the read and the write a single "
                       "statement, so no other transaction can slip between them — which is the "
                       "check-then-act race, solved by not separating the two. When the guard fails, "
                       "nothing is returned at all, so an empty result is the signal that the item had "
                       "already gone.",
        "instructions": "The table is `inventory(product_id, stock)`.\n\nIn **one statement**, decrement "
                        "`stock` by 1 for the row with `product_id = 1`, and only when its current `stock` "
                        "is greater than zero. Return the new value as `stock_after`.\n\nWhen the guard "
                        "fails, the statement should return no rows — that is how the caller learns "
                        "nothing was written.\n\nUse `UPDATE ... RETURNING`. Press **Run tests** to "
                        "execute this query in your browser through sql.js. Nothing runs on the server.",
        "starter": "-- inventory(product_id, stock)\n"
                   "-- Decrement only when stock > 0, and return the new value.\n",
        "setup": "CREATE TABLE inventory (product_id INTEGER PRIMARY KEY, stock INTEGER);\n"
                 "INSERT INTO inventory VALUES (1,3),(2,0);",
        "tests": [
            {"id": "t1", "name": "three units become two when stock is available",
             "args": [], "expected": []},
            {"id": "t2", "hidden": True,
             "name": "hidden: on an empty shelf the guard matches no row, so nothing is returned",
             "args": [], "expected": []},
        ],
    },
    {
        "marker": "restocking a returned item",
        "prompt": "Write a query restocking a returned item: increase `stock` by 2 for the product with "
                  "`id` 1, and return the new value as `stock_after` in the same statement. The editor "
                  "below shows the schema and the tests.",
        "explanation": "An increment needs no guard, because it cannot fail on the current value — which is "
                       "the contrast with the decrement, where the condition is the whole point. "
                       "`RETURNING` reports the new value from the same statement, so no second read is "
                       "needed and there is no window in which someone else could change it.",
        "instructions": "The table is `inventory(product_id, stock)`.\n\nIn **one statement**, increase "
                        "`stock` by 2 for the row with `product_id = 1`, and return the new value as "
                        "`stock_after`.\n\nUse `UPDATE ... RETURNING`. Press **Run tests** to execute "
                        "this query in your browser through sql.js. Nothing runs on the server.",
        "starter": "-- inventory(product_id, stock)\n"
                   "-- Add 2 to the stock and return the new value.\n",
        "setup": "CREATE TABLE inventory (product_id INTEGER PRIMARY KEY, stock INTEGER);\n"
                 "INSERT INTO inventory VALUES (1,5),(2,0);",
        "tests": [
            {"id": "t1", "name": "restocking two units takes the product from 5 to 7",
             "args": [], "expected": []},
            {"id": "t2", "hidden": True,
             "name": "hidden: the update is unconditional, so a product at zero would also be restocked",
             "args": [], "expected": []},
        ],
    },
]


def main() -> int:
    data = json.loads(PATH.read_text(encoding="utf-8"))
    replaced = 0

    for question in data["questions"]:
        spec = question.get("code")
        if not spec or spec.get("language") != "sql":
            continue
        for rewrite in REWRITES:
            if rewrite["marker"] not in question.get("prompt", ""):
                continue
            question["prompt"] = rewrite["prompt"]
            question["explanation"] = rewrite["explanation"]
            spec["instructionsMarkdown"] = rewrite["instructions"]
            spec["starterCode"] = rewrite["starter"]
            spec["setupSql"] = rewrite["setup"]
            spec["tests"] = rewrite["tests"]
            replaced += 1
            break

    PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8", newline="\n")
    print(f"  rewrote {replaced} SQL question(s) to use RETURNING")
    return 0 if replaced == 2 else 1


if __name__ == "__main__":
    raise SystemExit(main())
