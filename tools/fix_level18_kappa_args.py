#!/usr/bin/env python3
"""Expand the Python list arithmetic that leaked into level 18's JSON.

`[["ok"] * 19 + ["bad"], ...]` is Python, not JSON, so the file would not parse. This is the same class of
mistake as `True`/`False`/`None`, which `repair_json_literals.py` handles — but that tool deliberately only
rewrites whole value tokens in value positions, so an expression like this is left alone rather than guessed
at. Expanding it needs the expression evaluated, which is what this does for the one known form.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

PATH = Path(r"C:\Users\arian\OneDrive\ドキュメント\Myriaxon\certivo-rework\data-scientist\levels\18.json")

OLD = '[["ok"] * 19 + ["bad"], ["ok"] * 19 + ["ok"]]'


def main() -> int:
    text = PATH.read_text(encoding="utf-8")
    if OLD not in text:
        print("  the expression is not present; nothing to expand")
    else:
        expanded = json.dumps([["ok"] * 19 + ["bad"], ["ok"] * 19 + ["ok"]])
        text = text.replace(OLD, expanded)
        PATH.write_text(text, encoding="utf-8", newline="\n")
        print(f"  expanded the list arithmetic into {len(expanded)} characters of JSON")

    data = json.loads(PATH.read_text(encoding="utf-8"))
    target = next(q for q in data["questions"]
                  if q.get("code", {}).get("entryPoint") == "cohens_kappa")
    test = target["code"]["tests"][2]
    print(f"  t3 args now hold {len(test['args'][0])} labels per reviewer, with one disagreement")
    return 0


if __name__ == "__main__":
    sys.exit(main())
