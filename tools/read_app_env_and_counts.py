#!/usr/bin/env python3
"""Read the app container's environment and entrypoint, and count the live content rows.

The previous probe established that `build-seed.ts` writes `db/seed.postgres.sql` for "the self-hosted
Node/VPS deployment", which is the deployment in front of us. What it did not establish is whether the
running container has already been seeded, and with what -- which is the difference between "the database
holds the originals" and "the database holds the rework".

`role "postgres" does not exist` means the credentials are the container's business, so they are read from
its environment rather than assumed.
"""

from __future__ import annotations

import subprocess
import sys

HOST = "admin-certivo@139.190.97.11"
CONTAINER = "certivo-app-1"
DB = "certivo-db-1"


def ssh(command: str, timeout: int = 300) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=25", HOST, command],
        capture_output=True, text=True, encoding="utf-8", timeout=timeout,
    )


def main() -> int:
    print("  === app container environment (database keys only) ===")
    env = ssh(
        f"docker exec {CONTAINER} sh -c 'env | grep -iE \"database|postgres|pg|drizzle\" | "
        f"sed -E \"s/(PASSWORD|SECRET|KEY)=.*/\\1=<redacted>/\"'"
    )
    for line in ((env.stdout or "") + (env.stderr or "")).splitlines():
        print(f"    {line}")

    print()
    print("  === container start command ===")
    info = ssh(
        f"docker inspect {CONTAINER} --format '{{{{json .Config.Cmd}}}} {{{{json .Config.Entrypoint}}}}'"
    )
    print(f"    {info.stdout.strip() or info.stderr.strip()}")

    print()
    print("  === db container environment (user/db only) ===")
    dbenv = ssh(
        f"docker exec {DB} sh -c 'env | grep -iE \"^POSTGRES\" | "
        f"sed -E \"s/(PASSWORD)=.*/\\1=<redacted>/\"'"
    )
    user = "postgres"
    database = "postgres"
    for line in ((dbenv.stdout or "") + (dbenv.stderr or "")).splitlines():
        print(f"    {line}")
        if line.startswith("POSTGRES_USER="):
            user = line.split("=", 1)[1]
        if line.startswith("POSTGRES_DB="):
            database = line.split("=", 1)[1]

    print()
    print(f"  === live content rows (user={user}, db={database}) ===")
    query = (
        "SELECT 'tracks=' || (SELECT count(*) FROM tracks) || "
        "' levels=' || (SELECT count(*) FROM levels) || "
        "' questions=' || (SELECT count(*) FROM questions) || "
        "' options=' || (SELECT count(*) FROM question_options);"
    )
    result = ssh(f"docker exec {DB} psql -U {user} -d {database} -t -c \"{query}\"")
    text = ((result.stdout or "") + (result.stderr or "")).strip()
    for line in text.splitlines()[:6]:
        print(f"    {line}")

    print()
    print("  === questions per track ===")
    per_track = ssh(
        f"docker exec {DB} psql -U {user} -d {database} -t -c \""
        f"SELECT t.slug, count(q.id) FROM tracks t "
        f"JOIN levels l ON l.track_id = t.id "
        f"JOIN questions q ON q.level_id = l.id "
        f"WHERE t.slug IN ('fullstack-developer','ai-ml-engineer') "
        f"GROUP BY t.slug ORDER BY t.slug;\""
    )
    for line in ((per_track.stdout or "") + (per_track.stderr or "")).splitlines()[:8]:
        print(f"    {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
