#!/usr/bin/env python3
"""Fill a level's code-question expectations by running its registered reference query.

Generalises the level-15 fill script. For every code question in the level, the reference is chosen by the
same matcher table the verifier uses, run against the question's own `setupSql`, and whatever SQLite
returns becomes the expected value for every test.

Two properties this relies on, both worth stating because they are what make it trustworthy:

  * The reference and the question are matched by `PROMPT_MATCHERS`, the same table the verifier uses. If
    the matcher is wrong, the verifier will disagree with what this writes, so a mistake surfaces as a
    failure rather than as silently agreeing values.
  * `?` placeholders are bound from the first test's `args`, so a parameterised question is generated
    against the arguments it will actually be asked with. A level whose tests use different args per test
    needs those checked separately.

Usage:  python3 fill_level_expected.py <level.json> [--report-names]
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))

from verify_code_questions import PROMPT_MATCHERS, REFERENCE_SOLUTIONS  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("level")
    parser.add_argument("--report-names", action="store_true",
                        help="print each test name beside the value derived for it")
    parser.add_argument("--skip-ts", action="store_true",
                        help="ignore TypeScript questions, which this tool cannot derive values for")
    args = parser.parse_args()

    path = Path(args.level)
    data = json.loads(path.read_text(encoding="utf-8"))
    filled = 0

    for question in data["questions"]:
        spec = question.get("code")
        if not spec:
            continue
        # Only SQL questions can be derived here: the reference table holds queries, and a TypeScript
        # question's values come from `verify_ts_questions.py` running a reference implementation instead.
        # Saying so rather than reporting "no reference matched" keeps a real matcher failure visible.
        if spec.get("language") != "sql":
            if not args.skip_ts:
                print(f"  skipped (not SQL): {spec.get('language')} -- use verify_ts_questions.py")
            continue
        prompt = question.get("prompt", "")
        key = next((k for fragment, k in PROMPT_MATCHERS if fragment in prompt), None)
        if key is None:
            print(f"  no reference matched: {prompt[:64]}...")
            continue
        query = REFERENCE_SOLUTIONS[key]

        connection = sqlite3.connect(":memory:")
        connection.executescript(spec["setupSql"])
        tests = spec["tests"]
        # Bind from the first test's arguments, which is what all tests in these levels share.
        params = tests[0].get("args") or []
        try:
            cursor = connection.execute(query, params)
        except sqlite3.Error as error:
            print(f"  {key}: QUERY FAILED {error}")
            connection.close()
            return 1
        columns = [d[0] for d in cursor.description]
        rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
        connection.close()

        if not rows:
            print(f"  {key}: REFUSING to write an empty expectation -- a learner returning nothing "
                  f"would pass, so the fixture does not exercise the question")
            return 1

        print(f"  {key}: {len(rows)} row(s)")
        print(f"    {json.dumps(rows, ensure_ascii=False)}")
        for test in tests:
            if (test.get("args") or []) != params and test.get("args"):
                print(f"    NOTE: test {test['id']!r} uses different args {test['args']} "
                      f"than the generated {params}")
            test["expected"] = json.loads(json.dumps(rows))
            if args.report_names:
                print(f"      {test['id']}: {test['name']}")
            filled += 1

    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8", newline="\n")
    print(f"  wrote {filled} expectation(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
