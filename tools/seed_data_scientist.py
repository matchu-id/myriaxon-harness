#!/usr/bin/env python3
"""Seed the reworked Data Scientist track, verifying each step before the next one runs.

Order, and why each step is where it is:

1. **Check what is live now.** The row counts before the seed are the only baseline against which "did the
   seed work?" can be answered. Without them a seed that silently did nothing looks the same as one that
   replaced everything.
2. **Confirm the generated SQL actually carries the Indonesian.** The build writes `db/seed.postgres.sql`;
   grepping it for a populated `prompt_markdown_id` proves the localisation reached the SQL rather than being
   dropped on the way. An earlier staging attempt reported 500 questions with `withId: 0`, which is the failure
   this check exists for.
3. **Seed, then re-count.** The verification is row counts *and* the number of non-null Indonesian columns,
   because a seed that replaced the questions but lost the translations would pass a question count.

Every remote command is one line joined with `&&`, since a PowerShell here-string picks up a trailing carriage
return that breaks the shell (`url: 'file:///app/content/scripts/build-seed.ts%0D'`).
"""

from __future__ import annotations

import json
import subprocess
import sys

HOST = "admin-certivo@139.190.97.11"
CONTAINER = "certivo-app-1"
DB = "certivo-db-1"
SLUG = "data-scientist"

PSQL = (f"docker exec {DB} psql -U certivo -d certivo -t -A -F'|' -c ")


def ssh(command: str, timeout: int = 900) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=25", HOST, command],
        capture_output=True, text=True, encoding="utf-8", timeout=timeout,
    )


def query(sql: str) -> str:
    """Run one query by piping it to psql over stdin, which avoids every layer of shell quoting."""
    result = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=25", HOST,
         f"docker exec -i {DB} psql -U certivo -d certivo -t -A -F'|'"],
        input=sql.encode("utf-8"), capture_output=True, timeout=600,
    )
    return (result.stdout or b"").decode("utf-8", "replace").strip()


COUNTS = f"""
SELECT l.track_id,
  count(*) AS questions,
  count(*) FILTER (WHERE q.kind = 'multiple_choice') AS mc,
  count(*) FILTER (WHERE q.kind = 'code') AS code,
  count(*) FILTER (WHERE q.kind = 'essay') AS essay,
  count(q.prompt_markdown_id) AS id_prompts
FROM questions q
JOIN exams e ON q.exam_id = e.id
JOIN levels l ON e.level_id = l.id
WHERE l.track_id = '{SLUG}'
GROUP BY l.track_id
"""

OPTIONS = f"""
SELECT count(*) AS options, count(o.option_text_id) AS id_options
FROM question_options o
JOIN questions q ON o.question_id = q.id
JOIN exams e ON q.exam_id = e.id
JOIN levels l ON e.level_id = l.id
WHERE l.track_id = '{SLUG}'
"""


def main() -> int:
    print("  === before ===")
    before = query(COUNTS)
    print(f"    {before or '(no rows)'}")
    print(f"    options: {query(OPTIONS) or '(no rows)'}")

    print()
    print("  === generated SQL carries the Indonesian? ===")
    check = ssh(
        f"cd /srv/certivo && ls -la db/seed.postgres.sql && "
        f"grep -c prompt_markdown_id db/seed.postgres.sql && "
        f"grep -c option_text_id db/seed.postgres.sql"
    )
    if check.returncode != 0:
        print(f"    FAILED: {(check.stdout or '') + (check.stderr or '')}")
        return 1
    for line in check.stdout.strip().splitlines():
        print(f"    {line}")

    print()
    print("  === seeding ===")
    seed = ssh(
        "cd /srv/certivo && "
        "docker exec certivo-app-1 sh -c 'cd /app && SEED_OUT=1 node_modules/.bin/tsx "
        "content/scripts/build-seed.ts' >/dev/null && "
        "docker cp certivo-app-1:/app/db/seed.postgres.sql /srv/certivo/db/seed.postgres.sql && "
        "docker exec -i certivo-db-1 psql -U certivo -d certivo -v ON_ERROR_STOP=1 "
        "< /srv/certivo/db/seed.postgres.sql 2>&1 | tail -5"
    )
    output = (seed.stdout or "") + (seed.stderr or "")
    for line in output.strip().splitlines()[-8:]:
        print(f"    {line}")
    if seed.returncode != 0:
        print("    SEED FAILED")
        return 1

    print()
    print("  === after ===")
    after = query(COUNTS)
    print(f"    {after or '(no rows)'}")
    print(f"    options: {query(OPTIONS) or '(no rows)'}")
    print()
    print("  a question count of 500 with id_prompts of 500 means both the rework and the")
    print("  Indonesian reached the database; id_prompts of 0 means the translations did not.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
