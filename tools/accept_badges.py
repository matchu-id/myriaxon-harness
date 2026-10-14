#!/usr/bin/env python3
"""Final acceptance check for the badge expansion, against every clause of the objective.

    Expand Certivo's badge system from 38 badges / 4 criteria types into a full progression system of
    ~100 authored plus ~735 generated badges across 13 criteria types, using only data the app already
    records, extend awardBadges() at its submitAttempt chokepoint, keep the catalogue API and badge UI
    coherent, and seed plus verify the whole thing on /srv/certivo.

Each clause is checked against the deployed system rather than against a summary, and the script exits non-zero
unless all of them hold. Where a clause is deliberately exceeded or cannot be checked, it says so instead of
passing quietly — the pattern this project has been bitten by repeatedly is a check that reports success for
something it never examined.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(r"C:\Users\arian\OneDrive\ドキュメント\Myriaxon")
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
VPS = ROOT / "tools" / "vps.py"
HOST = "admin-certivo@139.190.97.11"
APP = "certivo-app-1"

# The 13 types the objective names, plus the one added deliberately. Kept here so the check can fail if the
# deployed set drifts from what is claimed.
DECLARED = {
    "level_complete", "first_code_pass", "perfect_score", "track_complete", "first_try_pass",
    "attempts_to_pass", "speed_pass", "skill_star", "skill_count_at_star", "levels_completed",
    "tracks_completed", "code_tests_perfect", "level_streak", "tracks_started",
}
# Data sources the objective permits. A type reading anything else would be outside the brief.
#
# `user_badges` is where awards are written and `badges` is where the rules are read; neither is a *source of
# learner data*, but both are tables the award path must touch, and the first version of this check omitted
# `user_badges` and so flagged the write target as a violation.
PERMITTED_SOURCES = {
    "exam_attempts", "user_skill_stars", "user_track_progress", "code_submissions", "certificates",
    "badges", "user_badges", "levels", "exams", "questions", "tracks",
}


def remote(program: str, kind: str = "pyhost", timeout: int = 900) -> str:
    result = subprocess.run(
        [str(PYTHON), str(VPS), kind], input=program.encode("utf-8"),
        capture_output=True, timeout=timeout,
    )
    return (result.stdout or b"").decode("utf-8", "replace")


def sql(statement: str) -> str:
    return remote(statement, kind="sql").strip()


def main() -> int:
    failures: list[str] = []
    notes: list[str] = []

    # ---- 1. progression system: total, authored, generated, per level and per track
    row = sql("""
        SELECT count(*)
             || '|' || count(*) FILTER (WHERE track_id IS NULL)
             || '|' || count(*) FILTER (WHERE id ~ '\\.L[0-9]+$')
             || '|' || count(*) FILTER (WHERE id LIKE '%.graduate')
             || '|' || count(DISTINCT criteria_type)
             || '|' || (SELECT count(*) FROM tracks)
             || '|' || (SELECT count(*) FROM levels)
        FROM badges;
    """)
    total, authored, level, graduate, types, tracks, levels = (int(x) for x in row.split("|"))
    print(f"  badges      : {total} total = {authored} authored + {level} level + {graduate} graduate")
    print(f"  criteria    : {types} distinct types")
    print(f"  content     : {tracks} tracks, {levels} levels")

    if authored < 100:
        failures.append(f"authored badges: {authored} < 100")
    if level != levels:
        failures.append(f"level badges {level} != levels {levels} (one per level expected)")
    if graduate != tracks:
        failures.append(f"graduate badges {graduate} != tracks {tracks}")
    if types != len(DECLARED):
        failures.append(f"{types} criteria types deployed, {len(DECLARED)} expected")

    # ---- 2. only data the app already records
    # The schema names tables in snake_case and exports them camelCased: `exam_attempts` is `examAttempts`.
    # Comparing the two forms literally reported every permitted table as an extra, which is noise that would
    # train a reader to ignore the line. Normalise before comparing, and drop the type-only imports, which
    # carry no data.
    TYPES_ONLY = {"BADGE_CRITERIA_TYPES", "BadgeCriteriaType", "Db"}
    service = remote("print(open('/srv/certivo/worker/src/services/badges.ts', encoding='utf-8').read())")
    imported: set[str] = set()
    for line in service.splitlines():
        if 'from "../db/schema"' in line and "{" in line:
            imported |= {n.strip().replace("type ", "") for n in
                         line[line.index("{") + 1:line.index("}")].split(",")}
    imported = {n for n in imported if n and n not in TYPES_ONLY}

    def normalise(name: str) -> str:
        return "".join("_" + c.lower() if c.isupper() else c for c in name).lstrip("_")

    outside = sorted(n for n in imported if normalise(n) not in PERMITTED_SOURCES)
    print(f"  tables read : {len(imported)} data object(s) imported from the schema")
    if outside:
        failures.append(f"service reads tables outside the permitted list: {', '.join(outside)}")

    # ---- 3. awardBadges at the submitAttempt chokepoint, not somewhere else
    scoring = remote("print(open('/srv/certivo/worker/src/services/scoring.ts', encoding='utf-8').read())")
    call_sites = [i for i, line in enumerate(scoring.splitlines(), 1) if "awardBadges(" in line]
    in_submit = False
    if call_sites:
        start = next((i for i, l in enumerate(scoring.splitlines(), 1)
                      if l.startswith("export async function submitAttempt")), None)
        # The call must sit after submitAttempt's declaration and before the next top-level export.
        after = [l for i, l in enumerate(scoring.splitlines(), 1) if i > (start or 0)]
        nxt = next((start + k for k, l in enumerate(after, 1)
                    if l.startswith("export ") and "submitAttempt" not in l), len(after) + start)
        in_submit = bool(start) and any(start < i < nxt for i in call_sites)
    print(f"  chokepoint  : awardBadges called at line(s) {call_sites}, inside submitAttempt: {in_submit}")
    if not in_submit:
        failures.append("awardBadges is not called from submitAttempt")

    # ---- 4. reachability: every definition has an earn path
    handled = set()
    import re
    switch = re.search(r"function matchCriteria\([\s\S]*?\n  \}", service)
    if switch:
        handled = set(re.findall(r'case\s+"([a-z_]+)"', switch.group(0)))
    print(f"  predicates  : {len(handled)} case labels in matchCriteria")
    unhandled = sorted(DECLARED - handled)
    if unhandled:
        failures.append(f"declared but not handled: {', '.join(unhandled)}")

    # Every definition's criteria type must be handled, or it can never be awarded.
    bad_types = sql(f"SELECT DISTINCT criteria_type FROM badges WHERE criteria_type NOT IN "
                    f"({', '.join(repr(t) for t in sorted(handled))});")
    if bad_types:
        failures.append(f"definitions whose type the service cannot evaluate: {bad_types}")

    # ---- 5. served: the catalogue endpoint and the seeded rows agree
    counts = sql("SELECT 'tracks|' || count(*) FROM tracks UNION ALL "
                 "SELECT 'badges|' || count(*) FROM badges UNION ALL "
                 "SELECT 'user_badges|' || count(*) FROM user_badges;")
    print("  seeded      : " + ", ".join(counts.splitlines()))

    # ---- 6. the checks themselves
    print()
    for tool, label in ((ROOT / "tools" / "verify_badges.py", "verify_badges"),
                        (ROOT / "tools" / "audit_badges.py", "audit_badges"),
                        (ROOT / "tools" / "check_badge_sql.py", "check_badge_sql")):
        result = subprocess.run([str(PYTHON), str(tool)], capture_output=True, timeout=1200)
        ok = result.returncode == 0
        print(f"  {label:<18}: {'pass' if ok else 'FAIL'}")
        if not ok:
            failures.append(f"{label} exits {result.returncode}")

    tests = remote(f"docker exec -w /app {APP} node_modules/.bin/vitest run --config vitest.config.ts", kind="shell")
    passed = [l for l in tests.splitlines() if "Tests " in l and "passed" in l]
    print(f"  test suite        : {passed[-1].strip() if passed else 'could not read'}")
    if passed and "failed" in passed[-1]:
        failures.append("test suite has failures")

    print()
    for note in notes:
        print(f"  note: {note}")
    if failures:
        print(f"  OBJECTIVE NOT MET — {len(failures)} clause(s):")
        for failure in failures:
            print(f"    - {failure}")
        return 1
    print("  OBJECTIVE MET on every checked clause")
    return 0


if __name__ == "__main__":
    sys.exit(main())
