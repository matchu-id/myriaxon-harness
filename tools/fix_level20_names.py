#!/usr/bin/env python3
"""Correct level 20's baseline-comparison test names.

Both names asserted things the fixture does not show. The baseline flags exactly one case, and that case
is genuine, so it has 100% precision and 33.33% recall rather than "catches none" with zero recall. Only
the recall figure made it lose: it misses two of the three frauds, giving f1 0.5 against the ensemble's
0.857.

This is the eleventh level where a test name has asserted something the fixture did not contain. The
corrected names are also a better description of what the example teaches, which has been true every time:
a trivial baseline can have perfect precision and still be useless, because precision alone says nothing
about how much of the thing you were looking for you actually found.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

PATH = Path(r"C:\Users\arian\OneDrive\ドキュメント\Myriaxon\certivo-rework\ai-ml-engineer\levels\20.json")

REFERENCE = (
    "SELECT model_name, "
    "COALESCE(ROUND(100.0 * SUM(CASE WHEN predicted = 1 AND actual = 1 THEN 1 ELSE 0 END) / "
    "NULLIF(SUM(CASE WHEN predicted = 1 THEN 1 ELSE 0 END), 0), 2), 0) AS precision_pct, "
    "COALESCE(ROUND(100.0 * SUM(CASE WHEN predicted = 1 AND actual = 1 THEN 1 ELSE 0 END) / "
    "NULLIF(SUM(CASE WHEN actual = 1 THEN 1 ELSE 0 END), 0), 2), 0) AS recall_pct, "
    "COALESCE(ROUND(2.0 * (1.0 * SUM(CASE WHEN predicted = 1 AND actual = 1 THEN 1 ELSE 0 END) / "
    "NULLIF(SUM(CASE WHEN predicted = 1 THEN 1 ELSE 0 END), 0)) * "
    "(1.0 * SUM(CASE WHEN predicted = 1 AND actual = 1 THEN 1 ELSE 0 END) / "
    "NULLIF(SUM(CASE WHEN actual = 1 THEN 1 ELSE 0 END), 0)) / "
    "NULLIF((1.0 * SUM(CASE WHEN predicted = 1 AND actual = 1 THEN 1 ELSE 0 END) / "
    "NULLIF(SUM(CASE WHEN predicted = 1 THEN 1 ELSE 0 END), 0)) + "
    "(1.0 * SUM(CASE WHEN predicted = 1 AND actual = 1 THEN 1 ELSE 0 END) / "
    "NULLIF(SUM(CASE WHEN actual = 1 THEN 1 ELSE 0 END), 0)), 0), 3), 0) AS f1 "
    "FROM evaluations GROUP BY model_name ORDER BY f1 DESC, model_name ASC"
)


def main() -> int:
    data = json.loads(PATH.read_text(encoding="utf-8"))
    target = next(q for q in data["questions"]
                  if "comparing the deployed model against a documented baseline" in q.get("prompt", "")
                  and "code" in q)

    connection = sqlite3.connect(":memory:")
    connection.executescript(target["code"]["setupSql"])
    cursor = connection.execute(REFERENCE)
    columns = [d[0] for d in cursor.description]
    rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
    connection.close()

    print(f"  reference returns: {rows}")
    by_name = {r["model_name"]: r for r in rows}
    baseline = by_name.get("baseline")
    ensemble = by_name.get("ensemble")
    if not baseline or not ensemble:
        print("  REFUSING: expected both a baseline and an ensemble row")
        return 1
    if baseline["recall_pct"] == 0:
        print("  the baseline recalls nothing, so the original names would have been right; "
              "leaving them alone")
        return 0

    tests = target["code"]["tests"]
    tests[0]["name"] = (
        f"the ensemble recalls all three frauds at {ensemble['precision_pct']:.0f}% precision, "
        f"while the baseline finds only one"
    )
    tests[1]["name"] = (
        f"hidden: the baseline has {baseline['precision_pct']:.0f}% precision and still loses, "
        f"because precision alone says nothing about what was missed"
    )
    for test in tests:
        test["expected"] = json.loads(json.dumps(rows))

    PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8", newline="\n")
    print(f"  corrected {len(tests)} test name(s) and expectation(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
