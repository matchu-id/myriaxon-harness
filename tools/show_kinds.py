#!/usr/bin/env python3
"""Print one example of each question kind from a Certivo level file.

The kinds are distinguished by which key is present -- `options` for multiple choice, `code` for a
live-coding question, `essay` for a written one -- not by a `kind` field. A previous tool assumed a
`kind` field, which does not exist, and reported every question as unknown.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def kind_of(question: dict) -> str:
    if "options" in question:
        return "multiple"
    if "code" in question:
        return "code"
    if "essay" in question:
        return "essay"
    return "?"


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: show_kinds.py <level.json>")
        return 2

    path = Path(sys.argv[1])
    data = json.loads(path.read_text(encoding="utf-8"))
    questions = data.get("questions") or []

    seen: dict[str, dict] = {}
    counts: dict[str, int] = {}
    for question in questions:
        kind = kind_of(question)
        counts[kind] = counts.get(kind, 0) + 1
        seen.setdefault(kind, question)

    print(f"=== {path.name} ===")
    print("  exam block:", json.dumps(data.get("exam"), indent=2).replace("\n", "\n  "))
    print("  counts:", counts)
    print()

    for kind in ("multiple", "code", "essay"):
        if kind not in seen:
            print(f"  --- {kind}: NONE IN THIS LEVEL ---")
            print()
            continue
        print(f"  --- {kind}: full example ---")
        print(json.dumps(seen[kind], indent=4)[:3000])
        print()

    # Every distinct sub-key used by each kind, so the writer can match the shape exactly.
    print("  --- keys used per kind, across the whole file ---")
    for kind in ("multiple", "code", "essay"):
        merged: set[str] = set()
        nested: set[str] = set()
        for question in questions:
            if kind_of(question) != kind:
                continue
            merged.update(question.keys())
            for value in question.values():
                if isinstance(value, dict):
                    nested.update(value.keys())
        print(f"    {kind:9} top: {sorted(merged)}")
        if nested:
            print(f"    {kind:9} nested: {sorted(nested)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
