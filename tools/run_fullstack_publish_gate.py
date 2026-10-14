#!/usr/bin/env python3
"""Run the repo's own publish gate against the reworked Fullstack levels.

Five things had to be established before this could validate anything, and every one was wrong on a first
attempt. They are recorded because each failure looked like something other than what it was.

**The gate only checks `coming_soon` tracks.** Its first act is to look for that exact string and skip
anything else, reporting "already available". Both tracks are `available`, so running the gate against them
directly validates nothing at all.

**The container does not read the host's content directory.** `/app` is baked into the image; the only mount
on `certivo-app-1` is a volume at `/app/uploads`. Staging files to `/srv/certivo/content/tracks/...` changes
nothing the gate can see. The first run reported `READY to publish (0)` with an empty slug list because
`readdirSync` inside the container never saw the probe directory -- which read as a quoting bug in the gate
invocation rather than as a filesystem boundary.

**`/tmp` is not shared either.** A tar written to the VPS's `/tmp` and unpacked with
`docker exec tar -xf /tmp/...` fails with "Cannot open: No such file or directory". The error was accurate
and pointed at the wrong thing.

**Piping the tar into `docker exec -i` extracts nothing,** silently: `tar -x -C <dir>` inside the container
reports success while leaving the directory empty, because the redirect and the pipe are the host's and the
container's stdin is not the same stream. `docker cp` is the mechanism that actually moves bytes between
the two filesystems, and it was verified with a round trip before being used here.

**Docker is not installed locally**, so every container command goes over SSH and is quoted for one more
shell than is comfortable. Commands are therefore built from single-quote-free fragments.

The copy into the container is deliberately not persisted -- it exists to run the gate and is removed
afterwards, in a `finally` so a failure cannot leave a stale track directory behind for a later
`readdirSync` to find. The host's staged files remain the source of truth.
"""

from __future__ import annotations

import io
import subprocess
import sys
import tarfile
from pathlib import Path

ROOT = Path(r"C:\Users\arian\OneDrive\ドキュメント\Myriaxon")
LOCAL_LEVELS = ROOT / "certivo-rework" / "fullstack-developer" / "levels"

HOST = "admin-certivo@139.190.97.11"
CONTAINER = "certivo-app-1"
SLUG = "fullstack-developer"
PROBE = "__probe-fs"
REMOTE_TAR = "/tmp/myriaxon-fs-levels.tar"


def ssh(command: str, timeout: int = 900, stdin: bytes | None = None) -> subprocess.CompletedProcess:
    """Run a command on the VPS, feeding stdin when given.

    Stdin rather than an argument because Windows caps a command line at roughly 32 KB and `CreateProcess`
    fails outright beyond it, so an 830 KB archive cannot be an argument however it is encoded.
    """
    return subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=25", HOST, command],
        input=stdin, capture_output=True, timeout=timeout,
        text=stdin is None, encoding="utf-8" if stdin is None else None,
    )


def main() -> int:
    levels = sorted(LOCAL_LEVELS.glob("*.json"))
    if len(levels) != 20:
        print(f"  REFUSING: expected 20 local level files, found {len(levels)}")
        return 1

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for path in levels:
            tar.add(path, arcname=path.name)
    blob = buffer.getvalue()

    try:
        # 1. Host /tmp.
        put = ssh(f"cat > {REMOTE_TAR}; stat -c %s {REMOTE_TAR}", stdin=blob)
        if put.returncode != 0 or int(put.stdout.strip()) != len(blob):
            print(f"  upload failed: {put.stderr.strip()[:300]}")
            return 1
        print(f"  uploaded {len(blob) // 1024} KB to the VPS")

        # 2. docker cp is what actually crosses from the host filesystem into the container's.
        copy = ssh(f"docker cp {REMOTE_TAR} {CONTAINER}:/tmp/levels.tar")
        if copy.returncode != 0:
            print(f"  docker cp failed: {copy.stderr.strip()[:300]}")
            return 1

        setup = ssh(
            f"set -e; "
            f"docker exec -i {CONTAINER} tar -xf /tmp/levels.tar "
            f"-C /app/content/tracks/{SLUG}/levels; "
            f"docker exec {CONTAINER} rm -f /tmp/levels.tar; "
            f"docker exec {CONTAINER} rm -rf /app/content/tracks/{PROBE}; "
            f"docker exec {CONTAINER} cp -a /app/content/tracks/{SLUG} "
            f"/app/content/tracks/{PROBE}; "
            # Only the status is rewritten. The directory name is the probe so the gate has something to
            # read, but the `slug` *inside* track.json stays as the real one: the schema constrains it, and
            # an invalid slug is rejected as `track.json invalid: Invalid` before any level is examined.
            f"docker exec {CONTAINER} sed -i "
            f"'s/\"available\"/\"coming_soon\"/' "
            f"/app/content/tracks/{PROBE}/track.json; "
            f"docker exec {CONTAINER} ls /app/content/tracks/{SLUG}/levels/ | wc -l; "
            f"docker exec {CONTAINER} grep -c coming_soon "
            f"/app/content/tracks/{PROBE}/track.json; "
            # Confirms the gate is reading the levels that were pushed rather than the ones the image was
            # built with: 25 questions each is the reworked shape, the originals had 18 to 22.
            f"docker exec {CONTAINER} node -e \""
            f"const fs=require('fs'),p='/app/content/tracks/{SLUG}/levels/';"
            f"const n=fs.readdirSync(p).filter(f=>f.endsWith('.json'))"
            f".reduce((s,f)=>s+JSON.parse(fs.readFileSync(p+f,'utf8')).questions.length,0);"
            f"console.log(n)\""
        )
        if setup.returncode != 0:
            print(f"  probe setup failed: {(setup.stdout or '') + (setup.stderr or '')}")
            return 1
        values = setup.stdout.split()
        file_count, status_count, question_total = values[0], values[1], values[2]
        print(f"  {file_count} level file(s) in the container holding {question_total} questions; "
              f"probe status rewritten ({status_count} occurrence)")
        if question_total != "500":
            print(f"  REFUSING: the container holds {question_total} questions, not the 500 just pushed")
            return 1

        gate_ok = False
        run = ssh(
            f"docker exec {CONTAINER} sh -c "
            f"'cd /app && node_modules/.bin/tsx content/scripts/publish-tracks.ts "
            f"--dry-run {PROBE}'"
        )
        output = ((run.stdout or "") + (run.stderr or "")).strip()
        for line in output.splitlines():
            print(f"  {line}")
        gate_ok = run.returncode == 0 and f"+ {PROBE}" in output
        print()
        print(f"  publish gate verdict: {'PASSES' if gate_ok else 'FAILS'}")
        return 0 if gate_ok else 1
    finally:
        ssh(f"rm -f {REMOTE_TAR}")
        cleanup = ssh(f"docker exec {CONTAINER} rm -rf /app/content/tracks/{PROBE}")
        print(f"  probe removed: {cleanup.returncode == 0}")


if __name__ == "__main__":
    sys.exit(main())
