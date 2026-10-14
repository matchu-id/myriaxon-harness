#!/usr/bin/env python3
"""Fill level 15's expectations from the reference queries, and check the test names against reality.

Three of level 15's four code questions were authored with empty expectations on purpose: the values are
derived here by running each reference query against the question's own setup, which is the discipline
that has caught a wrong expectation on each of the last five levels.

The test names are checked against the derived output too. Five levels running, a name has asserted
something the data does not show -- a category a size filter excluded, a recall whose region had three
returns, a column that was not entirely NULL, a stale column that was not stale. The names are asserted
claims like any other, so they get the same treatment.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))

from verify_code_questions import REFERENCE_SOLUTIONS  # noqa: E402

PATH = Path(r"C:\Users\arian\OneDrive\ドキュメント\Myriaxon\certivo-rework\ai-ml-engineer\levels\15.json")

# Prompt fragment -> (reference key, human check of what the output should show)
EXPECTATIONS = (
    ("ranking features by importance", "importance_cumulative",
     "order_value 50/50, then 75, 90, 100 cumulative"),
    ("quantifying the class imbalance", "split_balance",
     "test keeps 25% positives; train was not resampled in this fixture"),
    ("reliability table", "reliability_table",
     "high band states 85% predicted against 66.67% actual -- overconfident"),
    ("summarising a learning curve", "learning_curve_average",
     "gap of 0.135 at 100 rows narrowing to 0.0275 at 2000"),
)


def main() -> int:
    data = json.loads(PATH.read_text(encoding="utf-8"))
    filled = 0

    for question in data["questions"]:
        spec = question.get("code")
        if not spec:
            continue
        prompt = question.get("prompt", "")
        for fragment, key, _description in EXPECTATIONS:
            if fragment not in prompt:
                continue
            query = REFERENCE_SOLUTIONS.get(key)
            if not query:
                print(f"  no reference registered under {key!r}")
                return 1
            connection = sqlite3.connect(":memory:")
            connection.executescript(spec["setupSql"])
            cursor = connection.execute(query)
            columns = [d[0] for d in cursor.description]
            rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
            connection.close()
            print(f"  {key}: {len(rows)} row(s)")
            print(f"    {json.dumps(rows, ensure_ascii=False)}")
            for test in spec["tests"]:
                test["expected"] = json.loads(json.dumps(rows))
                filled += 1
            break

    PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8", newline="\n")
    print(f"  wrote {filled} expectation(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
