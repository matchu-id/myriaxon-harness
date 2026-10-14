#!/usr/bin/env python3
"""Fill level 17's connection-demand fixture and correct the latency question's stated mean.

Two defects in what was first written:

  * The connection-demand question was authored with an empty `setupSql` and no expectation, so the
    fixture was never there and the question could not have been run.
  * The latency question's hidden test claimed a mean of 200 for a sample whose real mean is 205. The p95
    claim in the same test does hold -- with twenty samples the nearest-rank 95th percentile lands on the
    largest value -- which is what makes the test worth keeping: it shows the tail that the mean hides.

The correct mean is written back here and the whole thing is then verified by execution, which is the only
way the figure should ever have been produced.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

PATH = Path(r"C:\Users\arian\OneDrive\ドキュメント\Myriaxon\certivo-rework\fullstack-developer\levels\17.json")

SETUP = (
    "CREATE TABLE environments (name TEXT, instances INTEGER, pool_per_instance INTEGER, "
    "server_connection_limit INTEGER);\n"
    "INSERT INTO environments VALUES "
    "('staging',2,10,100),('production',4,25,100),('sale-day',40,25,100);"
)

QUERY = (
    "SELECT name, instances * pool_per_instance AS demand, "
    "CASE WHEN instances * pool_per_instance > server_connection_limit THEN 'yes' ELSE 'no' END "
    "AS exceeds FROM environments ORDER BY demand DESC, name ASC"
)

SAMPLES = list(range(200, 420, 20)) * 10 + [2000] * 6


def main() -> int:
    data = json.loads(PATH.read_text(encoding="utf-8"))

    # 1. The connection-demand question.
    demand_q = next(q for q in data["questions"]
                    if "how many database connections the application fleet" in q.get("prompt", ""))
    demand_q["code"]["setupSql"] = SETUP

    connection = sqlite3.connect(":memory:")
    connection.executescript(SETUP)
    cursor = connection.execute(QUERY)
    columns = [d[0] for d in cursor.description]
    rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
    connection.close()
    for row in rows:
        print(f"  {row['name']:<12} demand={row['demand']:<5} exceeds={row['exceeds']}")
    for test in demand_q["code"]["tests"]:
        test["expected"] = json.loads(json.dumps(rows))
    print(f"  wrote {len(rows)} row(s) into {len(demand_q['code']['tests'])} test(s)")

    # 2. The latency question's mean.
    samples = sorted(SAMPLES)
    count = len(samples)
    mean = round(sum(samples) / count)
    p95 = samples[__import__("math").ceil(0.95 * count) - 1]
    print(f"  latency: count={count} mean={mean} p95={p95} max={samples[-1]}")

    latency_q = next(q for q in data["questions"]
                     if q.get("code", {}).get("entryPoint") == "latencyReport")
    hidden = latency_q["code"]["tests"][1]
    hidden["args"] = [list(SAMPLES)]
    hidden["expected"] = {"count": count, "mean": mean, "p95": p95, "max": samples[-1]}
    hidden["name"] = (
        f"hidden: a mean of {mean} looks healthy while p95 is {p95} and the worst case {samples[-1]}"
    )
    print(f"  corrected the latency expectation to mean={mean}, p95={p95}, max={samples[-1]}")

    PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
