#!/usr/bin/env python3
"""Stage the reworked Fullstack Developer levels onto the VPS.

Two things make this more than a copy.

**Chunked base64 over the pipe.** The whole set is 786 KB, which becomes about 1.05 MB of base64 -- far more
than can be passed as an SSH command argument, where the shell's quoting is fragile and the argument length
is limited. The archive is therefore streamed as a sequence of self-contained `base64 -d` appends, each
small enough to be a single command, with `set -e` on the remote side so a decode failure stops the run
rather than leaving a truncated archive behind.

**A backup before anything is replaced.** The VPS currently holds the *originals* -- 18 to 22 multiple-choice
questions per level with at most one code question, which is the content being replaced. They are archived
first, because the whole point of this rework is that the originals were unusable and there is no other copy
of them.
"""

from __future__ import annotations

import base64
import subprocess
import sys
import tarfile
import time
from pathlib import Path

ROOT = Path(r"C:\Users\arian\OneDrive\ドキュメント\Myriaxon")
LEVELS = ROOT / "certivo-rework" / "fullstack-developer" / "levels"
ARCHIVE = ROOT / "certivo-rework" / ".staging-fullstack.tar.gz"

HOST = "admin-certivo@139.190.97.11"
REMOTE_ROOT = "/srv/certivo"
REMOTE_LEVELS = f"{REMOTE_ROOT}/content/tracks/fullstack-developer/levels"
CHUNK = 40000


def ssh(command: str, timeout: int = 180, stdin: bytes | None = None,
        timeout_ms: int | None = None) -> subprocess.CompletedProcess:
    """Run a command on the VPS, optionally feeding bytes to its stdin.

    The archive goes through stdin rather than as a command argument because Windows caps a command line at
    roughly 32 KB and `CreateProcess` fails outright beyond it -- which is how the first attempt at this
    ended, with `WinError 206`. base64 of the archive is about 1.05 MB, so no chunk size makes it an
    argument; a pipe has no such limit.
    """
    if timeout_ms is not None:
        timeout = timeout_ms
    return subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=25", HOST, command],
        input=stdin, capture_output=True, timeout=timeout,
        text=stdin is None, encoding="utf-8" if stdin is None else None,
    )


def pack() -> bytes:
    with tarfile.open(ARCHIVE, "w:gz") as tar:
        for path in sorted(LEVELS.glob("*.json")):
            tar.add(path, arcname=path.name)
    return ARCHIVE.read_bytes()


def main() -> int:
    files = sorted(LEVELS.glob("*.json"))
    if len(files) != 20:
        print(f"  REFUSING: expected 20 level files, found {len(files)}")
        return 1

    stamp = time.strftime("%Y%m%dT%H%M%S")
    backup = f"{REMOTE_ROOT}/backups/fullstack-developer-{stamp}"

    result = ssh(
        f"set -e; mkdir -p {backup}; "
        f"cp -a {REMOTE_LEVELS}/*.json {backup}/; "
        f"ls {backup} | wc -l"
    )
    if result.returncode != 0:
        print(f"  backup failed: {result.stderr.strip()[:300]}")
        return 1
    print(f"  backed up {result.stdout.strip()} original file(s) to {backup}")

    blob = pack()
    remote_tar = "/tmp/myriaxon-fullstack.tar.gz"
    # One pipe, no chunks and no argument-length limit.
    result = ssh(f"set -e; cat > {remote_tar}; ls -l {remote_tar} | awk '{{print $5}}'", stdin=blob)
    if result.returncode != 0:
        print(f"  transfer failed: {result.stderr.decode('utf-8', 'replace').strip()[:300]}")
        return 1
    landed = result.stdout.decode("utf-8", "replace").strip()
    print(f"  streamed {len(blob) // 1024} KB; the VPS reports {int(landed) // 1024} KB")
    if int(landed) != len(blob):
        print(f"  REFUSING to extract: {landed} bytes arrived, {len(blob)} were sent")
        return 1

    result = ssh(
        f"set -e; mkdir -p {REMOTE_LEVELS}; "
        f"tar -xzf {remote_tar} -C {REMOTE_LEVELS}; "
        f"rm -f {remote_tar}; "
        f"ls {REMOTE_LEVELS}/*.json | wc -l"
    )
    if result.returncode != 0:
        print(f"  extract failed: {result.stderr.strip()[:400]}")
        return 1
    print(f"  extracted; {result.stdout.strip()} file(s) now in place")

    # Read back what actually landed rather than trusting the copy.
    verify = ssh(
        "cd /srv/certivo && python3 -c \""
        "import json, glob, os;"
        "fs = sorted(glob.glob('content/tracks/fullstack-developer/levels/*.json'));"
        "bad = [];"
        "\nfor f in fs:\n"
        "    d = json.load(open(f, encoding='utf-8'))\n"
        "    q = d.get('questions', [])\n"
        "    mc = sum(1 for x in q if 'options' in x)\n"
        "    code = sum(1 for x in q if 'code' in x)\n"
        "    essay = sum(1 for x in q if 'essay' in x)\n"
        "    if len(q) != 25 or (mc, code, essay) not in ((21, 4, 0), (18, 4, 3)):\n"
        "        bad.append((os.path.basename(f), len(q), mc, code, essay))\n"
        "print(f'  {len(fs)} file(s); shape mismatches: {len(bad)}')\n"
        "for b in bad: print('   ', b)\n"
        "\""
    )
    print(verify.stdout.rstrip() or verify.stderr.strip()[:400])
    return 0 if verify.returncode == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
