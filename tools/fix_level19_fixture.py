#!/usr/bin/env python3
"""Correct level 19's API-usage fixture and test name.

The name said v2 leads with three requests from two callers, while v1 has three from two. That is a tie, so
the `version ASC` tiebreak puts **v1** first -- the name described an ordering the query would not produce.

The fixture is what should change here rather than the name: a version on its way out being *tied* with its
replacement says nothing, whereas one that has clearly overtaken it is the state in which a sunset date is
worth trusting. v2 now has a fourth call from a third partner, so it leads on requests and on callers, and
the name is generated from the derived rows rather than written beside them.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

PATH = Path(r"C:\Users\arian\OneDrive\ドキュメント\Myriaxon\certivo-rework\fullstack-developer\levels\19.json")

SETUP = (
    "CREATE TABLE api_calls (version TEXT, caller_id TEXT, called_at TEXT);\n"
    "INSERT INTO api_calls VALUES "
    "('v1','partner-a','2026-07-01T10:00:00'),"
    "('v1','partner-a','2026-07-01T11:00:00'),"
    "('v1','partner-b','2026-07-02T09:00:00'),"
    "('v2','partner-b','2026-07-02T10:00:00'),"
    "('v2','partner-c','2026-07-02T11:00:00'),"
    "('v2','partner-c','2026-07-02T12:00:00'),"
    "('v2','partner-d','2026-07-02T13:00:00'),"
    "('v1','partner-a','2026-06-30T23:00:00');"
)

QUERY = (
    "SELECT version, COUNT(DISTINCT caller_id) AS callers, COUNT(*) AS requests "
    "FROM api_calls WHERE called_at >= ? "
    "GROUP BY version ORDER BY requests DESC, version ASC"
)


def main() -> int:
    data = json.loads(PATH.read_text(encoding="utf-8"))
    target = next(q for q in data["questions"]
                  if "each API version's caller count and total requests" in q.get("prompt", ""))

    target["code"]["setupSql"] = SETUP

    cutoff = target["code"]["tests"][0]["args"][0]
    connection = sqlite3.connect(":memory:")
    connection.executescript(SETUP)
    cursor = connection.execute(QUERY, (cutoff,))
    columns = [d[0] for d in cursor.description]
    rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
    connection.close()

    for row in rows:
        print(f"  {row['version']}: callers={row['callers']} requests={row['requests']}")

    if not (rows[0]["requests"] > rows[1]["requests"] and rows[0]["callers"] > rows[1]["callers"]):
        print("  REFUSING to write: the fixture still ties, so the name would describe a wrong ordering")
        return 1

    target["code"]["tests"][0]["name"] = (
        f"{rows[0]['version']} leads with {rows[0]['requests']} requests from {rows[0]['callers']} callers, "
        f"while {rows[1]['version']} has {rows[1]['requests']} from {rows[1]['callers']}"
    )
    for test in target["code"]["tests"]:
        test["expected"] = json.loads(json.dumps(rows))

    PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8", newline="\n")
    print("  name generated from the derived rows; v2 now leads on both counts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
