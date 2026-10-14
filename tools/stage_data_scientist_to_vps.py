#!/usr/bin/env python3
"""Stage the reworked Data Scientist levels onto the VPS for the publish gate.

Same two mechanics as the Fullstack staging, and both were learned the hard way there:

* The archive is piped through stdin rather than passed as a command argument, because Windows caps a command
  line at roughly 32 KB and `CreateProcess` fails outright beyond it.
* `docker cp` moves the archive between the host filesystem and the container's. Writing it to the VPS's
  `/tmp` is not enough, because the container has its own `/tmp`, and `docker exec -i` piping extracts
  nothing at all.

The levels are copied into the running container so the gate reads the reworked content rather than the
content the image was built with. That copy is deliberately not persisted.
"""

from __future__ import annotations

import io
import subprocess
import sys
import tarfile
import time
from pathlib import Path

ROOT = Path(r"C:\Users\arian\OneDrive\ドキュメント\Myriaxon")
LOCAL_LEVELS = ROOT / "certivo-rework" / "data-scientist" / "levels"

HOST = "admin-certivo@139.190.97.11"
CONTAINER = "certivo-app-1"
SLUG = "data-scientist"
REMOTE_TAR = "/tmp/myriaxon-ds-levels.tar"


def ssh(command: str, timeout: int = 900, stdin: bytes | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=25", HOST, command],
        input=stdin, capture_output=True, timeout=timeout,
        text=stdin is None, encoding="utf-8" if stdin is None else None,
    )


def main() -> int:
    levels = sorted(LOCAL_LEVELS.glob("*.json"))
    if len(levels) != 20:
        print(f"  REFUSING: expected 20 level files, found {len(levels)}")
        return 1

    stamp = time.strftime("%Y%m%dT%H%M%S")
    backup = f"/srv/certivo/backups/{SLUG}-{stamp}"
    result = ssh(
        f"set -e; mkdir -p {backup}; "
        f"cp -a /app/content/tracks/{SLUG}/levels/*.json {backup}/ 2>/dev/null || "
        f"cp -a /srv/certivo/content/tracks/{SLUG}/levels/*.json {backup}/; "
        f"ls {backup} | wc -l"
    )
    if result.returncode != 0:
        print(f"  backup failed: {result.stderr.strip()[:300]}")
        return 1
    print(f"  backed up {result.stdout.strip()} original file(s) to {backup}")

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for path in levels:
            tar.add(path, arcname=path.name)
    blob = buffer.getvalue()

    put = ssh(f"cat > {REMOTE_TAR}; stat -c %s {REMOTE_TAR}", stdin=blob)
    if put.returncode != 0 or int(put.stdout.strip()) != len(blob):
        print(f"  upload failed: {put.stderr.strip()[:300]}")
        return 1
    print(f"  uploaded {len(blob) // 1024} KB to the VPS")

    copy = ssh(f"docker cp {REMOTE_TAR} {CONTAINER}:/tmp/levels.tar")
    if copy.returncode != 0:
        print(f"  docker cp failed: {copy.stderr.strip()[:300]}")
        return 1

    setup = ssh(
        f"set -e; "
        f"docker exec -i {CONTAINER} tar -xf /tmp/levels.tar "
        f"-C /app/content/tracks/{SLUG}/levels; "
        f"docker exec {CONTAINER} rm -f /tmp/levels.tar; "
        f"docker exec {CONTAINER} node -e \""
        f"const fs=require('fs'),p='/app/content/tracks/{SLUG}/levels/';"
        f"const n=fs.readdirSync(p).filter(f=>f.endsWith('.json'))"
        f".reduce((s,f)=>s+JSON.parse(fs.readFileSync(p+f,'utf8')).questions.length,0);"
        f"console.log(n)\""
    )
    if setup.returncode != 0:
        print(f"  staging failed: {(setup.stdout or '') + (setup.stderr or '')}")
        return 1
    merged = setup.stdout.strip().splitlines()[-1]
    print(f"  container now holds {merged} question(s) for {SLUG}")

    ssh(f"rm -f {REMOTE_TAR}")
    print(f"  staged. Run tools/run_fullstack_publish_gate.py pointed at {SLUG} to validate it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
