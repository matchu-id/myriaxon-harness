#!/usr/bin/env python3
"""Fix level 20's boundary-crossing fixture so it distinguishes a shared module from a violation.

The fixture put `shared` in a domain of its own, so an import of it counted as crossing a boundary -- and
the test name claiming that "cart to shared" was absent was false. The reference returned four rows where
the name described three.

The deeper problem is that the model had no way to express what a shared module *is*. A module every domain
may use is not in a domain at all; marking it as shared and excluding it from the comparison is what makes
the query answer the question the board actually asks, which is which imports cross a boundary they should
not. The fixture and the query both change, and the name is generated from the derived rows.

The reference in `verify_code_questions.py` needs the matching `is_shared` column, and its entry is updated
here as well so the two cannot drift apart.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

LEVEL = Path(r"C:\Users\arian\OneDrive\ドキュメント\Myriaxon\certivo-rework\fullstack-developer\levels\20.json")
TOOL = Path(r"C:\Users\arian\OneDrive\ドキュメント\Myriaxon\tools\verify_code_questions.py")

SETUP = (
    "CREATE TABLE imports (from_module TEXT, to_module TEXT);\n"
    "CREATE TABLE boundaries (module TEXT, domain TEXT, is_shared INTEGER);\n"
    "INSERT INTO imports VALUES "
    "('cart','catalog'),('cart','shared'),('cart','orders'),"
    "('shipping','catalog'),('orders','shared'),('orders','cart');\n"
    "INSERT INTO boundaries VALUES "
    "('cart','checkout',0),('orders','checkout',0),('catalog','catalog',0),"
    "('shipping','shipping',0),('shared','shared',1);"
)

QUERY = (
    "SELECT DISTINCT i.from_module, i.to_module FROM imports i "
    "JOIN boundaries f ON f.module = i.from_module "
    "JOIN boundaries t ON t.module = i.to_module "
    "WHERE f.domain <> t.domain AND t.is_shared = 0 "
    "ORDER BY i.from_module ASC, i.to_module ASC"
)

NEW_REFERENCE = (
    '    "boundary_crossings": (\n'
    '        "SELECT DISTINCT i.from_module, i.to_module FROM imports i "\n'
    '        "JOIN boundaries f ON f.module = i.from_module "\n'
    '        "JOIN boundaries t ON t.module = i.to_module "\n'
    '        "WHERE f.domain <> t.domain AND t.is_shared = 0 "\n'
    '        "ORDER BY i.from_module ASC, i.to_module ASC"\n'
    '    ),'
)

OLD_REFERENCE_PATTERN = re.compile(
    r'    "boundary_crossings": \(\n.*?\n    \),', re.DOTALL
)


def main() -> int:
    data = json.loads(LEVEL.read_text(encoding="utf-8"))
    target = next(q for q in data["questions"]
                  if "outbound dependencies that cross a boundary" in q.get("prompt", ""))

    target["code"]["setupSql"] = SETUP

    connection = sqlite3.connect(":memory:")
    connection.executescript(SETUP)
    cursor = connection.execute(QUERY)
    columns = [d[0] for d in cursor.description]
    rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
    connection.close()

    pairs = [f"{r['from_module']} to {r['to_module']}" for r in rows]
    for pair in pairs:
        print(f"  crossing: {pair}")
    if any(r["to_module"] == "shared" for r in rows):
        print("  REFUSING: a shared module still counts as a crossing")
        return 1

    plural = "crossings are" if len(pairs) > 1 else "crossing is"
    target["code"]["tests"][0]["name"] = (
        f"{len(pairs)} {plural} listed: " + ", ".join(pairs)
    )
    target["code"]["tests"][1]["name"] = (
        "hidden: cart to shared and orders to shared are shared modules and are absent"
    )
    for test in target["code"]["tests"]:
        test["expected"] = json.loads(json.dumps(rows))

    LEVEL.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                     encoding="utf-8", newline="\n")

    source = TOOL.read_text(encoding="utf-8")
    updated, count = OLD_REFERENCE_PATTERN.subn(NEW_REFERENCE, source, count=1)
    if count != 1:
        print(f"  could not locate the boundary_crossings reference to update (matched {count})")
        return 1
    TOOL.write_text(updated, encoding="utf-8", newline="\n")

    print(f"  name generated from {len(rows)} derived row(s); the reference query was updated to match")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
