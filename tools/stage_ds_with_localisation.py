#!/usr/bin/env python3
"""Stage the reworked Data Scientist track -- English and Indonesian -- ready for seeding.

The order matters and each step was established earlier in this work:

1. **Back up what is live.** `/srv/certivo` is not a git repo, and the database currently holds the original
   397 questions. A backup of the level files is the only way back.
2. **Push the levels into the running container.** `/app` is baked into the image and the only mount is
   `/app/uploads`, so writing to `/srv/certivo/content` changes nothing the seeder can read. The archive goes
   over ssh stdin because Windows caps a command line at about 32 KB, and `docker cp` moves it between the
   host and the container's own filesystem -- the two `/tmp` directories are not shared.
3. **Verify what landed**, by counting questions in the container rather than trusting the upload.

Both `NN.json` and `NN.id.json` are staged: the seeder reads the `Id` fields from the same level file, so the
Indonesian travels with the English rather than in a separate artefact.
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
REMOTE_TAR = "/tmp/myriaxon-ds.tar"


def ssh(command: str, timeout: int = 900, stdin: bytes | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=25", HOST, command],
        input=stdin, capture_output=True, timeout=timeout,
        text=stdin is None, encoding="utf-8" if stdin is None else None,
    )


def main() -> int:
    english = sorted(p for p in LOCAL_LEVELS.glob("*.json") if not p.name.endswith(".id.json"))
    indonesian = sorted(LOCAL_LEVELS.glob("*.id.json"))
    if len(english) != 20:
        print(f"  REFUSING: expected 20 English levels, found {len(english)}")
        return 1
    if len(indonesian) != 20:
        print(f"  REFUSING: expected 20 Indonesian levels, found {len(indonesian)}")
        return 1
    print(f"  staging {len(english)} English + {len(indonesian)} Indonesian level file(s)")

    stamp = time.strftime("%Y%m%dT%H%M%S")
    backup = f"/srv/certivo/backups/{SLUG}-rework-{stamp}"
    result = ssh(f"mkdir -p {backup} && cp -a /srv/certivo/content/tracks/{SLUG}/levels/. {backup}/ "
                 f"&& ls {backup} | wc -l")
    if result.returncode != 0:
        print(f"  backup failed: {(result.stdout or '') + (result.stderr or '')}")
        return 1
    print(f"  backed up {result.stdout.strip()} file(s) to {backup}")

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for path in english + indonesian:
            tar.add(path, arcname=path.name)
    blob = buffer.getvalue()

    put = ssh(f"cat > {REMOTE_TAR}; stat -c %s {REMOTE_TAR}", stdin=blob)
    if put.returncode != 0 or int(put.stdout.strip()) != len(blob):
        print(f"  upload failed: {(put.stdout or '') + (put.stderr or '')}")
        return 1
    print(f"  uploaded {len(blob) // 1024} KB")

    if ssh(f"docker cp {REMOTE_TAR} {CONTAINER}:/tmp/levels.tar").returncode != 0:
        print("  docker cp failed")
        return 1

    setup = ssh(
        f"set -e; "
        f"docker exec -i {CONTAINER} tar -xf /tmp/levels.tar "
        f"-C /app/content/tracks/{SLUG}/levels; "
        f"docker exec {CONTAINER} rm -f /tmp/levels.tar; "
        f"docker exec {CONTAINER} node -e \""
        f"const fs=require('fs'),p='/app/content/tracks/{SLUG}/levels/';"
        f"const fs2=fs.readdirSync(p).filter(f=>f.endsWith('.json'));"
        f"const en=fs2.filter(f=>!f.endsWith('.id.json'));"
        f"const id=fs2.filter(f=>f.endsWith('.id.json'));"
        f"let q=0,withId=0,optId=0,opts=0;"
        f"for(const f of en){{const d=JSON.parse(fs.readFileSync(p+f,'utf8'));"
        f"for(const x of d.questions){{q++;"
        f"if(x.promptId)withId++;"
        f"for(const o of (x.options||[])){{opts++;if(o.textId)optId++;}}}}}}"
        f"console.log(JSON.stringify({{en:en.length,id:id.length,q,withId,opts,optId}}))\""
    )
    if setup.returncode != 0:
        print(f"  staging failed: {(setup.stdout or '') + (setup.stderr or '')}")
        return 1
    print(f"  container: {setup.stdout.strip()}")

    ssh(f"rm -f {REMOTE_TAR}")
    print(f"  backup path recorded: {backup}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
