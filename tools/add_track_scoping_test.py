#!/usr/bin/env python3
"""Add a regression test for the cross-track award bug.

The live probe found that one `ai-ml-engineer` level-20 pass awarded all 35 graduate badges and all 35 level-20
badges. `awardBadges()` never passed the definition's `trackId` to `matchCriteria()`, so
`level_complete {levelNumber: 20}` matched a level-20 pass in any track.

The existing 19 tests could not catch it. They build a fake `Db` whose fixture contains a handful of
definitions that all belong to the track under test, so "the badge belongs to a different track" is a case the
fixture cannot express. **A test can only fail on a distinction its fixture can represent**, and this one could
not represent the distinction that mattered.

The test added here gives the fixture definitions from two tracks and asserts that a pass in one awards only
that track's badges. It is written to fail against the pre-fix code.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

HOST = "admin-certivo@139.190.97.11"
CONTAINER = "certivo-app-1"
TEST_FILE = "/srv/certivo/worker/src/services/badges.test.ts"

TEST = '''
  // Regression: `awardBadges()` used to evaluate `level_complete` and `track_complete` without consulting the
  // definition's own track, so a level-20 pass in one track satisfied the level-20 badge of every track.
  // Driving the real service against the live database produced 81 awards from a single pass.
  //
  // The fixture has to contain definitions from more than one track for this to be expressible at all, which
  // is why the earlier tests could not see it. A test can only fail on a distinction its fixture represents.
  it("awards only the badges of the track that was passed, and not another track's", async () => {
    // `currentExam()` defaults its examId to `RESULT.examId`, so the history row and the result agree without
    // the test restating the id; only the level and track differ from the default scenario.
    const db = seed(
      makeFixture({
        badges: [
          definition("alpha.L20", "level_complete", { levelNumber: 20 }, "alpha"),
          definition("beta.L20", "level_complete", { levelNumber: 20 }, "beta"),
          definition("alpha.graduate", "track_complete", {}, "alpha"),
          definition("beta.graduate", "track_complete", {}, "beta"),
        ],
        history: [currentExam({ levelId: "alpha.L20", levelNumber: 20, trackId: "alpha" })],
      }),
    );
    const earned = await db.run({ trackId: "alpha", levelNumber: 20, isLastLevel: true });
    // `beta`'s two badges carry the same level number and satisfy `isLastLevel` identically; only the
    // definition's own track separates them, which is the distinction this test exists to pin.
    expect(earned.sort()).toEqual(["alpha.L20", "alpha.graduate"]);
  });
'''


def ssh(command: str, stdin: bytes | None = None, timeout: int = 600) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=25", HOST, command],
        input=stdin, capture_output=True, timeout=timeout,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Add the cross-track regression test.")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    source = (ssh(f"cat {TEST_FILE}").stdout or b"").decode("utf-8", "replace")
    if not source:
        print(f"  could not read {TEST_FILE}")
        return 1
    if "not another track's" in source:
        print("  the regression test is already present")
        return 0

    # The helper signatures decide whether the test can express a second track at all. `definition()` needs a
    # track argument, and the fixture's definitions must not be forced onto the passed track.
    print("  current helper signatures:")
    for line in source.splitlines():
        if re.match(r"function (definition|makeFixture|currentExam)\(", line):
            print(f"    {line.strip()[:110]}")

    # `definition()` needs to be able to name a track, and `BadgeRow` needs to carry one, or the fixture cannot
    # express "a badge belonging to another track" — which is precisely why the 19 existing tests could not
    # catch the cross-track award bug. A test can only fail on a distinction its fixture can represent.
    edits = [
        (
            "interface BadgeRow {\n  id: string;\n  criteriaType: string;\n  criteriaValueJson: string;\n}",
            "interface BadgeRow {\n  id: string;\n  /** Null for a platform-wide badge. A track-owned badge may only be earned in its own track. */\n  trackId?: string | null;\n  criteriaType: string;\n  criteriaValueJson: string;\n}",
        ),
        (
            'function definition(id: string, criteriaType: string, criteriaValue: Record<string, unknown> = {}): BadgeRow {\n  return { id, criteriaType, criteriaValueJson: JSON.stringify(criteriaValue) };\n}',
            'function definition(\n  id: string,\n  criteriaType: string,\n  criteriaValue: Record<string, unknown> = {},\n  trackId: string | null = null,\n): BadgeRow {\n  return { id, trackId, criteriaType, criteriaValueJson: JSON.stringify(criteriaValue) };\n}',
        ),
    ]
    for old, new in edits:
        if old not in source:
            print(f"  REFUSING: could not find this to extend:\n    {old.splitlines()[0][:90]}")
            return 1
        source = source.replace(old, new, 1)
    print("  extended `BadgeRow` with an optional trackId and `definition()` with a track argument")

    closing = source.rstrip().rfind("});")
    if closing == -1:
        print("  could not find the end of the outermost describe block")
        return 1
    patched = source[:closing] + TEST + source[closing:]

    if args.dry_run:
        print("  dry run: nothing written")
        return 0

    ssh(f"cp -a {TEST_FILE} {TEST_FILE}.before-regression")
    if ssh(f"cat > {TEST_FILE}", patched.encode("utf-8")).returncode != 0:
        print("  write failed")
        return 1
    ssh(f"docker cp {TEST_FILE} {CONTAINER}:{TEST_FILE}")
    print("  test appended and copied in")

    run = ssh(f"docker exec -w /app {CONTAINER} node_modules/.bin/vitest run "
              f"--config vitest.config.ts worker/src/services/badges.test.ts")
    out = ((run.stdout or b"") + (run.stderr or b"")).decode("utf-8", "replace")
    for line in out.strip().splitlines()[-14:]:
        print(f"    {line}")
    return 0 if run.returncode == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
