#!/usr/bin/env python3
"""Repair the badge service after two agents wrote to the same file.

The app is currently crash-looping:

    Error [TransformError]: Transform failed with 1 error:
    /app/worker/src/services/badges.ts:307:9: ERROR: The symbol "parseCriteriaValue" has already been declared

What happened: Agent B owns `worker/src/services/badges.ts` and rewrote it definitions-driven. Agent C owns the
badge catalogue endpoint, needed a new `listBadgesFor`, and wrote their version into the *same file* — the one
file the work split did not assign unambiguously. The result is a 709-line file that is Agent C's catalogue code
(lines 1-246) concatenated with Agent B's award logic (lines 247-709), with two independent
`parseCriteriaValue` helpers:

* line 177 — `(json: string): unknown`, tolerant, for rendering the catalogue
* line 307 — `(json: string): CriteriaValue | null`, strict, for evaluating predicates

Both are correct in their own context and neither is redundant, so the repair is a rename rather than a
deletion. The catalogue version becomes `parseCriteriaValueRaw` and its two call sites are updated; the strict
version keeps its name because that is the one referenced by `awardBadges`.

This is deliberately the smallest change that restores a working app. The duplicate `started`, `earnedAt` and
other symbols the diagnostic also reported are object keys and local variables inside separate functions, not
top-level declarations, so they are not conflicts.
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
    result = ssh(f"cat {path}")
    return (result.stdout or b"").decode("utf-8", "replace")


def main() -> int:
    parser = argparse.ArgumentParser(description="Rename the colliding helper in badges.ts.")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    source = read_remote(TARGET)
    if not source:
        print(f"  could not read {TARGET}")
        return 1

    lines = source.splitlines()
    print(f"  {TARGET}: {len(lines)} lines")

    # Back up first. The file is live and the app is down; a repair that makes it worse is not recoverable.
    backup = f"{TARGET}.before-rename"
    ssh(f"cp -a {TARGET} {backup}")
    print(f"  backup: {backup}")

    # Rename only the catalogue helper: the declaration at the FIRST occurrence, and the call sites above the
    # strict declaration. Identification is by position, because both declarations share a name and a text
    # match cannot tell them apart.
    declarations = [i for i, line in enumerate(lines) if line.startswith("function parseCriteriaValue(")]
    if len(declarations) != 2:
        print(f"  REFUSING: expected 2 declarations, found {len(declarations)}")
        return 1
    catalogue_decl, strict_decl = declarations
    print(f"  declarations at lines {catalogue_decl + 1} and {strict_decl + 1}")

    changed = 0
    for index, line in enumerate(lines):
        if "parseCriteriaValue" not in line:
            continue
        if index == strict_decl or index > strict_decl:
            continue          # the award-logic helper and its call site keep the strict name
        lines[index] = line.replace("parseCriteriaValue", "parseCriteriaValueRaw")
        changed += 1
    print(f"  renamed {changed} occurrence(s) in the catalogue section")

    repaired = "\n".join(lines) + "\n"
    if args.dry_run:
        print("  dry run: nothing written")
        return 0

    result = ssh(f"cat > {TARGET}", repaired.encode("utf-8"))
    if result.returncode != 0:
        print(f"  write failed: {(result.stderr or b'').decode('utf-8', 'replace')[:300]}")
        return 1
    print("  written to the VPS")

    copy = ssh(f"docker cp {TARGET} {CONTAINER}:{TARGET}")
    print(f"  copied into the container: {copy.returncode == 0}")

    check = ssh(f"docker exec -w /app {CONTAINER} node_modules/.bin/tsc --noEmit -p tsconfig.json")
    output = ((check.stdout or b"") + (check.stderr or b"")).decode("utf-8", "replace")
    if check.returncode == 0:
        print("  typecheck: clean")
    else:
        print("  typecheck FAILED:")
        for line in output.strip().splitlines()[:15]:
            print(f"    {line}")
        return 1

    state = ssh(f"sleep 5; docker ps --format '{{{{.Names}}}}  {{{{.Status}}}}' | grep certivo-app")
    print(f"  container: {(state.stdout or b'').decode('utf-8', 'replace').strip()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
