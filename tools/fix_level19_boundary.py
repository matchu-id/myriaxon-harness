#!/usr/bin/env python3
"""Give level 19's review-cadence fixture the boundary its hidden test claims.

The hidden test said "a model at exactly 60 days is not overdue". The fixture's ages were 107, 90, 12 and
7, so no model sat on the boundary and a learner writing `>=` would have passed. This is the tenth level
where a name asserted something the fixture did not contain, and like the level 16 case it left a required
behaviour untested rather than merely describing it wrongly.

Adding a model reviewed exactly 60 days before 2026-04-01 -- which is 2026-02-01 -- exercises the strict
comparison directly.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

PATH = Path(r"C:\Users\arian\OneDrive\ドキュメント\Myriaxon\certivo-rework\ai-ml-engineer\levels\19.json")

SETUP = (
    "CREATE TABLE reviews (model_name TEXT, last_reviewed TEXT);\n"
    "INSERT INTO reviews VALUES ('returns','2026-03-20'),('churn','2026-03-25'),"
    "('demand','2025-12-15'),('routing','2026-01-01'),('pricing','2026-01-31');"
)

REFERENCE = (
    "SELECT model_name, "
    "CAST(julianday('2026-04-01') - julianday(last_reviewed) AS INTEGER) AS days_since_review, "
    "CASE WHEN CAST(julianday('2026-04-01') - julianday(last_reviewed) AS INTEGER) > ? "
    "THEN 1 ELSE 0 END AS overdue FROM reviews "
    "ORDER BY days_since_review DESC, model_name ASC"
)


def main() -> int:
    data = json.loads(PATH.read_text(encoding="utf-8"))
    target = next(q for q in data["questions"]
                  if "comparing model review ages against a policy" in q.get("prompt", "")
                  and "code" in q)

    target["code"]["setupSql"] = SETUP
    threshold = target["code"]["tests"][0].get("args") or [60]

    connection = sqlite3.connect(":memory:")
    connection.executescript(SETUP)
    cursor = connection.execute(REFERENCE, threshold)
    columns = [d[0] for d in cursor.description]
    rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
    connection.close()

    print(f"  reference returns: {rows}")
    on_boundary = [r for r in rows if r["days_since_review"] == int(threshold[0])]
    if not on_boundary:
        print(f"  REFUSING to write: no model sits at exactly {threshold[0]} days, so the test name "
              f"claiming the boundary is checked would still be false")
        return 1
    if any(r["overdue"] for r in on_boundary):
        print(f"  REFUSING to write: a model at exactly {threshold[0]} days is overdue, so the "
              f"comparison is not strict")
        return 1

    tests = target["code"]["tests"]
    tests[0]["name"] = "demand at 107 days and routing at 90 are overdue against a 60-day cadence"
    tests[1]["name"] = "hidden: pricing sits at exactly 60 days and is not overdue, so the rule is strict"
    for test in tests:
        test["expected"] = json.loads(json.dumps(rows))

    PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8", newline="\n")
    print(f"  wrote into {len(tests)} test(s); a model now sits exactly on the boundary")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
