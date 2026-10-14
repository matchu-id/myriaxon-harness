#!/usr/bin/env python3
"""Restore the NULL-email code question in level 02, which a too-broad repair script overwrote.

`fix_level02.py` selected questions with `if "customer_email" not in setupSql: continue`. Both email
questions contain that column name, so it rewrote the NULL question into a second copy of the group-count
question -- which is why the file now holds the same prompt twice and the verifier maps both to the same
reference. The lesson is in the selector: match the DISTINCTIVE part of a question (its NULL test), not a
column name it happens to share with another.
"""

from __future__ import annotations

import json
from pathlib import Path

PATH = Path(r"C:\Users\arian\OneDrive\ドキュメント\Myriaxon\certivo-rework\ai-ml-engineer\levels\02.json")

SETUP = (
    "CREATE TABLE orders (order_id TEXT, customer_email TEXT, placed_at TEXT);\n"
    "INSERT INTO orders VALUES ('o1','a@mail.com','2026-01-05'), ('o2',NULL,'2026-01-09'), "
    "('o3','b@mail.com','2026-01-02'), ('o4',NULL,'2026-01-12');"
)


def main() -> int:
    data = json.loads(PATH.read_text(encoding="utf-8"))
    restored = 0

    for question in data.get("questions") or []:
        spec = question.get("code") or {}
        setup = spec.get("setupSql") or ""
        # The distinctive marker: this question is the one with NULL emails in its fixture. Matching on
        # `customer_email` alone caught both email questions, which is the bug being repaired.
        if "NULL" not in setup or "placed_at" not in setup:
            continue

        question["skill"] = "data-preparation"
        question["prompt"] = (
            "Write a query that returns `order_id` for every order whose `customer_email` is NULL, "
            "most recent order first. The editor below shows the schema and the tests."
        )
        question["explanation"] = (
            "`IS NULL` is the only comparison that finds a missing value, because NULL is not equal to "
            "anything including itself — `= NULL` matches no rows and returns an empty result rather "
            "than an error, which is a silent wrong answer rather than a loud one. ORDER BY placed_at "
            "DESC puts the most recent order first."
        )
        spec["language"] = "sql"
        spec["starterCode"] = "-- orders(order_id, customer_email, placed_at)\nSELECT order_id FROM orders;\n"
        spec["instructionsMarkdown"] = (
            "The table is `orders(order_id, customer_email, placed_at)`.\n\n"
            "Return the `order_id` of every order where `customer_email` **is NULL**, ordered by "
            "`placed_at` descending (most recent first).\n\n"
            "Press **Run tests** to execute this query in your browser through sql.js. Nothing runs on "
            "the server."
        )
        spec["timeoutMs"] = 5000
        spec["setupSql"] = SETUP
        spec["tests"] = [
            {
                "id": "t1",
                "name": "the two orders with no email, newest first",
                "args": [],
                "expected": [{"order_id": "o4"}, {"order_id": "o2"}],
            },
            {
                "id": "t2",
                "name": "hidden: writing = NULL instead of IS NULL matches nothing and fails here",
                "hidden": True,
                "args": [],
                "expected": [{"order_id": "o4"}, {"order_id": "o2"}],
            },
        ]
        restored += 1

    PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8", newline="\n")
    print(f"  restored {restored} NULL-email question(s)")

    prompts: dict[str, int] = {}
    for question in data.get("questions") or []:
        if "code" in question:
            prompts[question["prompt"][:60]] = prompts.get(question["prompt"][:60], 0) + 1
    duplicates = {k: v for k, v in prompts.items() if v > 1}
    print(f"  duplicate code prompts remaining: {duplicates or 'none'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
