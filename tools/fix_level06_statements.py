#!/usr/bin/env python3
"""Rewrite level 6's three SQL questions as single statements.

The browser runner does this:

    db.run(setupSql);
    const stmt = db.prepare(code);   // <- one statement, prepared
    while (stmt.step()) rows.push(stmt.getAsObject());

`prepare` compiles a single statement, so a learner cannot write `BEGIN; UPDATE ...; ROLLBACK;` and have
it run -- the whole script would be handed to a compiler expecting one statement. My first version of this
level asked for exactly that, which would have failed for every learner while passing in my own verifier,
because the verifier uses Python's `sqlite3` and can execute a script.

That is the worst shape of bug: a question that works in the tool and not in the product. The three
questions now ask for the single-statement operations that make the transaction safe -- which is also
closer to what a learner actually writes, since the transaction block belongs to application code and the
guard belongs in the statement.
"""

from __future__ import annotations

import json
from pathlib import Path

PATH = Path(r"C:\Users\arian\OneDrive\ドキュメント\Myriaxon\certivo-rework\fullstack-developer\levels\06.json")

REPLACEMENTS = [
    {
        "marker": "decrement `stock` by 1 for the product with `id` 1, but only while there is stock",
        "prompt": "Write the guarded decrement checkout needs as a single statement: reduce `stock` by 1 "
                  "for the product with `id` 1, only while there is stock left, then return `changes()` "
                  "as `rows_changed`. The editor below shows the schema and the tests.",
        "explanation": "Putting the stock check in the `WHERE` clause makes the read and the write one "
                       "statement, so no other transaction can slip between them — which is the "
                       "check-then-act race, solved by not separating the two. `changes()` then reports "
                       "whether the decrement happened: a zero means the item had already gone.",
        "instructions": "The table is `inventory(product_id, stock)`.\n\nIn a **single statement "
                        "batch**, decrement `stock` by 1 for the row with `product_id = 1`, and only when "
                        "its current `stock` is greater than zero. Then return `changes()` as "
                        "`rows_changed`.\n\nA result of `1` means the decrement happened; `0` means the "
                        "product had no stock and nothing was written.\n\nPress **Run tests** to execute "
                        "this query in your browser through sql.js. Nothing runs on the server.",
        "starter": "-- inventory(product_id, stock)\n"
                   "-- Decrement stock only when stock > 0, then report how many rows changed.\n",
        "setup": "CREATE TABLE inventory (product_id INTEGER PRIMARY KEY, stock INTEGER);\n"
                 "INSERT INTO inventory VALUES (1,3),(2,0);",
        "tests": [
            {"id": "t1",
             "name": "one row changes when stock is available",
             "args": [], "expected": []},
            {"id": "t2", "hidden": True,
             "name": "hidden: the guard sits in the WHERE clause, so the check and the write are one statement",
             "args": [], "expected": []},
        ],
    },
    {
        "marker": "what a rolled-back checkout leaves behind",
        "prompt": "Write a query reporting the outcome of an oversold attempt: return `stock_before`, the "
                  "current `stock`, and `would_decrement` \u2014 `1` when a single unit could still be taken, "
                  "else `0` \u2014 for the product with `id` 1. The editor below shows the schema and the tests.",
        "explanation": "This is the condition the guarded update relies on, expressed as a read. Reporting "
                       "both the value and the verdict is what makes an empty-shelf case visible rather "
                       "than silent: a zero here is the same zero the `WHERE` clause would produce, so the "
                       "two agree instead of being written twice.",
        "instructions": "The table is `inventory(product_id, stock)`.\n\nReturn a single row for the "
                        "product with `product_id = 1`, with three columns:\n\n- `stock_before` \u2014 the "
                        "current stock value\n- `stock` \u2014 the same value, read a second time\n- "
                        "`would_decrement` \u2014 `1` when `stock` is greater than zero, otherwise `0`\n\n"
                        "Press **Run tests** to execute this query in your browser through sql.js. "
                        "Nothing runs on the server.",
        "starter": "-- inventory(product_id, stock)\n"
                   "-- Report the stock and whether a unit could still be taken.\n",
        "setup": "CREATE TABLE inventory (product_id INTEGER PRIMARY KEY, stock INTEGER);\n"
                 "INSERT INTO inventory VALUES (1,1),(2,0);",
        "tests": [
            {"id": "t1",
             "name": "the last unit is still available, so the guard reports 1",
             "args": [], "expected": []},
            {"id": "t2", "hidden": True,
             "name": "hidden: a product at zero would report 0, which is the oversold case",
             "args": [], "expected": []},
        ],
    },
    {
        "marker": "invisible to another reader",
        "prompt": "Write a query restocking a returned item: increase `stock` by 2 for the product with "
                  "`id` 1, then return the new `stock` as `stock_after` in the same batch. The editor "
                  "below shows the schema and the tests.",
        "explanation": "An increment needs no guard because it cannot fail on the current value \u2014 which "
                       "is the contrast with the decrement, where the condition is the whole point. "
                       "Reading the new value back in the same batch is what confirms the write took "
                       "effect rather than assuming it did.",
        "instructions": "The table is `inventory(product_id, stock)`.\n\nIn a **single statement batch**, "
                        "increase `stock` by 2 for the row with `product_id = 1`, then return that "
                        "product's new `stock` as `stock_after`.\n\nPress **Run tests** to execute this "
                        "query in your browser through sql.js. Nothing runs on the server.",
        "starter": "-- inventory(product_id, stock)\n"
                   "-- Add 2 to the stock and report the new value.\n",
        "setup": "CREATE TABLE inventory (product_id INTEGER PRIMARY KEY, stock INTEGER);\n"
                 "INSERT INTO inventory VALUES (1,5),(2,0);",
        "tests": [
            {"id": "t1",
             "name": "restocking two units takes the product from 5 to 7",
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
        for replacement in REPLACEMENTS:
            if replacement["marker"] not in question.get("prompt", ""):
                continue
            question["prompt"] = replacement["prompt"]
            question["explanation"] = replacement["explanation"]
            spec["instructionsMarkdown"] = replacement["instructions"]
            spec["starterCode"] = replacement["starter"]
            spec["setupSql"] = replacement["setup"]
            spec["tests"] = replacement["tests"]
            replaced += 1
            break

    PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8", newline="\n")
    print(f"  rewrote {replaced} SQL question(s) as single statements")
    return 0 if replaced == 3 else 1


if __name__ == "__main__":
    raise SystemExit(main())
