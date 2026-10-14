#!/usr/bin/env python3
"""Write `user_track_progress.completed_at` when a learner passes a track's final level.

Agent B found that `tracks_completed` reads a column nothing writes. `submitAttempt()` stamps
`currentLevelNumber` on every levelling-up pass but never `completed_at`, so exactly one row in the whole
database had a value — a backfill. The consequence was that `generalist` and `completionist` were reachable only
for learners whose row happened to be timestamped already, which is to say almost never.

Two ways to fix it, and the choice matters:

* **(b) derive it** — count distinct tracks whose max-level exam is passed, inside `loadBadgeState`. The badge
  would then be correct while `completed_at` stayed permanently null.
* **(a) write the column** — stamp it here, where "level N of track T was just passed" is already known.

(a) is right because `completed_at` is the read model's own record of the event, and it already has one writer's
worth of semantics: `startedAt` is set on the insert path beside it. Deriving around a stale column would leave
every future reader of `completed_at` — a certificate, a profile, an admin view — inheriting the same silent
zero. The badge was the first symptom, not the problem.

The write is unconditional on `isLastLevel` rather than guarded by "is it already set": re-stamping the moment
of completion on a re-pass would move the date, and `completed_at` should record when the track was first
finished. An `IS NULL`-style guard is therefore expressed as "only set it when it is not already set", which is
what the surrounding progress write already does for `startedAt`.
"""

from __future__ import annotations

import argparse
import subprocess
import sys

HOST = "admin-certivo@139.190.97.11"
TARGET = "/srv/certivo/worker/src/services/scoring.ts"
CONTAINER = "certivo-app-1"


def ssh(command: str, stdin: bytes | None = None, timeout: int = 600) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=25", HOST, command],
        input=stdin, capture_output=True, timeout=timeout,
    )


# 1. Name the condition once, next to the computation it belongs to.
OLD_LASTLEVEL = """    const [lastLevel] = await db
      .select({ n: sql<number>`max(level_number)::int` })
      .from(levels)
      .where(eq(levels.trackId, level.trackId));
"""
NEW_LASTLEVEL = """    const [lastLevel] = await db
      .select({ n: sql<number>`max(level_number)::int` })
      .from(levels)
      .where(eq(levels.trackId, level.trackId));

    // Finishing a track is recorded here because this is the one place the app knows it happened.
    // `tracks_completed` badges read `completed_at`, and nothing wrote it, so they were reachable only
    // for rows a backfill had already stamped.
    const finishedTrack = !!lastLevel?.n && level.levelNumber >= lastLevel.n;
"""

# 2. Stamp completed_at on both progress paths, next to the currentLevelNumber write.
OLD_UPDATE = """        await db
          .update(userTrackProgress)
          .set({ currentLevelNumber: level.levelNumber })
          .where(and(eq(userTrackProgress.userId, userId), eq(userTrackProgress.trackId, level.trackId)));"""
NEW_UPDATE = """        await db
          .update(userTrackProgress)
          .set({
            currentLevelNumber: level.levelNumber,
            // Only the first completion is recorded: a later re-pass must not move the date.
            ...(finishedTrack && !progressRows[0].completedAt ? { completedAt: now } : {}),
          })
          .where(and(eq(userTrackProgress.userId, userId), eq(userTrackProgress.trackId, level.trackId)));"""

OLD_INSERT = """        await db.insert(userTrackProgress).values({
          userId,
          trackId: level.trackId,
          currentLevelNumber: level.levelNumber,
          startedAt: now,
        });"""
NEW_INSERT = """        await db.insert(userTrackProgress).values({
          userId,
          trackId: level.trackId,
          currentLevelNumber: level.levelNumber,
          startedAt: now,
          completedAt: finishedTrack ? now : null,
        });"""

# 3. The badge result can now read the same condition rather than re-deriving it.
OLD_RESULT = """      isLastLevel: !!lastLevel?.n && level.levelNumber >= lastLevel.n,"""
NEW_RESULT = """      isLastLevel: finishedTrack,"""


def main() -> int:
    parser = argparse.ArgumentParser(description="Stamp completed_at on track completion.")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    source = (ssh(f"cat {TARGET}").stdout or b"").decode("utf-8", "replace")
    if not source:
        print(f"  could not read {TARGET}")
        return 1

    for label, needle in (("lastLevel", OLD_LASTLEVEL), ("update", OLD_UPDATE),
                          ("insert", OLD_INSERT), ("badge result", OLD_RESULT)):
        if needle not in source:
            print(f"  REFUSING: could not find the {label} block")
            return 1

    patched = source
    patched = patched.replace(OLD_LASTLEVEL, NEW_LASTLEVEL, 1)
    patched = patched.replace(OLD_UPDATE, NEW_UPDATE, 1)
    patched = patched.replace(OLD_INSERT, NEW_INSERT, 1)
    patched = patched.replace(OLD_RESULT, NEW_RESULT, 1)
    print("  four edits staged: finishedTrack, progress update, progress insert, badge result")

    if args.dry_run:
        print("  dry run: nothing written")
        return 0

    ssh(f"cp -a {TARGET} {TARGET}.before-completedat")
    if ssh(f"cat > {TARGET}", patched.encode("utf-8")).returncode != 0:
        print("  write failed")
        return 1

    copy = ssh(f"docker cp {TARGET} {CONTAINER}:{TARGET}")
    verify = (ssh(f"docker exec {CONTAINER} grep -c finishedTrack {TARGET}").stdout or b"").decode().strip()
    print(f"  copied in: finishedTrack occurrences in the container = {verify}")
    if verify != "4":
        print("  REFUSING to continue: the container copy does not match (docker cp has failed silently before)")
        return 1

    check = ssh(f"docker exec -w /app {CONTAINER} node_modules/.bin/tsc --noEmit -p tsconfig.json")
    errors = ((check.stdout or b"") + (check.stderr or b"")).decode("utf-8", "replace")
    relevant = [line for line in errors.splitlines() if "scoring.ts" in line]
    if relevant:
        print("  typecheck errors in scoring.ts:")
        for line in relevant[:10]:
            print(f"    {line}")
        return 1
    print("  typecheck: no errors in scoring.ts")

    tests = ssh(f"docker exec -w /app {CONTAINER} node_modules/.bin/vitest run --config vitest.config.ts")
    out = ((tests.stdout or b"") + (tests.stderr or b"")).decode("utf-8", "replace")
    for line in out.strip().splitlines()[-6:]:
        print(f"    {line}")
    return 0 if tests.returncode == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
