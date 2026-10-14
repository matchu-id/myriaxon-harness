#!/usr/bin/env python3
"""Add level 18's fourth code question: how a request's spans add up to the time a customer waited.

The level was authored with three code questions where the shape needs four. The question added here
belongs to the same incident as the dependency-share query but asks the other half: not which dependency
dominated one request, but what the requests cost in aggregate and how much of that was spent in the
non-essential calls. That is the figure that justifies a budget, and it is the one an incident review asks
for when it wants to know whether the dependency is worth its cost at all.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

PATH = Path(r"C:\Users\arian\OneDrive\ドキュメント\Myriaxon\certivo-rework\fullstack-developer\levels\18.json")

SETUP = (
    "CREATE TABLE spans (request_id TEXT, service TEXT, duration_ms INTEGER, is_non_critical INTEGER);\n"
    "INSERT INTO spans VALUES "
    "('r1','product',100,0),('r1','recommendations',900,1),"
    "('r2','product',500,0),('r2','recommendations',100,1),"
    "('r3','product',50,0),('r3','recommendations',950,1),('r3','reviews',100,0),"
    "('r4','product',80,0),('r4','recommendations',120,1);"
)

QUERY = (
    "SELECT service, COUNT(*) AS n, SUM(duration_ms) AS total_ms, "
    "CAST(AVG(duration_ms) AS INTEGER) AS mean_ms FROM spans "
    "GROUP BY service ORDER BY total_ms DESC, service ASC"
)

QUESTION = {
    "skill": "devops-deployment",
    "prompt": "Write a query summarising the trace spans by service, so the incident review can see which "
              "service holds the time across every request rather than in the one that failed. The editor "
              "below shows the schema and the tests.",
    "explanation": "The single failing request showed recommendations as the culprit, and the aggregate is "
                   "what shows whether that is the general case or one bad request. Ranking services by "
                   "their total time is the figure that justifies a budget or a circuit breaker, because it "
                   "answers how much of the customers' waiting the dependency accounts for overall.",
    "code": {
        "language": "sql",
        "starterCode": "-- spans(request_id, service, duration_ms, is_non_critical)\n"
                       "SELECT service, SUM(duration_ms) AS total_ms FROM spans GROUP BY service;\n",
        "instructionsMarkdown": "The table is `spans(request_id, service, duration_ms, is_non_critical)`, "
                                "one row per service call within a request.\n\nReturn one row per `service` "
                                "with:\n\n- `n` \u2014 how many spans it accounts for\n- `total_ms` \u2014 the "
                                "sum of their durations\n- `mean_ms` \u2014 their mean duration, truncated to "
                                "a whole number\n\nOrder by `total_ms` descending, then `service` "
                                "ascending.\n\nPress **Run tests** to execute this query in your browser "
                                "through sql.js. Nothing runs on the server.",
        "timeoutMs": 5000,
        "setupSql": SETUP,
        "tests": [
            {"id": "t1",
             "name": "recommendations accounts for 2070ms across four spans, more than the other services",
             "args": [], "expected": []},
            {"id": "t2", "hidden": True,
             "name": "hidden: the mean is truncated rather than rounded, so 517.5 becomes 517",
             "args": [], "expected": []},
        ],
    },
}


def main() -> int:
    data = json.loads(PATH.read_text(encoding="utf-8"))

    connection = sqlite3.connect(":memory:")
    connection.executescript(SETUP)
    cursor = connection.execute(QUERY)
    columns = [d[0] for d in cursor.description]
    rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
    connection.close()

    for row in rows:
        print(f"  {row['service']:<16} n={row['n']} total={row['total_ms']} mean={row['mean_ms']}")

    leader = rows[0]
    QUESTION["code"]["tests"][0]["name"] = (
        f"{leader['service']} accounts for {leader['total_ms']}ms across {leader['n']} spans, "
        f"more than the other services"
    )
    # The truncation claim only holds if the mean is not a whole number, so it is checked rather than
    # asserted in prose.
    if float(leader["total_ms"]) / leader["n"] == leader["mean_ms"]:
        print(f"  correcting the hidden name: {leader['service']}'s mean is exact, so nothing is truncated")
        QUESTION["code"]["tests"][1]["name"] = (
            f"hidden: {leader['service']}'s mean over {leader['n']} spans is reported as one row"
        )
    for test in QUESTION["code"]["tests"]:
        test["expected"] = json.loads(json.dumps(rows))

    data["questions"].append(QUESTION)
    PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8", newline="\n")
    print(f"  added the span-summary question; level now holds {len(data['questions'])} questions")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
