#!/usr/bin/env python3
"""Scope track-owned badge criteria to their own track.

The live probe found a real bug that the unit tests could not: passing **level 20 of `ai-ml-engineer`** awarded
**all 35 graduate badges and all 35 level-20 badges**, including tracks the user had never started.

`awardBadges()` reads every definition and calls

    matchCriteria(criteriaType, criteriaValue, state, result, speedFraction)

— the definition's `trackId` is never passed. So these two predicates match on the level alone:

    case "level_complete": return levelNumber === result.levelNumber;
    case "track_complete": return result.isLastLevel;

`ai-ml-engineer.L20` and `accountant.L20` both carry `{"levelNumber": 20}`, and both satisfy a level-20 pass in
either track. The same holds for every graduate badge, since `track_complete` carries no value at all.

The fix is a single guard rather than 35 special cases: **a badge that belongs to a track may only be awarded
by a pass in that track.** Platform badges (`track_id IS NULL`) keep matching globally, which they must — that
is what makes `explorer` and `generalist` breadth badges work.

This is exactly the class of defect that "762 of 762 reachable" cannot see. Reachability says a badge *can*
fire; it says nothing about whether it fires for the right person, and a fake `Db` in a unit test will not
notice either because the fixture's definitions all belong to the track under test.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

HOST = "admin-certivo@139.190.97.11"
TARGET = "/srv/certivo/worker/src/services/badges.ts"
CONTAINER = "certivo-app-1"


def ssh(command: str, stdin: bytes | None = None, timeout: int = 600) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=25", HOST, command],
        input=stdin, capture_output=True, timeout=timeout,
    )


def read_remote(path: str) -> str:
    return (ssh(f"cat {path}").stdout or b"").decode("utf-8", "replace")


OLD_SIGNATURE = """function matchCriteria(
  criteriaType: CriteriaType,
  payload: CriteriaValue | null,
  state: BadgeState,
  result: ExamResultForBadges,
  speedFraction: number | null,
): boolean {
  // An unreadable payload is not a wildcard.
  if (payload === null) return false;
"""

NEW_SIGNATURE = """function matchCriteria(
  criteriaType: CriteriaType,
  payload: CriteriaValue | null,
  state: BadgeState,
  result: ExamResultForBadges,
  speedFraction: number | null,
  definitionTrackId: string | null,
): boolean {
  // An unreadable payload is not a wildcard.
  if (payload === null) return false;

  // A badge owned by a track may only be earned by a pass in that track. Without this,
  // `level_complete {levelNumber: 20}` matches a level-20 pass in *any* track, and because all 35 tracks
  // number their levels 1..20, completing one track awards the level-20 badge of all 35 -- and
  // `track_complete`, which carries no value at all, awards all 35 graduate badges. Found by driving the real
  // service against the live database: one ai-ml-engineer level-20 pass produced 81 awards.
  //
  // Platform badges have a null track and must keep matching globally, which is what makes the breadth
  // criteria (`levels_completed`, `tracks_completed`) work across tracks.
  if (definitionTrackId !== null && definitionTrackId !== result.trackId) return false;
"""

OLD_SELECT = """    db
      .select({
        id: badges.id,
        criteriaType: badges.criteriaType,
        criteriaValueJson: badges.criteriaValueJson,
      })
      .from(badges),"""

NEW_SELECT = """    db
      .select({
        id: badges.id,
        trackId: badges.trackId,
        criteriaType: badges.criteriaType,
        criteriaValueJson: badges.criteriaValueJson,
      })
      .from(badges),"""

OLD_CALL = """    if (!matchCriteria(criteriaType, parseCriteriaValue(definition.criteriaValueJson), state, result, speedFraction)) {
      continue;
    }"""

NEW_CALL = """    if (
      !matchCriteria(
        criteriaType,
        parseCriteriaValue(definition.criteriaValueJson),
        state,
        result,
        speedFraction,
        definition.trackId ?? null,
      )
    ) {
      continue;
    }"""


def main() -> int:
    parser = argparse.ArgumentParser(description="Scope track-owned criteria to their own track.")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    source = read_remote(TARGET)
    if not source:
        print(f"  could not read {TARGET}")
        return 1

    for label, needle in (("signature", OLD_SIGNATURE), ("select", OLD_SELECT), ("call", OLD_CALL)):
        if needle not in source:
            print(f"  REFUSING: could not find the {label} to replace, so the file has moved on")
            return 1

    patched = source.replace(OLD_SIGNATURE, NEW_SIGNATURE, 1)
    patched = patched.replace(OLD_SELECT, NEW_SELECT, 1)
    patched = patched.replace(OLD_CALL, NEW_CALL, 1)
    print("  three edits staged: signature, definition select, call site")

    if args.dry_run:
        print("  dry run: nothing written")
        return 0

    ssh(f"cp -a {TARGET} {TARGET}.before-trackfix")
    print(f"  backup: {TARGET}.before-trackfix")

    if ssh(f"cat > {TARGET}", patched.encode("utf-8")).returncode != 0:
        print("  write failed")
        return 1
    copy = ssh(f"docker cp {TARGET} {CONTAINER}:{TARGET}")
    print(f"  copied into the container: {copy.returncode == 0}")

    check = ssh(f"docker exec -w /app {CONTAINER} node_modules/.bin/tsc --noEmit -p tsconfig.json")
    errors = ((check.stdout or b"") + (check.stderr or b"")).decode("utf-8", "replace")
    relevant = [line for line in errors.splitlines() if "badges.ts" in line]
    if relevant:
        print("  typecheck reports errors in badges.ts:")
        for line in relevant[:10]:
            print(f"    {line}")
        return 1
    print("  typecheck: no errors in badges.ts")

    tests = ssh(f"docker exec -w /app {CONTAINER} node_modules/.bin/vitest run "
                f"--config vitest.config.ts worker/src/services/badges.test.ts")
    tail = ((tests.stdout or b"") + (tests.stderr or b"")).decode("utf-8", "replace")
    for line in tail.strip().splitlines()[-6:]:
        print(f"    {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
