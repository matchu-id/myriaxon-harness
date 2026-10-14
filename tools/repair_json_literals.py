#!/usr/bin/env python3
"""Repair Python literals that leaked into JSON level files.

Hand-authoring 25-question JSON while thinking in Python produces `None`, `True` and `False`, none of which
are JSON. Each one is a hard parse error rather than a subtle problem, so they are safe to fix mechanically
— but patching them by hand one at a time is how a file ends up half-repaired and harder to diagnose, which
happened twice on level 4.

The replacement is deliberately literal-aware: it only rewrites the tokens when they appear where a JSON
value can be, and it reports every change with its line number so the fix can be reviewed rather than
trusted. It refuses to write a file that does not parse afterwards.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# A JSON value position: after `:`, `[`, or `,`, allowing whitespace. This avoids rewriting the words when
# they appear inside a prompt or an explanation, where they are prose rather than values.
PATTERNS: list[tuple[re.Pattern[str], str, str]] = [
    (re.compile(r'(?<=[\[:,])(\s*)None(?=\s*[,\}\]])'), r"\1null", "None -> null"),
    (re.compile(r'(?<=[\[:,])(\s*)True(?=\s*[,\}\]])'), r"\1true", "True -> true"),
    (re.compile(r'(?<=[\[:,])(\s*)False(?=\s*[,\}\]])'), r"\1false", "False -> false"),
]


def main() -> int:
    parser = argparse.ArgumentParser(description="Repair Python literals in a JSON file.")
    parser.add_argument("paths", type=Path, nargs="+")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    exit_code = 0
    for path in args.paths:
        text = path.read_text(encoding="utf-8")
        try:
            json.loads(text)
            print(f"  {path.name}: already valid JSON")
            continue
        except json.JSONDecodeError as error:
            print(f"  {path.name}: invalid at line {error.lineno} -- {error.msg}")

        repaired = text
        changes: list[str] = []
        for pattern, replacement, label in PATTERNS:
            repaired, count = pattern.subn(replacement, repaired)
            if count:
                # Report the line of each hit so the change can be checked rather than assumed.
                for match in pattern.finditer(text):
                    line = text.count("\n", 0, match.start()) + 1
                    changes.append(f"line {line}: {label}")

        if not changes:
            print("    no Python literals found in value positions; the error is something else")
            exit_code = 1
            continue

        try:
            data = json.loads(repaired)
        except json.JSONDecodeError as error:
            print(f"    REFUSING: still invalid after repair -- line {error.lineno}, {error.msg}")
            exit_code = 1
            continue

        for change in changes:
            print(f"    {change}")
        if not args.dry_run:
            path.write_text(repaired, encoding="utf-8", newline="\n")
            print(f"    wrote; {len(data.get('questions', []))} questions parse")
        else:
            print("    [dry-run] not written")

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
