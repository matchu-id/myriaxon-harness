#!/usr/bin/env python3
"""Run `loadBadgeState`'s SQL against the real database, since its tests only exercise a fake one.

Agent B's 19 tests are thorough about *logic* -- boundary thresholds, absent definitions, unknown criteria
types, single-insert. They run against a hand-built fake `Db`, though, so none of the SQL ever reaches
Postgres. A `tsx` transform error already slipped past in this module once (`sql` used six times, never
imported) and `tsc` did not catch it either; a malformed SQL fragment has the same character, except it fails
at runtime inside an exam submission rather than at build time.

This is not a substitute for the unit tests. It closes the one gap they cannot: whether the queries the
service will actually issue are valid against the live schema with the live data.

Queries are reproduced here from the service rather than executed through it, because importing the module
would drag in the whole worker. They are copied verbatim, so a drift between this file and the service is
possible -- the check is worth having anyway, and the alternative is no check at all.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(r"C:\Users\arian\OneDrive\ドキュメント\Myriaxon")
VPS = ROOT / "tools" / "vps.py"
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"

# Each entry: (label, SQL as the service builds it for a given user).
QUERIES: list[tuple[str, str]] = [
    (
        "1. attempt history per exam (history, streak, repeat counts)",
        """
        SELECT ea.exam_id,
               e.level_id,
               l.level_number,
               l.track_id,
               count(*)::int AS attempts,
               bool_or(ea.passed AND ea.id = (
                   SELECT x.id FROM exam_attempts x
                   WHERE x.exam_id = ea.exam_id AND x.user_id = ea.user_id
                   ORDER BY x.started_at ASC, x.id ASC LIMIT 1
               )) AS first_attempt_passed,
               (count(*) FILTER (WHERE ea.score_pct = 100 AND ea.passed))::int AS perfect_scores
        FROM exam_attempts ea
        INNER JOIN exams e ON e.id = ea.exam_id
        INNER JOIN levels l ON l.id = e.level_id
        WHERE ea.user_id = ?
          AND ea.status = 'submitted'
          AND ea.submitted_at IS NOT NULL
        GROUP BY ea.exam_id, e.level_id, l.level_number, l.track_id
        """,
    ),
    (
        "2. skill stars across every track",
        "SELECT skill_id, stars FROM user_skill_stars WHERE user_id = ?",
    ),
    (
        "3. completed tracks",
        "SELECT count(*)::int AS n FROM user_track_progress "
        "WHERE user_id = ? AND completed_at IS NOT NULL",
    ),
    (
        "4. code depth (largest tests_total among all-passing submissions)",
        "SELECT coalesce(max(tests_total), 0)::int AS n FROM code_submissions "
        "WHERE user_id = ? AND passed = true "
        "AND tests_total IS NOT NULL AND tests_total = tests_passed",
    ),
    (
        "5. passed levels, deduplicated",
        "SELECT count(DISTINCT e.level_id)::int AS n FROM exam_attempts ea "
        "INNER JOIN exams e ON e.id = ea.exam_id WHERE ea.user_id = ? AND ea.passed = true",
    ),
    (
        "6. per-badge definitions (the catalogue the award path reads)",
        "SELECT id, criteria_type, criteria_value_json FROM badges ORDER BY sort_order LIMIT 1",
    ),
]


def sql(statement: str) -> tuple[int, str]:
    result = subprocess.run(
        [str(PYTHON), str(VPS), "sql"], input=statement.encode("utf-8"),
        capture_output=True, timeout=600,
    )
    out = ((result.stdout or b"") + (result.stderr or b"")).decode("utf-8", "replace")
    return result.returncode, out.strip()


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the badge state queries against the live database.")
    args = parser.parse_args()

    users = sql("SELECT id FROM users LIMIT 1")[1]
    if not users:
        print("  no users in the database, so there is nothing to parameterise against")
        print("  (that is a finding: the award path cannot be exercised end to end without one)")
        return 1
    user_id = users.splitlines()[0].strip()
    print(f"  sample user: {user_id[:16]}...")
    print()

    failures = 0
    for label, statement in QUERIES:
        prepared = statement.replace("?", f"'{user_id}'")
        code, out = sql(prepared)
        # psql exits 0 on a successful empty result, so a non-zero code or an ERROR line is the signal.
        error = "ERROR" in out or "FATAL" in out
        if error or code != 0:
            failures += 1
            print(f"  FAIL  {label}")
            for line in out.splitlines()[:4]:
                print(f"          {line}")
        else:
            rows = len([line for line in out.splitlines() if line.strip()])
            preview = out.splitlines()[0][:70] if rows else "(no rows)"
            print(f"  ok    {label}")
            print(f"          {rows} row(s)  {preview}")

    print()
    if failures:
        print(f"  {failures} of {len(QUERIES)} quer(y/ies) failed against the live schema.")
        print("  The unit tests cannot see this: they run against a fake Db and never reach Postgres.")
        return 1
    print(f"  all {len(QUERIES)} quer(y/ies) valid against the live schema")
    return 0


if __name__ == "__main__":
    sys.exit(main())
