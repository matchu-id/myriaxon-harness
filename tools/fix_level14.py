#!/usr/bin/env python3
"""Correct level 14's provenance expectations, which I wrote from the fixture's shape rather than its dates.

I assumed `orders` had both a missing and a stale column. Checking the fixture against the rule:

    orders  order_value   2026-02-20   fresh
    orders  category      2026-01-05   fresh  (after the 2025-12-02 cutoff)
    orders  legacy_code   NULL         missing
    crm     lifetime_orders 2025-08-11 stale  (before the cutoff)
    crm     email_hash    2026-02-28   fresh

So only `crm` has anything stale, and `orders` has one missing column and no stale ones. My test name
asserted the opposite ("orders has one stale and one never refreshed"), which is the same failure mode as
the last four levels: a claim written about the fixture rather than derived from it.

The corrected figures also make the question teach its own point better — the two defect kinds now fall on
different source systems, which is exactly why they are counted separately.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

PATH = Path(r"C:\Users\arian\OneDrive\ドキュメント\Myriaxon\certivo-rework\ai-ml-engineer\levels\14.json")

REFERENCE = (
    "SELECT source_system, COUNT(*) AS n_columns, "
    "SUM(CASE WHEN last_refresh IS NULL THEN 1 ELSE 0 END) AS n_missing, "
    "SUM(CASE WHEN last_refresh IS NOT NULL AND last_refresh < '2025-12-02' THEN 1 ELSE 0 END) "
    "AS n_stale FROM provenance GROUP BY source_system ORDER BY source_system ASC"
)


def main() -> int:
    data = json.loads(PATH.read_text(encoding="utf-8"))
    target = next(q for q in data["questions"]
                  if "provenance inventory" in q.get("prompt", "") and "code" in q)

    connection = sqlite3.connect(":memory:")
    connection.executescript(target["code"]["setupSql"])
    cursor = connection.execute(REFERENCE)
    columns = [d[0] for d in cursor.description]
    rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
    connection.close()

    print(f"  reference returns: {rows}")

    tests = target["code"]["tests"]
    tests[0]["name"] = "crm's lifetime_orders is stale; orders has a never-refreshed column and none stale"
    tests[1]["name"] = "hidden: a NULL refresh counts as missing, and category's January date is not stale"
    for test in tests:
        test["expected"] = json.loads(json.dumps(rows))

    PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8", newline="\n")
    print(f"  wrote into {len(tests)} test(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
