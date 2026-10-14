#!/usr/bin/env python3
"""Update the badge test fixture for the merged progress query.

Adding `tracks_started` changed `loadBadgeState`'s third query from

    select { n: count(*) } where completed_at is not null

to

    select { completed: count(*) filter (where completed_at is not null), started: count(*) }

so one round trip answers both counts. The fake `Db` in the test file still returns `{ n: fixture.completedTracks }`,
which no longer has either field — so `completedTracks` silently became `0` and the `tracks_completed` assertion
failed at `badges.test.ts:316`.

That is the fixture doing its job: it is a stand-in for the database's response *shape*, and a query that
changes shape has to change the stand-in. The alternative — leaving it — would have every test read `0` for
both counts and pass, which is how a batching optimisation quietly disables two criteria.

The fixture gains a `tracksStarted` field defaulting to the completed count, so a test that does not care about
the distinction behaves as before.
"""

from __future__ import annotations

import argparse
import subprocess
import sys

HOST = "admin-certivo@139.190.97.11"
TEST_FILE = "/srv/certivo/worker/src/services/badges.test.ts"
CONTAINER = "certivo-app-1"

EDITS: list[tuple[str, str, str]] = [
    (
        "the Fixture interface",
        """  completedTracks: number;""",
        """  completedTracks: number;
  /** Tracks with any `user_track_progress` row. Defaults to `completedTracks` when not set. */
  tracksStarted?: number;""",
    ),
    (
        "the fake's user_track_progress projection",
        """    if (name === "user_track_progress") return [{ n: fixture.completedTracks }];""",
        """    // One query now returns both counts, so the stand-in returns both fields. A fixture that kept
    // returning `n` would make both read 0 and silently disable tracks_completed and tracks_started.
    if (name === "user_track_progress") {
      return [{ completed: fixture.completedTracks, started: fixture.tracksStarted ?? fixture.completedTracks }];
    }""",
    ),
]


def ssh(command: str, stdin: bytes | None = None, timeout: int = 600) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=25", HOST, command],
        input=stdin, capture_output=True, timeout=timeout,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Update the fixture for the merged progress query.")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    source = (ssh(f"cat {TEST_FILE}").stdout or b"").decode("utf-8", "replace")
    if not source:
        print(f"  could not read {TEST_FILE}")
        return 1

    patched = source
    for label, old, new in EDITS:
        if old not in patched:
            print(f"  REFUSING: could not find {label}")
            return 1
        patched = patched.replace(old, new, 1)
        print(f"  staged: {label}")

    if args.dry_run:
        print("  dry run: nothing written")
        return 0

    ssh(f"cp -a {TEST_FILE} {TEST_FILE}.before-tracksstarted")
    if ssh(f"cat > {TEST_FILE}", patched.encode("utf-8")).returncode != 0:
        print("  write failed")
        return 1
    ssh(f"docker cp {TEST_FILE} {CONTAINER}:{TEST_FILE}")
    print("  copied in")

    run = ssh(f"docker exec -w /app {CONTAINER} node_modules/.bin/vitest run --config vitest.config.ts")
    out = ((run.stdout or b"") + (run.stderr or b"")).decode("utf-8", "replace")
    for line in out.strip().splitlines()[-6:]:
        print(f"    {line}")
    return 0 if run.returncode == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
