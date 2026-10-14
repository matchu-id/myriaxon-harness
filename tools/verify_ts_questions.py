#!/usr/bin/env python3
"""Verify TypeScript code questions by running a reference implementation against each test.

The problem this solves. A `code` question carries `tests` with `args` and `expected`, and for SQL the
expected values are derived by running the reference query. TypeScript questions have no equivalent
check, which means their `expected` values would be typed by hand -- and hand-written expectations have
disagreed with their fixtures on eleven levels so far, twice in a way that left a required behaviour
untested.

This closes that gap the same way. A reference implementation is supplied for each question, compiled by
stripping TypeScript annotations, run in Node against the test arguments, and the returned value compared
to `expected`. A disagreement is a bug in one of the three, and the tool says which.

Scope, stated honestly because the limits matter:

  * References are **plain JavaScript, not TypeScript**. The verifier does not transform anything; it
    refuses `interface`, `enum`, `namespace`, `declare` and decorators, and lets Node report anything else
    as a syntax error. Five attempts at stripping annotations each produced JavaScript that parsed and
    meant something different, so the classification was abandoned rather than approximated -- see
    `strip_types` for the full account.
  * It runs untrusted-looking code in a real Node process, so it must only ever be pointed at reference
    implementations written for these levels -- never at learner submissions.
  * It needs a `node` binary. If none is found the tool reports that and exits non-zero rather than
    silently passing.

Usage:
  python3 verify_ts_questions.py <level.json> --reference <file.ts>

The reference file must define every function named by a question's `entryPoint`. Each question is run
separately, in its own process, so one broken reference cannot mask another.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# Node is invoked once per question. Twelve seconds is generous for these fixtures and still fails fast
# on an accidental infinite loop rather than hanging the round.
NODE_TIMEOUT_SECONDS = 12


def strip_types(source: str) -> str:
    """Return the reference implementation unchanged, refusing only what JavaScript cannot parse at all.

    This function used to remove TypeScript annotations and no longer does, because five attempts at
    stripping produced five different silent corruptions. Each one produced JavaScript that still parsed
    and meant something different, which is the worst failure available: the reference computes the wrong
    answer, the comparison fails, and the question looks broken rather than the tool.

    The reason it cannot work is that `name: Type` and `key: value` are the same characters. `: item.id` is
    a property value in one place and a type would never look like it -- until it does, as `const x: number`
    followed by `{ id: item.id }`, and any rule that catches one catches the other. Telling them apart needs
    a parser, and a partial parser is more dangerous than none.

    So references are written as plain JavaScript and this verifies that rather than transforming it. What
    it refuses is limited to syntax JavaScript cannot parse in any position, which makes the check exact:
    `interface`, `enum`, `namespace`, `declare` and decorators. An `: Type` annotation that slips through
    is caught by Node as a syntax error, with the offending line printed -- a loud failure, which is the
    outcome to prefer over a quiet wrong answer.

    The cost is verbosity in one internal file. The benefit is that what Node runs is byte-for-byte what
    was reviewed, so a green verification means what it says.
    """
    problems = []
    for feature in ("enum ", "namespace ", "declare ", "@Component", "@Injectable", "interface "):
        if feature in source:
            problems.append(feature.strip())
    if problems:
        raise ValueError(
            "the reference is not plain JavaScript: "
            + ", ".join(sorted(set(problems)))
            + " -- write it without TypeScript syntax; see this function's docstring for why nothing "
              "is stripped automatically"
        )
    return source


def find_node() -> str | None:
    for name in ("node", "node.exe"):
        found = shutil.which(name)
        if found:
            return found
    for candidate in (
        r"C:\Program Files\nodejs\node.exe",
        r"C:\Program Files (x86)\nodejs\node.exe",
    ):
        if Path(candidate).exists():
            return candidate
    return None


def run_one(node: str, reference_js: str, entry_point: str, args: list) -> tuple[bool, object]:
    """Call `entry_point(...args)` in a fresh Node process and return what it produced.

    The harness resolves a promise if the reference returned one, so a question can be about `async` code
    without the tool needing to know in advance which it is. `Promise.resolve(x).then(...)` handles both:
    a plain value passes straight through, and a promise is awaited before the result is written. That is
    what lets level 5's code questions exercise `await`, error handling and fallbacks.
    """
    harness = (
        reference_js
        + "\n\n"
        + f"const __args = {json.dumps(args)};\n"
        + "Promise.resolve()\n"
        + f"  .then(() => {entry_point}(...__args))\n"
        + "  .then((__value) => {\n"
        + "    process.stdout.write(JSON.stringify(__value === undefined ? null : __value));\n"
        + "  })\n"
        + "  .catch((__error) => {\n"
        + "    process.stderr.write('REFERENCE_THREW: ' + (__error && __error.message));\n"
        + "    process.exit(3);\n"
        + "  });\n"
    )
    with tempfile.TemporaryDirectory() as directory:
        script = Path(directory) / "probe.js"
        # Written as UTF-8 with LF endings: the harness embeds JSON that may contain non-ASCII, and CRLF
        # in a Node script is harmless but makes a diff of the probe unreadable.
        script.write_text(harness, encoding="utf-8", newline="\n")
        try:
            completed = subprocess.run(
                [node, str(script)],
                capture_output=True, text=True, timeout=NODE_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            return False, f"timed out after {NODE_TIMEOUT_SECONDS}s"

    if completed.returncode != 0:
        # Report the whole stderr rather than its last line. A Node syntax error puts the offending source
        # line and a caret underneath, and the last line is only the version banner -- which is exactly
        # what made an earlier failure here unreadable.
        detail = " | ".join(
            line.strip() for line in completed.stderr.strip().splitlines() if line.strip()
        )[:400]
        return False, f"the reference threw: {detail}"
    try:
        return True, json.loads(completed.stdout)
    except json.JSONDecodeError:
        return False, f"the reference returned something that is not JSON: {completed.stdout[:120]!r}"


def main() -> int:
    if len(sys.argv) < 4 or sys.argv[2] != "--reference":
        print(__doc__)
        return 2

    level_path = Path(sys.argv[1])
    reference_path = Path(sys.argv[3])

    node = find_node()
    if node is None:
        print("  no node binary found, so TypeScript references cannot be executed.")
        print("  Refusing to report success: an unverified expectation is worse than none.")
        return 1

    data = json.loads(level_path.read_text(encoding="utf-8"))
    reference = reference_path.read_text(encoding="utf-8")
    try:
        reference_js = strip_types(reference)
    except ValueError as error:
        print(f"  {error}")
        return 1

    checked = failures = skipped = 0
    for index, question in enumerate(data["questions"], 1):
        spec = question.get("code")
        if not spec or spec.get("language") != "typescript":
            continue
        entry = spec.get("entryPoint")
        if not entry:
            print(f"  q{index}: no entryPoint, cannot verify")
            failures += 1
            continue
        if not re.search(rf"function\s+{re.escape(entry)}\b", reference_js):
            print(f"  q{index} [{entry}]: reference defines no such function")
            failures += 1
            skipped += 1
            continue

        print(f"  q{index} [{entry}]")
        for test in spec["tests"]:
            ok, actual = run_one(node, reference_js, entry, test.get("args") or [])
            checked += 1
            label = f"    {test['id']}{' (hidden)' if test.get('hidden') else ''}"
            if not ok:
                failures += 1
                print(f"{label}: ERROR {actual}")
                continue
            expected = test.get("expected")
            if actual == expected:
                print(f"{label}: ok  {json.dumps(actual, ensure_ascii=False)[:110]}")
            else:
                failures += 1
                print(f"{label}: MISMATCH")
                print(f"       expected: {json.dumps(expected, ensure_ascii=False)[:110]}")
                print(f"       actual  : {json.dumps(actual, ensure_ascii=False)[:110]}")

    print()
    print(f"  {checked - failures}/{checked} test expectation(s) confirmed by running the reference")
    if skipped:
        print(f"  {skipped} question(s) skipped for want of a reference function")
    if failures:
        print(f"  {failures} FAILURE(S)")
        return 1
    print("  every expected value is reproducible")
    return 0


if __name__ == "__main__":
    sys.exit(main())
