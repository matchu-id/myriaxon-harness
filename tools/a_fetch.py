#!/usr/bin/env python3
"""Fetch Agent A's two files from the VPS over ssh and write them locally as UTF-8/LF.

PowerShell's Set-Content re-encodes to cp1252 and corrupts emoji, and vps.py's stdout is cp1252 on this box,
so nothing non-ASCII can round-trip through a console. This writes bytes to disk instead.
"""
import subprocess
import sys
from pathlib import Path

HOST = "admin-certivo@139.190.97.11"
OUT = Path(r"C:\Users\arian\OneDrive\ドキュメント\Myriaxon\certivo-rework\.agent-a")

FILES = {
    "badges.json": "/srv/certivo/content/badges.json",
    "build-seed.ts": "/srv/certivo/content/scripts/build-seed.ts",
}


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    for local, remote in FILES.items():
        r = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=25", HOST, f"cat {remote}"],
            capture_output=True, timeout=180,
        )
        if r.returncode != 0:
            print(f"FAILED {remote}: {r.stderr.decode('utf-8', 'replace')}", file=sys.stderr)
            return 1
        data = r.stdout
        # Normalise any CRLF the remote may hold; keep LF, keep bytes identical otherwise.
        data = data.replace(b"\r\n", b"\n")
        dest = OUT / local
        dest.write_bytes(data)
        print(f"{remote} -> {local} ({len(data)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
