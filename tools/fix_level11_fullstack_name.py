#!/usr/bin/env python3
"""Correct level 11's layer-shape test name to match the figure the reference produces.

The name said the mixed layer holds "more than every real layer combined", and the reference reports it at
4000 lines against 730 for services, routes and data together. The layer holds several times the rest, not
less than it, so the comparison in the name was inverted.
"""

from __future__ import annotations

import json
from pathlib import Path

PATH = Path(r"C:\Users\arian\OneDrive\ドキュメント\Myriaxon\certivo-rework\fullstack-developer\levels\11.json")


def main() -> int:
    data = json.loads(PATH.read_text(encoding="utf-8"))
    fixed = 0

    for question in data["questions"]:
        spec = question.get("code")
        if not spec or spec.get("language") != "sql":
            continue
        if "reporting the total lines and file count per layer" not in question.get("prompt", ""):
            continue
        spec["tests"][0]["name"] = (
            "the mixed layer still holds 4000 lines, several times the real layers together"
        )
        fixed += 1

    PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8", newline="\n")
    print(f"  corrected {fixed} test name(s)")
    return 0 if fixed == 1 else 1


if __name__ == "__main__":
    raise SystemExit(main())
