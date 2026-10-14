#!/usr/bin/env python3
"""Fix level 12's caller-burst fixture and test names.

Two claims did not hold against the derived output:

  * The name said bot-9 "covers three seconds" where the wall-clock span between 09:00:00 and 09:00:03 is
    two whole seconds. The row is correct; the name overstated it by counting the endpoints rather than
    the interval between them.
  * The hidden test claimed "a caller that made one request has a span of zero", and the fixture had no
    such caller, so that boundary was never exercised -- the same shape as the level 16 threshold and the
    level 9 mutation flag. A third caller with a single request is added, which makes it real.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

PATH = Path(r"C:\Users\arian\OneDrive\ドキュメント\Myriaxon\certivo-rework\fullstack-developer\levels\12.json")

SETUP = (
    "CREATE TABLE requests (request_id TEXT, caller_id TEXT, created_at TEXT);\n"
    "INSERT INTO requests VALUES "
    "('r1','bot-9','2026-05-01T09:00:00'),"
    "('r2','bot-9','2026-05-01T09:00:03'),"
    "('r3','cust-1','2026-05-01T09:05:00'),"
    "('r4','cust-1','2026-05-01T09:35:00'),"
    "('r5','cust-9','2026-05-01T09:10:00');"
)

REFERENCE = (
    "SELECT caller_id, MIN(created_at) AS first_at, MAX(created_at) AS last_at, "
    "CAST((julianday(MAX(created_at)) - julianday(MIN(created_at))) * 86400 AS INTEGER) "
    "AS span_seconds FROM requests GROUP BY caller_id "
    "ORDER BY span_seconds ASC, caller_id ASC"
)


def main() -> int:
    data = json.loads(PATH.read_text(encoding="utf-8"))
    target = next(q for q in data["questions"]
                  if "the first and last request in its window" in q.get("prompt", "")
                  and "code" in q)

    target["code"]["setupSql"] = SETUP

    connection = sqlite3.connect(":memory:")
    connection.executescript(SETUP)
    cursor = connection.execute(REFERENCE)
    columns = [d[0] for d in cursor.description]
    rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
    connection.close()

    print(f"  reference returns: {rows}")
    zero_span = [r for r in rows if r["span_seconds"] == 0]
    if not zero_span:
        print("  REFUSING to write: no caller has a zero span, so the boundary test name would be false")
        return 1

    tests = target["code"]["tests"]
    tests[0]["name"] = "bot-9 covers two seconds where cust-1 covers thirty minutes"
    tests[1]["name"] = "hidden: cust-9 made a single request, so its span is exactly zero"
    for test in tests:
        test["expected"] = json.loads(json.dumps(rows))

    PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8", newline="\n")
    print(f"  wrote into {len(tests)} test(s); a single-request caller now exists")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
