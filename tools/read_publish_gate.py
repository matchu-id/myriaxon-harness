#!/usr/bin/env python3
"""Read the repo's publish gate so its behaviour is known before relying on it.

This exists because `cat file` inside a PowerShell here-string sent over SSH picks up a carriage return on
the final line, so the delimiter arrives as `'EOF'\r` and the shell reports the file -- and then several
others in the same heredoc -- as missing. The output of that attempt looked like a missing file rather than
a quoting problem. Python reading the file directly avoids the whole question.
"""

from __future__ import annotations

import subprocess
import sys

HOST = "admin-certivo@139.190.97.11"


def main() -> int:
    remote = (
        "cd /srv/certivo && python3 -c \""
        "import sys;"
        "print(open('content/scripts/publish-tracks.ts', encoding='utf-8').read())\""
    )
    result = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=25", HOST, remote],
        capture_output=True, text=True, encoding="utf-8", timeout=120,
    )
    if result.returncode != 0:
        print(result.stderr.strip()[:400])
        return 1
    for line in result.stdout.splitlines():
        print(f"  {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
