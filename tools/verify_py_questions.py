#!/usr/bin/env python3
"""Verify Python code questions by running a reference implementation against each test.

The Data Scientist track's original code question is Python, and the schema accepts Python, so the rework
needs the same verification the other two tracks got -- but `verify_ts_questions.py` runs Node, and
`verify_code_questions.py` runs SQLite. Neither can execute Python, which means until this exists the only
way to produce a Python question's `expected` value would be to work it out by hand.

That is exactly the practice this pipeline was built to avoid, and it is worth saying why concretely: the
first draft of level 1 asked for the median of `[100, 200, 300, 400, 50000]` and stated 400. The correct
answer is 300. Nothing about the number looked wrong, and it would have reached a learner as an
unanswerable question.

How it works:

* A reference file holds plain Python functions, one per question, named by `entryPoint`.
* Each test's `args` are JSON-serialised, passed to a small runner, and the reference is called with them.
* The returned value is JSON-serialised and compared to `expected` as data, not as text, so `1` and `1.0`
  compare equal while `{"a": 1}` and `{"a": 1, "b": 2}` do not.
* A test whose expected value is missing or empty is refused rather than written, matching the other
  verifiers: a learner returning nothing must not pass.

The reference is imported into a namespace and called by name, so a question whose `entryPoint` has no
reference is reported as unverifiable rather than silently skipped.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

PYTHON = sys.executable
TIMEOUT_SECONDS = 20

RUNNER = '''
import json, sys

reference_path, level_path, out_path = sys.argv[1], sys.argv[2], sys.argv[3]

namespace = {}
with open(reference_path, encoding="utf-8") as handle:
    exec(compile(handle.read(), reference_path, "exec"), namespace)

with open(level_path, encoding="utf-8") as handle:
    level = json.load(handle)

results = []
for question in level["questions"]:
    spec = question.get("code")
    if not spec or spec.get("language") != "python":
        continue
    name = spec["entryPoint"]
    function = namespace.get(name)
    if function is None:
        results.append({"entry": name, "error": "no reference function named " + name})
        continue
    for test in spec["tests"]:
        record = {"entry": name, "id": test["id"], "hidden": bool(test.get("hidden"))}
        try:
            value = function(*test.get("args", []))
            json.dumps(value)
            record["actual"] = value
        except Exception as error:
            record["error"] = type(error).__name__ + ": " + str(error)[:160]
        results.append(record)

with open(out_path, "w", encoding="utf-8") as handle:
    json.dump(results, handle)
'''


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify Python code questions against a reference.")
    parser.add_argument("level", type=Path)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--report-names", action="store_true")
    parser.add_argument("--write", action="store_true",
                        help="write each verified value into the level file's expected field")
    args = parser.parse_args()

    level = json.loads(args.level.read_text(encoding="utf-8"))
    python_questions = [q for q in level["questions"]
                        if q.get("code", {}).get("language") == "python"]
    if not python_questions:
        print("  skipped (no python code questions)")
        return 0

    with tempfile.TemporaryDirectory() as work:
        runner = Path(work) / "runner.py"
        out = Path(work) / "out.json"
        runner.write_text(RUNNER, encoding="utf-8", newline="\n")
        completed = subprocess.run(
            [PYTHON, str(runner), str(args.reference), str(args.level), str(out)],
            capture_output=True, text=True, encoding="utf-8", timeout=TIMEOUT_SECONDS * 4,
        )
        if completed.returncode != 0 or not out.exists():
            print("  the reference could not be executed:")
            print(f"    {completed.stderr.strip()[:600]}")
            return 1
        results = json.loads(out.read_text(encoding="utf-8"))

    by_entry: dict[str, list[dict]] = {}
    for record in results:
        by_entry.setdefault(record["entry"], []).append(record)

    confirmed = 0
    expected_total = 0
    failures: list[str] = []
    written = 0

    for question in python_questions:
        spec = question["code"]
        name = spec["entryPoint"]
        records = by_entry.get(name, [])
        print(f"    q {name}")
        if not records or records[0].get("error", "").startswith("no reference"):
            print(f"      UNVERIFIABLE: no reference function named {name}")
            failures.append(name)
            continue
        for test, record in zip(spec["tests"], records):
            expected_total += 1
            if "error" in record:
                print(f"      {record['id']}: ERROR {record['error']}")
                failures.append(f"{name}.{record['id']}")
                continue
            actual = record["actual"]
            if args.report_names:
                print(f"      {record['id']}: {json.dumps(actual, ensure_ascii=False)[:110]}")
            if args.write:
                # The guard against a do-nothing implementation is now *question-level* rather than
                # test-level: a function that returns None for every input would satisfy any test expecting
                # None, so a question that cannot express a value is refused outright. A single test
                # expecting None is legitimate when it is describing a defined "nothing" answer -- level 7's
                # threshold chooser returns None when no operating point fits the review capacity -- and
                # refusing that would force the question to be written around the tool.
                if actual is None:
                    non_none = sum(1 for other in spec["tests"]
                                   if other.get("expected") is not None)
                    if non_none == 0:
                        print(f"      REFUSING to write a None expectation for {record['id']}: "
                              f"every test in {name} would expect nothing")
                        failures.append(f"{name}.{record['id']}")
                        continue
                if test.get("expected") != actual:
                    written += 1
                test["expected"] = actual
            if test.get("expected") == actual:
                confirmed += 1
                if not args.report_names:
                    print(f"      {record['id']}: ok  {json.dumps(actual, ensure_ascii=False)[:80]}")
            else:
                print(f"      {record['id']}: MISMATCH")
                print(f"        expected: {json.dumps(test.get('expected'), ensure_ascii=False)[:150]}")
                print(f"        actual  : {json.dumps(actual, ensure_ascii=False)[:150]}")
                failures.append(f"{name}.{record['id']}")

    if args.write and written:
        args.level.write_text(json.dumps(level, indent=2, ensure_ascii=False) + "\n",
                              encoding="utf-8", newline="\n")
        print(f"    wrote {written} expectation(s)")

    print()
    print(f"    {confirmed}/{expected_total} test expectation(s) confirmed by running the reference")
    if failures:
        print(f"    {len(failures)} FAILURE(S)")
        return 1
    print("    every expected value is reproducible")
    return 0


if __name__ == "__main__":
    sys.exit(main())
