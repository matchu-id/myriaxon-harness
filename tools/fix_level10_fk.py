#!/usr/bin/env python3
"""Reframe level 10's foreign-key question around a form SQLite actually answers.

The original asked for a count of foreign keys per table, through `pragma_foreign_key_list` joined to
`pragma_table_list`. That returns no rows: `pragma_foreign_key_list` takes the table name as an argument,
and without one it lists nothing, so the join had no rows to match. `pragma_table_list` does not expose a
foreign-key count in this SQLite version either, so there is no column to reach for instead.

The question now asks for the foreign-key relationships themselves, using the table-valued function with
its argument. That is both closer to what a migration that renames or drops a table needs -- the edges of
the dependency graph, not a count of them -- and answerable in one statement.
"""

from __future__ import annotations

import json
from pathlib import Path

PATH = Path(r"C:\Users\arian\OneDrive\ドキュメント\Myriaxon\certivo-rework\fullstack-developer\levels\10.json")

PROMPT = (
    "Write a query listing every foreign-key relationship in the database, as `from_table` and "
    "`to_table`, so a migration that renames or drops a table can see what depends on it. The editor "
    "below shows the schema and the tests."
)

EXPLANATION = (
    "Dependencies decide the order a migration can make its changes in, and they stop being visible from "
    "the table definitions alone once there are several. Asking the database for the edges is more "
    "reliable than reading migration files in order and reconstructing the graph by hand."
)

INSTRUCTIONS = (
    "SQLite exposes pragma table-valued functions that can be queried like ordinary tables.\n\n"
    "- `pragma_table_list` has a `name` column, one row per table\n"
    "- `pragma_foreign_key_list(name)` has `table` (the referenced table) and `from` (the referencing "
    "column) for each foreign key on that table, and must be given the table name as an argument\n\n"
    "Return one row per foreign-key relationship with:\n\n"
    "- `from_table` \u2014 the table that declares the key\n"
    "- `to_table` \u2014 the table it references\n\n"
    "Exclude SQLite's own tables, whose names begin with `sqlite_`. Order by `from_table` ascending, then "
    "`to_table` ascending.\n\n"
    "Press **Run tests** to execute this query in your browser through sql.js. Nothing runs on the server."
)

STARTER = (
    "-- pragma_table_list lists tables; pragma_foreign_key_list(name) lists one table's keys.\n"
    "SELECT name FROM pragma_table_list;\n"
)

TESTS = [
    {"id": "t1",
     "name": "three relationships are listed, with products appearing twice as the referencing table",
     "args": [], "expected": []},
    {"id": "t2", "hidden": True,
     "name": "hidden: categories and suppliers declare no keys and so appear only as referenced tables",
     "args": [], "expected": []},
]


def main() -> int:
    data = json.loads(PATH.read_text(encoding="utf-8"))
    fixed = 0

    for question in data["questions"]:
        spec = question.get("code")
        if not spec or spec.get("language") != "sql":
            continue
        if "reporting each table's foreign keys" not in question.get("prompt", ""):
            continue
        question["prompt"] = PROMPT
        question["explanation"] = EXPLANATION
        spec["instructionsMarkdown"] = INSTRUCTIONS
        spec["starterCode"] = STARTER
        spec["tests"] = TESTS
        fixed += 1

    PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8", newline="\n")
    print(f"  reframed {fixed} question(s)")
    return 0 if fixed == 1 else 1


if __name__ == "__main__":
    raise SystemExit(main())
