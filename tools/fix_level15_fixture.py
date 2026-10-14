#!/usr/bin/env python3
"""Rebuild level 15's slow-query fixture so it actually demonstrates the point its name claims.

The question is about ordering remediation work by **total** time rather than by the worst single run, and
the name said product-by-slug leads across three runs ahead of one 4000 ms run. The reference disagreed:
3 x 1000 ms is 3000, which loses to 4000. The name was wrong, and so was the fixture -- with only three
samples, the frequent-but-modest pattern cannot out-total a single disaster, so the question demonstrated
the opposite of what it asks about.

The fixture now has twelve recorded runs of product-by-slug against one of order-history, which is the
situation the ordering rule exists for: the pattern that is individually unremarkable is the one worth
fixing, and only the total shows it.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

PATH = Path(r"C:\Users\arian\OneDrive\ドキュメント\Myriaxon\certivo-rework\fullstack-developer\levels\15.json")

ROWS = [
    ("product-by-slug", 900),
    ("order-history", 4000),
    ("search-products", 250),
    ("search-products", 300),
]
ROWS += [("product-by-slug", 1000)] * 11

SETUP = (
    "CREATE TABLE slow_queries (pattern TEXT, duration_ms INTEGER);\n"
    "INSERT INTO slow_queries VALUES "
    + ",".join(f"('{pattern}',{duration})" for pattern, duration in ROWS)
    + ";"
)

REFERENCE = (
    "SELECT pattern, COUNT(*) AS n, SUM(duration_ms) AS total_ms FROM slow_queries "
    "GROUP BY pattern ORDER BY total_ms DESC, pattern ASC"
)


def main() -> int:
    data = json.loads(PATH.read_text(encoding="utf-8"))
    target = next(q for q in data["questions"]
                  if "total time spent per query pattern" in q.get("prompt", "") and "code" in q)

    target["code"]["setupSql"] = SETUP

    connection = sqlite3.connect(":memory:")
    connection.executescript(SETUP)
    cursor = connection.execute(REFERENCE)
    columns = [d[0] for d in cursor.description]
    rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
    connection.close()

    for row in rows:
        print(f"  {row['pattern']:<18} n={row['n']:<3} total={row['total_ms']}")

    if rows[0]["pattern"] != "product-by-slug":
        print(f"  REFUSING to write: {rows[0]['pattern']} still leads, so the question's point is not shown")
        return 1

    tests = target["code"]["tests"]
    tests[0]["name"] = (
        f"product-by-slug leads on total time with {rows[0]['total_ms']}ms across "
        f"{rows[0]['n']} runs, ahead of one 4000ms run"
    )
    tests[1]["name"] = "hidden: search-products is cheapest in total despite being recorded twice"
    for test in tests:
        test["expected"] = json.loads(json.dumps(rows))

    PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8", newline="\n")
    print(f"  wrote; the frequent pattern now out-totals the single slow one, {len(tests)} test(s) updated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
