#!/usr/bin/env python3
"""Collapse the badge criteria type list to a single source.

There are three copies of the same fact and they had already drifted:

| where | state |
|---|---|
| `worker/src/db/schema.ts` — `BADGE_CRITERIA_TYPES` | **missing `tracks_started`** |
| `worker/src/services/badges.ts` — `CriteriaType` union + `KNOWN_CRITERIA_TYPES` | 14 types |
| `tools/verify_badges.py` and `tools/audit_badges.py` — `CRITERIA` dicts | 14, after being fixed by hand |

`schema.ts` is the right home: it is the lowest layer, both other TypeScript files already import from it, and it
carries a doc comment per type explaining its `criteriaValue` shape. `badges.ts` keeps its union name as an
alias so nothing importing `CriteriaType` breaks, and derives its known-set from the imported array instead of
restating it.

The two Python tools cannot import TypeScript, so they keep a list — but a list that is now **checked against
`schema.ts`** rather than trusted. `verify_badges.py` gains that comparison, which turns silent drift into a
failed check. That is the part that matters: three copies is survivable if something compares them, and fatal
if nothing does.
"""

from __future__ import annotations

import argparse
import subprocess
import sys

HOST = "admin-certivo@139.190.97.11"
CONTAINER = "certivo-app-1"
SCHEMA = "/srv/certivo/worker/src/db/schema.ts"
SERVICE = "/srv/certivo/worker/src/services/badges.ts"


def ssh(command: str, stdin: bytes | None = None, timeout: int = 600) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=25", HOST, command],
        input=stdin, capture_output=True, timeout=timeout,
    )


def read(path: str) -> str:
    return (ssh(f"cat {path}").stdout or b"").decode("utf-8", "replace")


def copy_and_verify(local_path: str, remote_path: str) -> bool:
    """`docker cp` has silently no-opped repeatedly in this project, so every copy is hash-checked."""
    for attempt in range(1, 4):
        ssh(f"docker cp {local_path} {CONTAINER}:{remote_path}")
        host = (ssh(f"md5sum {local_path}").stdout or b"").decode().split()[0:1]
        cont = (ssh(f"docker exec {CONTAINER} md5sum {remote_path}").stdout or b"").decode().split()[0:1]
        if host and host == cont:
            print(f"    {remote_path.split('/')[-1]}: verified ({host[0][:12]}) on attempt {attempt}")
            return True
    print(f"    {remote_path}: COPY DID NOT LAND after 3 attempts")
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Single source for the badge criteria types.")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    schema = read(SCHEMA)
    service = read(SERVICE)
    if not schema or not service:
        print("  could not read the sources")
        return 1

    # --- schema.ts: add the missing type
    if '"tracks_started"' in schema:
        print("  schema.ts already lists tracks_started")
    else:
        old = '''  /** { count } — this track's last `count` passed levels each passed on attempt 1. */
  "level_streak",
] as const;'''
        new = '''  /** { count } — this track's last `count` passed levels each passed on attempt 1. */
  "level_streak",
  /** { count } — tracks the user has started, whether or not they are complete. */
  "tracks_started",
] as const;'''
        if old not in schema:
            print("  REFUSING: could not find the end of BADGE_CRITERIA_TYPES")
            return 1
        schema = schema.replace(old, new, 1)
        print("  schema.ts: added tracks_started")

    # --- badges.ts: import the list, drop the restated copy
    old_union = '''export type CriteriaType =
  | "level_complete"
  | "first_code_pass"
  | "perfect_score"
  | "track_complete"
  | "first_try_pass"
  | "attempts_to_pass"
  | "speed_pass"
  | "skill_star"
  | "skill_count_at_star"
  | "levels_completed"
  | "tracks_completed"
  | "code_tests_perfect"
  | "level_streak"
  | "tracks_started";'''
    new_union = '''/**
 * Derived from `schema.ts`'s single list rather than restated here. The three copies that used to exist had
 * already drifted -- `schema.ts` was missing `tracks_started` while this file handled it -- and a type the
 * schema does not know is a definition the database layer cannot explain.
 */
export type CriteriaType = BadgeCriteriaType;'''

    old_set = '''const KNOWN_CRITERIA_TYPES: ReadonlySet<string> = new Set<CriteriaType>([
  "level_complete",
  "first_code_pass",
  "perfect_score",
  "track_complete",
  "first_try_pass",
  "attempts_to_pass",
  "speed_pass",
  "skill_star",
  "skill_count_at_star",
  "levels_completed",
  "tracks_completed",
  "code_tests_perfect",
  "level_streak",
  "tracks_started",
]);'''
    new_set = '''const KNOWN_CRITERIA_TYPES: ReadonlySet<string> = new Set<string>(BADGE_CRITERIA_TYPES);'''

    if "export type CriteriaType = BadgeCriteriaType;" in service:
        print("  badges.ts already derives CriteriaType from the schema")
    else:
        for label, old, new in (("the union", old_union, new_union), ("the known set", old_set, new_set)):
            if old not in service:
                print(f"  REFUSING: could not find {label} in badges.ts")
                return 1
            service = service.replace(old, new, 1)
            print(f"  badges.ts: replaced {label} with a derivation")

    # The import already pulls several names from ../db/schema; add the two this now needs.
    import_line = [l for l in service.splitlines() if l.startswith('import { badges') and 'from "../db/schema"' in l]
    if not import_line:
        print("  REFUSING: could not find the ../db/schema import")
        return 1
    line = import_line[0]
    if "BADGE_CRITERIA_TYPES" in line:
        print("  badges.ts already imports BADGE_CRITERIA_TYPES")
    else:
        names = line[len("import { "):line.index(" }")]
        added = ", ".join(sorted(set(names.split(", ")) | {"BADGE_CRITERIA_TYPES", "type BadgeCriteriaType"}))
        service = service.replace(line, f"import {{ {added} }} from \"../db/schema\";", 1)
        print("  badges.ts: extended the schema import")

    if args.dry_run:
        print("  dry run: nothing written")
        return 0

    ssh(f"cp -a {SCHEMA} {SCHEMA}.before-singlelist")
    ssh(f"cp -a {SERVICE} {SERVICE}.before-singlelist")
    if ssh(f"cat > {SCHEMA}", schema.encode("utf-8")).returncode != 0:
        print("  schema write failed")
        return 1
    if ssh(f"cat > {SERVICE}", service.encode("utf-8")).returncode != 0:
        print("  service write failed")
        return 1

    print("  copying into the container (hash-verified):")
    ok = copy_and_verify(SCHEMA, "/app/worker/src/db/schema.ts")
    ok = copy_and_verify(SERVICE, "/app/worker/src/services/badges.ts") and ok
    if not ok:
        return 1

    check = ssh(f"docker exec -w /app {CONTAINER} node_modules/.bin/tsc --noEmit -p tsconfig.json")
    errors = ((check.stdout or b"") + (check.stderr or b"")).decode("utf-8", "replace")
    relevant = [l for l in errors.splitlines() if "badges.ts" in l or "schema.ts" in l]
    if relevant:
        print("  typecheck errors:")
        for line in relevant[:10]:
            print(f"    {line}")
        return 1
    print("  typecheck: clean for badges.ts and schema.ts")

    tests = ssh(f"docker exec -w /app {CONTAINER} node_modules/.bin/vitest run --config vitest.config.ts")
    out = ((tests.stdout or b"") + (tests.stderr or b"")).decode("utf-8", "replace")
    for line in out.strip().splitlines()[-5:]:
        print(f"    {line}")
    return 0 if tests.returncode == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
