#!/usr/bin/env python3
"""Read the seed script and the live database, to settle how content actually reaches learners.

Follow-up to the previous probe. Two candidate paths exist and only one of them is real:

  * the API reads content files from `/app/content` at request time, in which case the image is the
    deployment and the host's content directory is decoration; or
  * a seed step compiles `/app/content` into Postgres rows, in which case the image is the *input* and the
    seed is the deployment.

`db/seed.postgres.sql` and `content/scripts/build-seed.ts` both exist, which points at the second. This
prints the seed script's shape and the database's actual row counts so the answer comes from evidence.
"""

from __future__ import annotations

import subprocess
import sys

HOST = "admin-certivo@139.190.97.11"
CONTAINER = "certivo-app-1"


def ssh(command: str, timeout: int = 300) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=25", HOST, command],
        capture_output=True, text=True, encoding="utf-8", timeout=timeout,
    )


def container(command: str) -> str:
    result = ssh(f"docker exec {CONTAINER} sh -c '{command}'")
    return ((result.stdout or "") + (result.stderr or "")).strip()


SECTIONS: list[tuple[str, str]] = [
    ("build-seed.ts header",
     "head -45 /app/content/scripts/build-seed.ts"),
    ("seed.postgres.sql size and table list",
     "wc -l /app/db/seed.postgres.sql; grep -o 'INSERT INTO [a-z_]*' /app/db/seed.postgres.sql "
     "| sort | uniq -c | sort -rn | head -12"),
    ("how the app starts",
     "cat /app/entrypoint.sh 2>/dev/null || ls -1 /app/*.sh 2>/dev/null || echo 'no shell entrypoint'"),
]


def main() -> int:
    for title, command in SECTIONS:
        print(f"  === {title} ===")
        for line in container(command).splitlines()[:24]:
            print(f"    {line}")
        print()

    print("  === live database row counts ===")
    query = (
        "SELECT relname, n_live_tup FROM pg_stat_user_tables "
        "WHERE n_live_tup > 0 ORDER BY n_live_tup DESC LIMIT 12;"
    )
    result = ssh(
        f"docker exec certivo-db-1 psql -U postgres -d postgres -t -c \"{query}\""
    )
    text = ((result.stdout or "") + (result.stderr or "")).strip()
    for line in text.splitlines()[:14]:
        print(f"    {line}")
    if not text:
        print("    (no output -- checking the database name)")
        names = ssh("docker exec certivo-db-1 psql -U postgres -lqt")
        for line in ((names.stdout or "") + (names.stderr or "")).splitlines()[:8]:
            print(f"    {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
