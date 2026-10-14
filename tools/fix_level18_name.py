#!/usr/bin/env python3
"""Correct level 18's dependency-share test name.

The name said r3 leads at 95% and r1 qualifies at 90%. The reference returns r1 at 90% and r3 at 86%, in
that order, so both halves were wrong: r3 does not lead, and its share is 86 rather than 95.

The 95 figure came from dividing r3's dependency span by the wrong total -- 950 of 1000, which excludes the
100 ms reviews span. The whole point of the question is that the share is measured against the request's
total time, so the name was making exactly the mistake the question tests for. The other hidden test name on
this question already said so correctly; this one now agrees with it.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

PATH = Path(r"C:\Users\arian\OneDrive\ドキュメント\Myriaxon\certivo-rework\fullstack-developer\levels\18.json")

QUERY = (
    "SELECT request_id, SUM(duration_ms) AS total_ms, "
    "SUM(CASE WHEN is_non_critical = 1 THEN duration_ms ELSE 0 END) AS slow_dep_ms, "
    "CAST(100 * SUM(CASE WHEN is_non_critical = 1 THEN duration_ms ELSE 0 END) "
    "/ SUM(duration_ms) AS INTEGER) AS dep_share_pct "
    "FROM spans GROUP BY request_id HAVING dep_share_pct >= 70 "
    "ORDER BY dep_share_pct DESC, request_id ASC"
)


def main() -> int:
    data = json.loads(PATH.read_text(encoding="utf-8"))
    target = next(q for q in data["questions"]
                  if "total time was spent almost entirely in the recommendations service"
                  in q.get("prompt", ""))

    connection = sqlite3.connect(":memory:")
    connection.executescript(target["code"]["setupSql"])
    cursor = connection.execute(QUERY)
    columns = [d[0] for d in cursor.description]
    rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
    connection.close()

    for row in rows:
        print(f"  {row['request_id']}: total={row['total_ms']} dep={row['slow_dep_ms']} "
              f"share={row['dep_share_pct']}%")

    leader, second = rows[0], rows[1]
    excluded = "r2"
    target["code"]["tests"][0]["name"] = (
        f"{leader['request_id']} leads at {leader['dep_share_pct']}% and "
        f"{second['request_id']} qualifies at {second['dep_share_pct']}%, "
        f"while {excluded} is excluded"
    )
    for test in target["code"]["tests"]:
        test["expected"] = json.loads(json.dumps(rows))

    PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8", newline="\n")
    print(f"  name now built from the derived rows, not typed beside them")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
