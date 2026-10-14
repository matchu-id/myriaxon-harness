#!/usr/bin/env python3
"""Correct level 16's inRollout expectations from the reference, and check what the tests actually prove.

Two of the four expectations were guessed wrong: shopper-1 and shopper-42 both hash into a bucket below 50,
so the rollout answer is `true` where the file said `false`. The *properties* being asserted -- that the two
calls agree, that 0 includes nobody and 100 includes everybody -- all held, so the test names were accurate
and only the booleans were wrong.

The values are derived here rather than retyped, and the run also reports whether the two "does not flip"
tests are distinguishable at all: a subject whose percentage is far away from its bucket would pass whether
the implementation were deterministic or random, which is the vacuous-setup problem this project keeps
meeting. Printing the bucket beside the percentage is what makes that visible.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

PATH = Path(r"C:\Users\arian\OneDrive\ドキュメント\Myriaxon\certivo-rework\fullstack-developer\levels\16.json")
REFERENCE = Path(r"C:\Users\arian\OneDrive\ドキュメント\Myriaxon\certivo-rework\fullstack-developer\reference.js")

PROBE = """
const fs = require('fs');
// Passed through the environment because `node -e` shifts process.argv, so argv[2] is not the script
// path the way it is when Node runs a file.
const src = fs.readFileSync(process.env.MYRIAXON_REFERENCE, 'utf8');
eval(src);
const subjects = ['shopper-1', 'shopper-7', 'another-shopper', 'shopper-42'];
for (const s of subjects) {
  console.log(JSON.stringify({ subject: s, bucket: __bucket(s) }));
}
"""


def main() -> int:
    data = json.loads(PATH.read_text(encoding="utf-8"))
    target = next(q for q in data["questions"]
                  if q.get("code", {}).get("entryPoint") == "inRollout")

    import os
    env = dict(os.environ, MYRIAXON_REFERENCE=str(REFERENCE), PYTHONUTF8="1")
    probe = subprocess.run([r"C:\Program Files\nodejs\node.exe", "-e", PROBE],
                           capture_output=True, text=True, encoding="utf-8", env=env)
    if probe.returncode != 0:
        print(f"  bucket probe failed: {probe.stderr.strip()[:300]}")
        return 1
    buckets = {json.loads(line)["subject"]: json.loads(line)["bucket"]
               for line in probe.stdout.strip().splitlines()}
    for subject, bucket in buckets.items():
        print(f"  {subject:<18} bucket={bucket:<3} -> in a 50% rollout: {bucket < 50}, "
              f"in a 25% rollout: {bucket < 25}")

    corrected = 0
    for test in target["code"]["tests"]:
        subject, percent = test["args"]
        bucket = buckets[subject]
        expected = {
            "first": bucket < percent,
            "second": bucket < percent,
            "sameSubjectSameVersion": True,
            "atZero": bucket < 0,
            "atHundred": bucket < 100,
        }
        if test["expected"] != expected:
            corrected += 1
        test["expected"] = expected
        # A test only distinguishes deterministic from random when the subject sits near the boundary, so
        # say which of them actually do.
        boundary = abs(bucket - percent) <= 10
        print(f"    {test['id']}: {subject} at {percent}% -> first={expected['first']}"
              f"  {'(near the boundary, so it can fail)' if boundary else '(far from the boundary)'}")

    PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8", newline="\n")
    print(f"  corrected {corrected} expectation(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
