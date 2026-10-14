#!/usr/bin/env python3
"""Show the real shape of a Certivo level file, so tooling stops guessing at field names.

The previous audit reported every question as kind "?" -- a schema assumption, not a finding. This
prints the actual keys and two complete example questions instead of inferring them.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path


def shape(value: object, depth: int = 0) -> str:
    pad = "  " * depth
    if isinstance(value, dict):
        if depth >= 2:
            return "{" + ", ".join(list(value)[:8]) + "}"
        lines = []
        for key, item in list(value.items())[:20]:
            lines.append(f"{pad}  {key}: {shape(item, depth + 1)}")
        return "{\n" + "\n".join(lines) + f"\n{pad}}}"
    if isinstance(value, list):
        if not value:
            return "[]"
        return f"[{len(value)} x {shape(value[0], depth + 1)}]"
    if isinstance(value, str):
        return f"str({value[:60]!r})" if len(value) > 60 else f"str({value!r})"
    return f"{type(value).__name__}({value})"


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: show_schema.py <level.json> [more.json ...]")
        return 2

    for name in sys.argv[1:]:
        path = Path(name)
        data = json.loads(path.read_text(encoding="utf-8"))
        print(f"=== {path.name} ===")
        print("  top-level keys:", list(data))
        for key, value in data.items():
            if key == "questions":
                continue
            print(f"    {key}: {shape(value, 1)}")

        questions = data.get("questions") or []
        print(f"  questions: {len(questions)}")
        if questions:
            keys = Counter()
            for question in questions:
                keys.update(question.keys())
            print("  keys present across all questions:")
            for key, n in keys.most_common():
                print(f"    {key:24} in {n}/{len(questions)}")
            print()
            print("  --- first question, in full ---")
            print(json.dumps(questions[0], indent=4)[:2600])
            if len(questions) > 1:
                print()
                print("  --- keys of question 2, for comparison ---")
                print("   ", list(questions[1]))
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
