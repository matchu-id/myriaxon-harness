#!/usr/bin/env python3
"""Upload Agent A's two files to the VPS.

Backs up the live files first, sends them as raw UTF-8 bytes over ssh stdin (never through a PowerShell
command line, which re-encodes to cp1252 and corrupts emoji and em-dashes), then reads each file back and
byte-compares it with the local copy so a truncated or mangled upload cannot pass unnoticed.
"""
import subprocess
import sys
from pathlib import Path

HOST = "admin-certivo@139.190.97.11"
BASE = Path(r"C:\Users\arian\OneDrive\ドキュメント\Myriaxon\certivo-rework\.agent-a")

FILES = {
    "badges.json": "/srv/certivo/content/badges.json",
    "build-seed.ts": "/srv/certivo/content/scripts/build-seed.ts",
}
BACKUP_SUFFIX = ".agent-a.bak"


def ssh(argv: str, payload: bytes | None = None, timeout: int = 300) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=25", HOST, argv],
        input=payload, capture_output=True, timeout=timeout,
    )


def main() -> int:
    for local, remote in FILES.items():
        data = (BASE / local).read_bytes()
        # Normalise to LF and guarantee a trailing newline.
        data = data.replace(b"\r\n", b"\n")
        if not data.endswith(b"\n"):
            data += b"\n"

        bak = ssh(f"cp -p {remote} {remote}{BACKUP_SUFFIX} && echo backed-up")
        if bak.returncode != 0:
            print(f"BACKUP FAILED for {remote}: {bak.stderr.decode('utf-8', 'replace')}", file=sys.stderr)
            return 1
        print(f"{remote}: {bak.stdout.decode().strip()} ({len(data)} bytes to write)")

        # `cat > path` reads the payload from stdin as exact bytes.
        up = ssh(f"cat > {remote}", payload=data)
        if up.returncode != 0:
            print(f"UPLOAD FAILED for {remote}: {up.stderr.decode('utf-8', 'replace')}", file=sys.stderr)
            return 1

        back = ssh(f"cat {remote}")
        if back.returncode != 0:
            print(f"READBACK FAILED for {remote}", file=sys.stderr)
            return 1
        if back.stdout != data:
            print(f"MISMATCH after upload: {remote} "
                  f"(wrote {len(data)} bytes, read back {len(back.stdout)})", file=sys.stderr)
            return 1
        print(f"  byte-identical readback OK ({len(back.stdout)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
