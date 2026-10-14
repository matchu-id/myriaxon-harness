#!/usr/bin/env python3
"""Add level 20's fourth code question and bring the level to its shape.

The capstone was authored with three code questions where the shape needs four. The question added here is
the one a review board actually asks of a proposal: not whether the change is good, but what it costs to
undo. It reads a plan's steps and reports which of them cannot be reversed, which is what turns
"irreversible" from an adjective in a discussion into a list of steps somebody can point at.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

LEVEL = Path(r"C:\Users\arian\OneDrive\ドキュメント\Myriaxon\certivo-rework\fullstack-developer\levels\20.json")

SETUP = (
    "CREATE TABLE plan_steps (step_no INTEGER, description TEXT, reversible INTEGER);\n"
    "INSERT INTO plan_steps VALUES "
    "(1,'add unit_price column',1),"
    "(2,'backfill unit_price',1),"
    "(3,'drop price column',0),"
    "(4,'switch reads to unit_price',1),"
    "(5,'delete legacy price rows',0);"
)

QUERY = (
    "SELECT step_no, description FROM plan_steps WHERE reversible = 0 ORDER BY step_no ASC"
)

QUESTION = {
    "skill": "software-architecture",
    "prompt": "Write a query listing the steps of a migration plan that cannot be reversed, so the board can "
              "see what a rollback would leave behind rather than hearing that the change is irreversible. "
              "The editor below shows the schema and the tests.",
    "explanation": "A plan is only as reversible as its least reversible step, and naming those steps is what "
                   "makes the cost concrete: the board can ask what happens if step three runs and the "
                   "release is abandoned, rather than debating an adjective. It also makes the usual "
                   "reordering obvious, since an irreversible step placed last leaves everything before it "
                   "safe to undo.",
    "code": {
        "language": "sql",
        "starterCode": "-- plan_steps(step_no, description, reversible)\n"
                       "SELECT step_no, description FROM plan_steps;\n",
        "instructionsMarkdown": "The table is `plan_steps(step_no, description, reversible)`, where "
                                "`reversible` is `1` when the step can be undone.\n\nReturn `step_no` and "
                                "`description` for every step that **cannot** be reversed, ordered by "
                                "`step_no` ascending.\n\nPress **Run tests** to execute this query in your "
                                "browser through sql.js. Nothing runs on the server.",
        "timeoutMs": 5000,
        "setupSql": SETUP,
        "tests": [
            {"id": "t1", "name": "the two irreversible steps are named, at positions 3 and 5",
             "args": [], "expected": []},
            {"id": "t2", "hidden": True,
             "name": "hidden: the reversible steps are absent, since they are not what a rollback risks",
             "args": [], "expected": []},
        ],
    },
}


def main() -> int:
    data = json.loads(LEVEL.read_text(encoding="utf-8"))

    connection = sqlite3.connect(":memory:")
    connection.executescript(SETUP)
    cursor = connection.execute(QUERY)
    columns = [d[0] for d in cursor.description]
    rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
    connection.close()

    for row in rows:
        print(f"  irreversible step {row['step_no']}: {row['description']}")
    if not rows:
        print("  REFUSING: no irreversible step exists, so the question demonstrates nothing")
        return 1

    positions = " and ".join(str(r["step_no"]) for r in rows)
    QUESTION["code"]["tests"][0]["name"] = (
        f"the {len(rows)} irreversible steps are named, at positions {positions}"
    )
    for test in QUESTION["code"]["tests"]:
        test["expected"] = json.loads(json.dumps(rows))

    data["questions"].append(QUESTION)
    LEVEL.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                     encoding="utf-8", newline="\n")
    print(f"  added; level now holds {len(data['questions'])} questions")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
