#!/usr/bin/env python3
"""Correct level 8's two SQL test names, which do not match what the references return.

  * The join question's hidden test said a per-product query "would need four more round trips than this".
    The reference returns four products, so the honest figure is four round trips in total for that
    approach — one per product — against the single one. The corrected name says that.
  * The category-count question's hidden test said "outdoor is absent, since the join finds no rows", which
    is right, but its visible test said "then outdoor with none", which contradicts it: an inner join
    cannot return a category with no products. The visible name now describes the two rows it gets.

Written as a script rather than a one-liner because a `python -c` string with quotes inside it does not
survive being passed through PowerShell, and the failure is a syntax error rather than anything subtle.
"""

from __future__ import annotations

import json
from pathlib import Path

PATH = Path(r"C:\Users\arian\OneDrive\ドキュメント\Myriaxon\certivo-rework\fullstack-developer\levels\08.json")


def main() -> int:
    data = json.loads(PATH.read_text(encoding="utf-8"))
    fixed = 0

    for question in data["questions"]:
        spec = question.get("code")
        if not spec or spec.get("language") != "sql":
            continue
        prompt = question.get("prompt", "")

        if "replaces the page's 501 round trips" in prompt:
            spec["tests"][1]["name"] = (
                "hidden: the same four rows via one query per product would take four round trips"
            )
            fixed += 1
        elif "reporting each category's product count" in prompt:
            spec["tests"][0]["name"] = "kitchen leads with three products and lighting follows with two"
            spec["tests"][1]["name"] = (
                "hidden: outdoor is absent entirely, since an inner join finds no products for it"
            )
            fixed += 1

    PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8", newline="\n")
    print(f"  corrected names in {fixed} question(s)")
    return 0 if fixed == 2 else 1


if __name__ == "__main__":
    raise SystemExit(main())
