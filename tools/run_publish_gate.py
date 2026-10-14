#!/usr/bin/env python3
"""Run the repo's own publish gate against a reworked track, for any track slug.

Generalised from `run_fullstack_publish_gate.py`, whose comments record why each step is the way it is.
Those reasons apply to every track, so they are stated once here and the tool takes the slug as an argument.

The four things that had to be established, each wrong on a first attempt:

1. **The gate only checks `coming_soon` tracks.** Its first act is to look for that exact string and skip
   anything else, reporting "already available". Every reworked track is `available`, so it validates
   nothing unless a probe is built.
2. **The container does not read the host's content directory.** `/app` is baked into the image; the only
   mount is `/app/uploads`. Staging to `/srv/certivo/content/...` changes nothing the gate can see, so the
   levels are pushed into the running container and read from there.
3. **`/tmp` is not shared either.** A tar written to the VPS's `/tmp` is not visible inside the container.
   `docker cp` is what moves bytes between the two filesystems.
4. **`docker exec` without `-i` detaches stdin**, so `tar -xf` reports success while extracting nothing.

The probe's `slug` inside `track.json` is left as the real one: the schema constrains it, and an invalid slug
is rejected as `track.json invalid: Invalid` before a single level is examined. Only `status` is rewritten.
"""

from __future__ import annotations

import argparse
import subprocess
import sys

HOST = "admin-certivo@139.190.97.11"
CONTAINER = "certivo-app-1"
PROBE_SUFFIX = "__probe"


def ssh(command: str, timeout: int = 900) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=25", HOST, command],
        capture_output=True, text=True, encoding="utf-8", timeout=timeout,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the publish gate against a track.")
    parser.add_argument("slug")
    parser.add_argument("--expected-questions", type=int, default=500)
    args = parser.parse_args()

    slug = args.slug
    probe = f"{slug}{PROBE_SUFFIX}"

    build = ssh(
        f"set -e; "
        f"docker exec {CONTAINER} rm -rf /app/content/tracks/{probe}; "
        f"docker exec {CONTAINER} cp -a /app/content/tracks/{slug} "
        f"/app/content/tracks/{probe}; "
        f"docker exec {CONTAINER} sed -i 's/\"available\"/\"coming_soon\"/' "
        f"/app/content/tracks/{probe}/track.json; "
        f"docker exec {CONTAINER} grep -o coming_soon "
        f"/app/content/tracks/{probe}/track.json; "
        f"docker exec {CONTAINER} node -e \""
        f"const fs=require('fs'),p='/app/content/tracks/{slug}/levels/';"
        f"console.log(fs.readdirSync(p).filter(f=>f.endsWith('.json'))"
        f".reduce((s,f)=>s+JSON.parse(fs.readFileSync(p+f,'utf8')).questions.length,0))\""
    )
    if build.returncode != 0:
        print(f"  probe setup failed: {(build.stdout or '') + (build.stderr or '')}")
        ssh(f"docker exec {CONTAINER} rm -rf /app/content/tracks/{probe}")
        return 1
    values = build.stdout.split()
    print(f"  {slug}: probe built ({values[0]} status marker), container holds {values[-1]} question(s)")
    if int(values[-1]) != args.expected_questions:
        print(f"  REFUSING: expected {args.expected_questions} questions, the container holds {values[-1]}")
        ssh(f"docker exec {CONTAINER} rm -rf /app/content/tracks/{probe}")
        return 1

    gate_ok = False
    try:
        run = ssh(
            f"docker exec {CONTAINER} sh -c "
            f"'cd /app && node_modules/.bin/tsx content/scripts/publish-tracks.ts --dry-run {probe}'"
        )
        output = ((run.stdout or "") + (run.stderr or "")).strip()
        for line in output.splitlines():
            print(f"  {line}")
        gate_ok = run.returncode == 0 and f"+ {probe}" in output
        print()
        print(f"  publish gate verdict for {slug}: {'PASSES' if gate_ok else 'FAILS'}")
    finally:
        cleanup = ssh(f"docker exec {CONTAINER} rm -rf /app/content/tracks/{probe}")
        print(f"  probe removed: {cleanup.returncode == 0}")

    return 0 if gate_ok else 1


if __name__ == "__main__":
    sys.exit(main())
