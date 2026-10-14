#!/usr/bin/env python3
"""Generate a code question's `expected` values by running a candidate query.

Why this exists. Seven times now, an `expected` value has been wrong in a way that only showed up when the
reference query was executed: a category that a size filter should have excluded, a threshold table
computed by hand, a recall whose region had three returns rather than two, an overfitting gap read off
two columns. Each was caught by `verify_code_questions.py`, and each cost a write-fix-verify cycle.

The cause is the ordering. Authoring a fixture and then typing what it should produce means writing the
same reasoning twice, and the second pass is where it goes wrong. This inverts that: you supply the
question's setup and the query that answers it, and the expected values are whatever SQLite returns.

Usage:
  python3 gen_expected.py --setup "CREATE TABLE ...; INSERT ...;" --query "SELECT ..."

Or with a level file and a question index, to fill in a `code` question in place:

  python3 gen_expected.py --level levels/13.json --question 21 --query-file q.sql
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path


def run(setup: str, query: str) -> list[dict]:
    # An in-memory database, so this can never touch a real file by accident.
    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript(setup)
        cursor = connection.execute(query)
        columns = [description[0] for description in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]
    finally:
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--setup", default="")
    parser.add_argument("--query", default="")
    parser.add_argument("--level", default="")
    parser.add_argument("--question", type=int, default=0,
                        help="1-based question number within the level")
    parser.add_argument("--write", action="store_true",
                        help="write the generated values into every test of that question")
    args = parser.parse_args()

    setup, query = args.setup, args.query
    level_path = Path(args.level) if args.level else None
    question = None

    if level_path:
        data = json.loads(level_path.read_text(encoding="utf-8"))
        # `--question` counts all questions; the code spec lives under the nth question's `code` key.
        question = data["questions"][args.question - 1]
        spec = question.get("code")
        if not spec:
            print(f"  question {args.question} is not a code question")
            return 1
        setup = setup or spec.get("setupSql", "")
        query = query or ""

    if not setup:
        print("  nothing to run: pass --setup, or --level with --question")
        return 1
    if not query:
        print("  no query given; pass --query with the reference solution")
        return 1

    try:
        rows = run(setup, query)
    except sqlite3.Error as error:
        print(f"  QUERY FAILED: {error}")
        return 1

    print(f"  {len(rows)} row(s):")
    print(json.dumps(rows, indent=2, ensure_ascii=False))

    if args.write and question is not None:
        tests = question["code"]["tests"]
        for test in tests:
            test["expected"] = json.loads(json.dumps(rows))
        level_path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                              encoding="utf-8", newline="\n")
        print(f"  wrote {len(rows)} row(s) into {len(tests)} test(s)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
