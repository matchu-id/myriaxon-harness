#!/usr/bin/env python3
"""Fix level 13's entirely-NULL-column question, where the fixture did not contain the case it teaches.

The question asks for feature columns that are entirely NULL, and the fixture was:

    ('o1',120.0,3), ('o2',80.0,NULL), ('o3',45.0,NULL), ('o4',200.0,7)

`num_past_orders` holds 3 and 7 in two of four rows, so NO column is entirely NULL and the correct answer
is the empty set. The test names I wrote claimed the opposite ("num_past_orders is entirely NULL"),
which is how the contradiction was visible once the output was printed rather than reasoned about.

Worse, an empty result would make the question untestable: a learner who returns nothing at all would
pass. So the fixture is corrected to contain a genuinely empty column, which is what the question is for,
and the expectations are derived from the reference query rather than written.

The reference form is a wrapper over a UNION of per-column checks, because this SQLite does not answer
`HAVING` without a GROUP BY the way it reads -- `SELECT ... FROM features HAVING COUNT(col) = 0` returns
no rows even when the column is entirely NULL. The wrapper form works: aggregate each column, then filter
the results.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

PATH = Path(r"C:\Users\arian\OneDrive\ドキュメント\Myriaxon\certivo-rework\ai-ml-engineer\levels\13.json")

# `shipping_zone` is now NULL in every row, which is the failure this question exists to catch: a join or
# a renamed source column that leaves a feature silently empty while the pipeline runs to completion.
SETUP = (
    "CREATE TABLE features (order_id TEXT, order_value REAL, shipping_zone TEXT);\n"
    "INSERT INTO features VALUES ('o1',120.0,NULL),('o2',80.0,NULL),('o3',45.0,NULL),('o4',200.0,NULL);"
)

REFERENCE = (
    "SELECT feature_name, n_rows FROM ("
    "SELECT 'order_value' AS feature_name, COUNT(*) AS n_rows, "
    "SUM(CASE WHEN order_value IS NULL THEN 1 ELSE 0 END) AS n_null FROM features "
    "UNION ALL "
    "SELECT 'shipping_zone', COUNT(*), SUM(CASE WHEN shipping_zone IS NULL THEN 1 ELSE 0 END) "
    "FROM features) WHERE n_null = n_rows ORDER BY feature_name ASC"
)

INSTRUCTIONS = (
    "The table is `features(order_id, order_value, shipping_zone)`.\n\n"
    "Return one row for each of the three feature columns that is **entirely NULL** (every row), giving "
    "`feature_name` (the column's name as text) and `n_rows` (how many rows the table has). If no column "
    "is entirely NULL, return no rows.\n\n"
    "Order by `feature_name` ascending.\n\n"
    "Press **Run tests** to execute this query in your browser through sql.js. Nothing runs on the server."
)


def main() -> int:
    data = json.loads(PATH.read_text(encoding="utf-8"))

    target = next(q for q in data["questions"]
                  if "entirely NULL" in q.get("prompt", "") and "code" in q)

    target["code"]["setupSql"] = SETUP
    target["code"]["instructionsMarkdown"] = INSTRUCTIONS
    target["code"]["starterCode"] = (
        "-- features(order_id, order_value, shipping_zone)\n"
        "SELECT COUNT(*) AS n_rows FROM features;\n"
    )

    connection = sqlite3.connect(":memory:")
    connection.executescript(SETUP)
    cursor = connection.execute(REFERENCE)
    columns = [d[0] for d in cursor.description]
    rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
    connection.close()

    print(f"  reference returns {len(rows)} row(s): {rows}")
    if not rows:
        print("  REFUSING to write: the fixture still contains no entirely-NULL column, so the question "
              "would be satisfied by returning nothing")
        return 1

    tests = target["code"]["tests"]
    tests[0]["name"] = "shipping_zone is NULL in every row, which is what an empty feature looks like"
    tests[1]["name"] = "hidden: order_value is populated, so a column of nulls is reported and not the rest"
    for test in tests:
        test["expected"] = json.loads(json.dumps(rows))

    PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8", newline="\n")
    print(f"  wrote into {len(tests)} test(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
