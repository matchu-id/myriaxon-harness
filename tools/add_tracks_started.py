#!/usr/bin/env python3
"""Add `tracks_started` as a fourteenth criteria type, implemented rather than declared.

Nothing in the catalogue rewarded *starting* a track. A learner who opens all 35 tracks and finishes none held
no breadth badge at all, while "started every track" is a real signal: it says someone surveyed the catalogue
rather than going deep on one path. It is also the only honest way to reach the ~100 authored badges the
objective names, since the other twelve families are saturated — see BADGES.md.

A script that added these definitions alone was written and **deleted unfired**, because `tracks_started` is
not a type `matchCriteria` handles: 10 definitions would have had no earn path, which is the exact defect
rule 4 exists to prevent. This does the whole change instead:

1. the type in the `CriteriaType` union,
2. the label in `KNOWN_CRITERIA_TYPES`,
3. a `count` field on `BadgeState`,
4. the count read in `loadBadgeState` — from the **same query** that already counts completed tracks, so the
   batching property (a fixed number of round trips regardless of catalogue size) is preserved,
5. a `case` in `matchCriteria`, written as "at least", matching `levels_completed` and `tracks_completed`.

Only `badges.ts` changes; `scoring.ts` needs nothing, because `tracks_started` is derivable from committed rows
at the existing chokepoint.
"""

from __future__ import annotations

import argparse
import subprocess
import sys

HOST = "admin-certivo@139.190.97.11"
TARGET = "/srv/certivo/worker/src/services/badges.ts"
CONTAINER = "certivo-app-1"

EDITS: list[tuple[str, str, str]] = [
    (
        "the CriteriaType union",
        '''  | "code_tests_perfect"
  | "level_streak";''',
        '''  | "code_tests_perfect"
  | "level_streak"
  | "tracks_started";''',
    ),
    (
        "KNOWN_CRITERIA_TYPES",
        '''  "code_tests_perfect",
  "level_streak",
]);''',
        '''  "code_tests_perfect",
  "level_streak",
  "tracks_started",
]);''',
    ),
    (
        "BadgeState",
        '''  /** Tracks marked complete in `user_track_progress`. */
  completedTracks: number;''',
        '''  /** Tracks marked complete in `user_track_progress`. */
  completedTracks: number;
  /**
   * Tracks the user has started -- any `user_track_progress` row, whether or not it is complete. Read by the
   * same query as `completedTracks`, since both are counts over that one table.
   */
  tracksStarted: number;''',
    ),
    (
        "the progress query",
        '''    // 3. Tracks marked complete in the progress read model.
    db
      .select({ n: sql<number>`count(*)::int` })
      .from(userTrackProgress)
      .where(and(eq(userTrackProgress.userId, userId), isNotNull(userTrackProgress.completedAt))),''',
        '''    // 3. The progress read model, counted two ways in one query: tracks finished, and tracks begun.
    //    Both are counts over the same rows, so splitting them into two queries would cost a round trip
    //    per submission for no benefit -- and the batching property (a fixed number of queries however
    //    large the catalogue grows) is the reason this function exists.
    db
      .select({
        completed: sql<number>`(count(*) filter (where ${userTrackProgress.completedAt} is not null))::int`,
        started: sql<number>`count(*)::int`,
      })
      .from(userTrackProgress)
      .where(eq(userTrackProgress.userId, userId)),''',
    ),
    (
        "the returned state",
        '''    completedTracks: Number(completedTrackRows[0]?.n ?? 0),''',
        '''    completedTracks: Number(completedTrackRows[0]?.completed ?? 0),
    tracksStarted: Number(completedTrackRows[0]?.started ?? 0),''',
    ),
    (
        "the matchCriteria case",
        '''    case "code_tests_perfect": {''',
        '''    // 14. tracks_started {count} -- breadth of exploration. Distinct from tracks_completed: a learner
    //     who opens every track and finishes none has started 35 and completed 0.
    case "tracks_started": {
      const count = requestedCount(payload);
      return count !== null && state.tracksStarted >= count;
    }

    case "code_tests_perfect": {''',
    ),
]


def ssh(command: str, stdin: bytes | None = None, timeout: int = 600) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=25", HOST, command],
        input=stdin, capture_output=True, timeout=timeout,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Implement the tracks_started criteria type.")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    source = (ssh(f"cat {TARGET}").stdout or b"").decode("utf-8", "replace")
    if not source:
        print(f"  could not read {TARGET}")
        return 1
    if '"tracks_started"' in source:
        print("  tracks_started is already implemented")
        return 0

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

    ssh(f"cp -a {TARGET} {TARGET}.before-tracksstarted")
    if ssh(f"cat > {TARGET}", patched.encode("utf-8")).returncode != 0:
        print("  write failed")
        return 1

    ssh(f"docker cp {TARGET} {CONTAINER}:{TARGET}")
    count = (ssh(f"docker exec {CONTAINER} grep -c tracks_started {TARGET}").stdout or b"").decode().strip()
    print(f"  container occurrences of tracks_started: {count}")
    if count != "6":
        print("  REFUSING: the container copy does not match the host (docker cp has failed silently before)")
        return 1

    check = ssh(f"docker exec -w /app {CONTAINER} node_modules/.bin/tsc --noEmit -p tsconfig.json")
    errors = ((check.stdout or b"") + (check.stderr or b"")).decode("utf-8", "replace")
    relevant = [line for line in errors.splitlines() if "badges.ts" in line or "scoring.ts" in line]
    if relevant:
        print("  typecheck errors:")
        for line in relevant[:10]:
            print(f"    {line}")
        return 1
    print("  typecheck: clean for badges.ts and scoring.ts")

    tests = ssh(f"docker exec -w /app {CONTAINER} node_modules/.bin/vitest run --config vitest.config.ts")
    out = ((tests.stdout or b"") + (tests.stderr or b"")).decode("utf-8", "replace")
    for line in out.strip().splitlines()[-5:]:
        print(f"    {line}")
    return 0 if tests.returncode == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
