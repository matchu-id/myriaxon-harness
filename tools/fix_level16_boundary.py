#!/usr/bin/env python3
"""Make level 16's rollback fixture actually contain the boundary its test name claims.

The name said "a minute exactly at the threshold does not breach", and the fixture's error rates were
0.5%, 2.5%, 4.0% and 0.71% against a 2.0% threshold. Nothing sat at 2.0, so the strictness of the
comparison was never exercised -- a learner writing `>=` would have passed.

That is the eighth level running where a test name asserted something the fixture did not contain, and
this instance is worse than the earlier ones: the others were merely false, whereas this one left a
required behaviour untested while claiming to test it. The fix is to put a minute at exactly 2% in the
fixture, which is the value the name has been describing all along.

200 requests with 4 errors is exactly 2.00%, so the comparison has to be strictly greater for the correct
answer to be 0.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

PATH = Path(r"C:\Users\arian\OneDrive\ドキュメント\Myriaxon\certivo-rework\ai-ml-engineer\levels\16.json")

SETUP = (
    "CREATE TABLE health (minute_utc TEXT, n_requests INTEGER, n_errors INTEGER);\n"
    "INSERT INTO health VALUES "
    "('10:00',1000,5),('10:01',1200,30),('10:02',1500,60),('10:03',200,4);"
)

REFERENCE = (
    "SELECT minute_utc, ROUND(100.0 * n_errors / n_requests, 2) AS error_pct, "
    "CASE WHEN 100.0 * n_errors / n_requests > ? THEN 1 ELSE 0 END AS breached "
    "FROM health ORDER BY minute_utc ASC"
)


def main() -> int:
    data = json.loads(PATH.read_text(encoding="utf-8"))
    target = next(q for q in data["questions"]
                  if "auditing an automatic rollback rule" in q.get("prompt", "") and "code" in q)

    target["code"]["setupSql"] = SETUP
    threshold = target["code"]["tests"][0].get("args") or [2.0]

    connection = sqlite3.connect(":memory:")
    connection.executescript(SETUP)
    cursor = connection.execute(REFERENCE, threshold)
    columns = [d[0] for d in cursor.description]
    rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
    connection.close()

    print(f"  reference returns: {rows}")
    at_threshold = [r for r in rows if r["error_pct"] == float(threshold[0])]
    if not at_threshold:
        print(f"  REFUSING to write: no minute sits at exactly {threshold[0]}%, so the test name "
              f"claiming the boundary is checked would still be false")
        return 1
    if any(r["breached"] for r in at_threshold):
        print(f"  REFUSING to write: a minute at exactly {threshold[0]}% breached, so the comparison "
              f"is not strict")
        return 1

    tests = target["code"]["tests"]
    tests[0]["name"] = "the 10:01 and 10:02 minutes breach a 2% threshold before the rate falls back"
    tests[1]["name"] = "hidden: 10:03 sits at exactly 2.00% and does not breach, so the rule is strict"
    for test in tests:
        test["expected"] = json.loads(json.dumps(rows))

    PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8", newline="\n")
    print(f"  wrote into {len(tests)} test(s); a minute now sits exactly at the threshold")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
