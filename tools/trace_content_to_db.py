#!/usr/bin/env python3
"""Find out how track content reaches the database, before deciding how to deploy it.

The previous round established that `/app/content` is baked into the app image and is not a mount, so
editing the host's `/srv/certivo/content` changes nothing the container reads. That raises the question this
answers: does the running application read content files at all, or does it read rows that a seed step
produced? The two have completely different deployment paths, and guessing between them would waste a round.

The publish gate's own comment names a seed script -- `build-seed.ts` "compiles track.json into D1" -- so
this looks for it, for how it is invoked, and for what the API actually queries.
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


SECTIONS: list[tuple[str, str]] = [
    ("seed / content scripts present", "ls -1 /app/content/scripts/"),
    ("package.json scripts",
     "node -e \"const p=require('/app/package.json');console.log(JSON.stringify(p.scripts,null,1))\""),
    ("does the API query questions from a database?",
     "grep -rl 'questions' /app/app/src --include=*.ts 2>/dev/null | head -8"),
    ("schema / migration files",
     "ls -1 /app/content/ 2>/dev/null; echo '---'; find /app -name '*.sql' -not -path '*/node_modules/*' "
     "2>/dev/null | head -10"),
    ("database the app connects to",
     "docker exec certivo-db-1 psql -U postgres -l 2>/dev/null | head -12 || echo 'psql unavailable'"),
]


def main() -> int:
    for title, command in SECTIONS:
        print(f"  === {title} ===")
        if command.startswith("docker exec"):
            result = ssh(command)
        else:
            result = ssh(f"docker exec {CONTAINER} sh -c '{command}'")
        text = ((result.stdout or "") + (result.stderr or "")).strip()
        for line in text.splitlines()[:16]:
            print(f"    {line}")
        if not text:
            print("    (nothing)")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
