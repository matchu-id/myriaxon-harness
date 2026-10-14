"""Store the Daytona key where a command line cannot leak it.

An API key passed as `--api-key` or exported inline lands in the process table, in PowerShell's
history, and in any transcript that captured the invocation. This reads it from stdin once and writes
it to a git-ignored file, so every later run reads the file instead.

The file is written under the repo (git-ignored via the `*.env` rules) rather than to a home
directory, because the sandbox can write here and cannot write to `%LOCALAPPDATA%`.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TARGET = REPO / ".env.daytona"


def main() -> int:
    key = (sys.argv[1] if len(sys.argv) > 1 else sys.stdin.read()).strip()
    if not key:
        print("no key given", file=sys.stderr)
        return 2
    if not key.startswith("dtn_"):
        # Named explicitly: a mis-pasted key fails later as an opaque 401 from a remote service,
        # which is a much worse place to discover a typo.
        print(f"that does not look like a Daytona key (expected a dtn_ prefix, got {key[:4]!r})",
              file=sys.stderr)
        return 2

    TARGET.write_text(f"DAYTONA_API_KEY={key}\n", encoding="utf-8", newline="\n")
    try:
        os.chmod(TARGET, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass

    # Confirmed by length and prefix only. Printing the key back -- even truncated -- into a log is
    # the habit that leaks them.
    print(f"  stored {TARGET.name}: {key[:4]}…{key[-4:]} ({len(key)} chars)")
    print(f"  git-ignored: check with  git check-ignore -v {TARGET.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
