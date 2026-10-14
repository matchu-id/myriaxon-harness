#!/usr/bin/env python3
"""Read the content schema's column names, then count each reworked track's live questions.

The first attempt at the count guessed `questions.level_id` and Postgres reported it does not exist. Rather
than guessing again, this reads the actual columns from `information_schema` and builds the join from what
is there -- the same reason the SQL code questions in these levels are verified by running them.
"""

from __future__ import annotations

import subprocess
import sys

HOST = "admin-certivo@139.190.97.11"
DB = "certivo-db-1"
USER = "certivo"
DATABASE = "certivo"


def psql(sql: str, timeout: int = 300) -> str:
    result = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=25", HOST,
         f"docker exec {DB} psql -U {USER} -d {DATABASE} -t -A -F'|' -c \"{sql}\""],
        capture_output=True, text=True, encoding="utf-8", timeout=timeout,
    )
    return ((result.stdout or "") + (result.stderr or "")).strip()


def main() -> int:
    print("  === columns of the content tables ===")
    columns = psql(
        "SELECT table_name || '.' || column_name FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name IN "
        "('tracks','levels','questions','question_options','exams','track_skills') "
        "ORDER BY table_name, ordinal_position;"
    )
    for line in columns.splitlines():
        print(f"    {line}")

    print()
    print("  === foreign keys among them ===")
    keys = psql(
        "SELECT tc.table_name || '.' || kcu.column_name || ' -> ' || "
        "ccu.table_name || '.' || ccu.column_name FROM information_schema.table_constraints tc "
        "JOIN information_schema.key_column_usage kcu ON kcu.constraint_name = tc.constraint_name "
        "JOIN information_schema.constraint_column_usage ccu ON ccu.constraint_name = tc.constraint_name "
        "WHERE tc.constraint_type='FOREIGN KEY' AND tc.table_schema='public' "
        "ORDER BY tc.table_name;"
    )
    for line in keys.splitlines():
        print(f"    {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
