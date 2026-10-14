#!/usr/bin/env python3
"""Apply a generated seed file to the Certivo database, reporting what actually changed.

The seed is 30 MB of `INSERT ... ON CONFLICT DO UPDATE`, so re-running it is safe and idempotent -- which is
also why a *silent* failure is dangerous: the statements execute, psql reports success, and the row counts stay
exactly as they were. That is what happened three times while chasing this, for three different reasons:

1. `SEED_OUT=1` was read as a **path**, not a flag. `POSTGRES_OUT_FILE = process.env.SEED_OUT ?? ...` so the
   build wrote 46 MB to a file named `1` and never touched `db/seed.postgres.sql`. The build still printed
   `Wrote 69537 Postgres statements to 1`, which reads like success.
2. `docker cp` was chained after a build command that had failed, so the host kept a stale seed file.
3. `cd /srv/certivo && docker exec ... < file` was run under PowerShell 5.1, where `&&` is not a statement
   separator and `<` is reserved. The command never ran at all.

The lesson each time was the same: **check the artefact, not the exit code.** So this reads the row counts
before and after and refuses to call the seed successful unless they moved, or unless the content was already
current.

The SQL is piped to `psql` over stdin. It never becomes a shell argument, which removes the quoting layer that
caused failure 3.
"""

from __future__ import annotations

import argparse
import subprocess
import sys

HOST = "admin-certivo@139.190.97.11"
DB = "certivo-db-1"
APP = "certivo-app-1"
SLUG = "data-scientist"

COUNTS = f"""
SELECT l.track_id || '|' || count(*) || '|'
       || count(*) FILTER (WHERE q.kind = 'multiple_choice') || '|'
       || count(*) FILTER (WHERE q.kind = 'code') || '|'
       || count(*) FILTER (WHERE q.kind = 'essay') || '|'
       || count(q.prompt_markdown_id)
FROM questions q
JOIN exams e ON q.exam_id = e.id
JOIN levels l ON e.level_id = l.id
WHERE l.track_id = '{SLUG}'
GROUP BY l.track_id
"""

OPTIONS = f"""
SELECT count(*) || '|' || count(o.option_text_id)
FROM question_options o
JOIN questions q ON o.question_id = q.id
JOIN exams e ON q.exam_id = e.id
JOIN levels l ON e.level_id = l.id
WHERE l.track_id = '{SLUG}'
"""

# The seed is not only questions. A run that adds 78 badge definitions and changes no question rows was
# reported as "NOTHING CHANGED" by the first version of this tool, which watched question counts alone --
# the same class of blind spot as the 397-versus-500 seed it was written to catch.
BADGES = """
SELECT count(*) || '|' || count(*) FILTER (WHERE track_id IS NULL)
       || '|' || count(*) FILTER (WHERE id ~ '\\.L[0-9]+$')
       || '|' || count(*) FILTER (WHERE id LIKE '%.graduate')
       || '|' || count(DISTINCT criteria_type)
FROM badges
"""


def ssh(command: str, stdin: bytes | None = None, timeout: int = 3600) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=25", HOST, command],
        input=stdin, capture_output=True, timeout=timeout,
    )


def query(sql: str, timeout: int = 600) -> str:
    result = ssh(f"docker exec -i {DB} psql -U certivo -d certivo -t -A -F'|'", sql.encode(), timeout)
    return (result.stdout or b"").decode("utf-8", "replace").strip()


def counts() -> tuple[str, str, str]:
    return query(COUNTS), query(OPTIONS), query(BADGES)


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply the generated seed to the database.")
    parser.add_argument("--sql", default="/srv/certivo/db/seed.postgres.sql")
    parser.add_argument("--force", action="store_true", help="seed even if the counts already look right")
    args = parser.parse_args()

    print("  === before ===")
    before_questions, before_options, before_badges = counts()
    print(f"    questions: {before_questions or '(none)'}")
    print(f"    options  : {before_options or '(none)'}   (questions|with Indonesian)")
    print(f"    badges   : {before_badges or '(none)'}   (total|authored|level|graduate|types)")

    print()
    print(f"  reading {args.sql} from the VPS")
    read = ssh(f"cat {args.sql}")
    if read.returncode != 0 or not read.stdout:
        print(f"    FAILED to read: {(read.stderr or b'').decode('utf-8', 'replace')[:300]}")
        return 1
    sql = read.stdout
    print(f"    {len(sql) // 1024 // 1024} MB")

    expected = sql.count(b"INSERT INTO questions (")
    print(f"    {expected} question INSERT statement(s) in the file")

    # Cheap sanity check before touching the database: the whole point of this exercise was that the seed had
    # 397 question rows when the content held 500.
    ds_rows = sum(1 for line in sql.split(b"\n")
                  if line.startswith(b"INSERT INTO questions (") and b"'data-scientist." in line)
    print(f"    {ds_rows} of them for {SLUG}")
    if ds_rows < 500 and not args.force:
        print(f"    REFUSING: expected at least 500 {SLUG} questions in the seed, found {ds_rows}")
        print("    rebuild first: tools/vps.py shell 'docker exec certivo-app-1 sh -c \"cd /app && "
              "node_modules/.bin/tsx content/scripts/build-seed.ts\"'")
        return 1

    print()
    print("  === applying (this takes a minute) ===")
    result = ssh(f"docker exec -i {DB} psql -U certivo -d certivo -v ON_ERROR_STOP=1 -q",
                 sql, timeout=3600)
    err = (result.stderr or b"").decode("utf-8", "replace")
    errors = [line for line in err.splitlines() if "ERROR" in line or "FATAL" in line]
    if errors:
        print("    SQL errors:")
        for line in errors[:10]:
            print(f"      {line}")
        return 1
    print(f"    psql exit {result.returncode}")

    print()
    print("  === after ===")
    after_questions, after_options, after_badges = counts()
    print(f"    questions: {after_questions or '(none)'}")
    print(f"    options  : {after_options or '(none)'}   (questions|with Indonesian)")
    print(f"    badges   : {after_badges or '(none)'}   (total|authored|level|graduate|types)")

    print()
    moved = [name for name, before, after in
             (("questions", before_questions, after_questions),
              ("options", before_options, after_options),
              ("badges", before_badges, after_badges))
             if before != after]
    if not moved:
        print("    NOTHING CHANGED in questions, options or badges. Either the seed matches what was")
        print("    already applied, or it was built from stale content -- check the container copy first.")
        return 1
    print(f"    moved: {', '.join(moved)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
