#!/usr/bin/env python3
"""Fix level 15's imbalance fixture so it matches its own test names.

The names assert that the two splits are "the same size" and that the test split "keeps the true 25%
positive rate". The fixture gave train six rows and test four -- different sizes -- so the first claim was
false. Six levels running now, a test name has asserted a property the fixture did not have:

  11  region recall, where the region had three returns and the name said two
  12  shipped clean, names checked
  13  "num_past_orders is entirely NULL" when two of four rows had values
  14  "orders has one stale" when its refresh dates were both inside the window
  15  "the rows are the same size" when they were six and four

The names are claims, and they are checked here the same way an expected value is: against the output.

Making the splits equal also improves the question, because it isolates the variable that matters. With
equal sizes, the only difference between the two rows is the positive rate -- train at 33.33% against test
at 25% -- so the reader sees the resampling effect without a row-count difference to explain it away.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

PATH = Path(r"C:\Users\arian\OneDrive\ドキュメント\Myriaxon\certivo-rework\ai-ml-engineer\levels\15.json")

# Four rows per split. Train carries two positives (33.33%), test carries one (25%, the true rate).
SETUP = (
    "CREATE TABLE dataset (split TEXT, order_id TEXT, returned INTEGER);\n"
    "INSERT INTO dataset VALUES "
    "('train','t1',1),('train','t2',0),('train','t3',1),('train','t4',0),"
    "('test','e1',1),('test','e2',0),('test','e3',0),('test','e4',0);"
)

REFERENCE = (
    "SELECT split, COUNT(*) AS n, SUM(CASE WHEN returned = 1 THEN 1 ELSE 0 END) AS n_pos, "
    "SUM(CASE WHEN returned = 0 THEN 1 ELSE 0 END) AS n_neg, "
    "ROUND(100.0 * SUM(CASE WHEN returned = 1 THEN 1 ELSE 0 END) / COUNT(*), 2) AS pos_pct "
    "FROM dataset GROUP BY split ORDER BY split ASC"
)


def main() -> int:
    data = json.loads(PATH.read_text(encoding="utf-8"))
    target = next(q for q in data["questions"]
                  if "quantifying the class imbalance" in q.get("prompt", "") and "code" in q)

    target["code"]["setupSql"] = SETUP

    connection = sqlite3.connect(":memory:")
    connection.executescript(SETUP)
    cursor = connection.execute(REFERENCE)
    columns = [d[0] for d in cursor.description]
    rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
    connection.close()

    sizes = {row["split"]: row["n"] for row in rows}
    print(f"  reference returns: {rows}")
    if len(set(sizes.values())) != 1:
        print(f"  REFUSING to write: the splits are still unequal in size ({sizes}), so the test name "
              f"claiming they are the same size would still be false")
        return 1

    tests = target["code"]["tests"]
    tests[0]["name"] = "four rows per split, with the test split keeping the true 25% positive rate"
    tests[1]["name"] = "hidden: a balanced test split would mean resampling leaked out of training"
    for test in tests:
        test["expected"] = json.loads(json.dumps(rows))

    PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8", newline="\n")
    print(f"  wrote into {len(tests)} test(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
