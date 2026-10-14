#!/usr/bin/env python3
"""Award badges for a real user against the live database, to prove the path end to end.

Everything verified so far stops short of this. `verify_badges.py` reads definitions. `check_badge_sql.py`
proves the queries parse and return rows. Agent B's 19 tests exercise the *logic* against a fake `Db`. None of
them runs `awardBadges` through the real Drizzle client against real rows, so the one thing still unproven is
whether a passing exam actually produces an award — and `user_badges` has been empty for the whole life of this
feature, which means a failure here looks exactly like a system nobody has used.

The initial version of this script wrote SQL by hand to simulate the queries. That is circular: it tests my
transcription of the service, not the service. This runs the module itself, through the same `tsx` the server
uses, against the same database the server uses.

Awarding is idempotent (`user_badges` has a primary key on `(user_id, badge_id)` and re-earning is a no-op),
so running this twice is safe. It prints what was awarded and then re-runs to confirm the second pass awards
nothing, which is the property that makes it safe to call on every submission.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(r"C:\Users\arian\OneDrive\ドキュメント\Myriaxon")
VPS = ROOT / "tools" / "vps.py"
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"

# Runs inside the container: imports the real service, builds the result object from a real submitted attempt
# for the chosen user, and calls awardBadges twice.
PROGRAM = r"""
// Runs inside the container. Imports the real service and drives it against the real database, through the
// same `tsx` the server uses.
//
// Raw SQL goes through `postgres` rather than the Drizzle instance: `getDb()` returns a
// `drizzle-orm/postgres-js` client, which has no `db.all`/`db.execute` on the client object itself. The first
// version of this probe called `db.all(...)` and died with `TypeError: db.all is not a function`.
process.chdir('/app');

const { getDb, getClient } = await import('/app/worker/src/db/client.ts');
const { awardBadges } = await import('/app/worker/src/services/badges.ts');

const url = process.env.DATABASE_URL
  || 'postgres://certivo:932125ef2144bdbe3cc819bbc646cca19ac167a2a5efeb81@db:5432/certivo';
const db = getDb({ DATABASE_URL: url });
const sqlClient = getClient(url);

const rows = await sqlClient`
  SELECT ea.id, ea.exam_id, ea.user_id, ea.score_pct, ea.time_limit_seconds,
         ea.started_at, ea.submitted_at, e.level_id, l.level_number, l.track_id,
         (SELECT count(*) FROM levels l2 WHERE l2.track_id = l.track_id) AS track_levels,
         EXISTS (SELECT 1 FROM questions q WHERE q.exam_id = ea.exam_id AND q.code_spec_json IS NOT NULL) AS had_code
  FROM exam_attempts ea
  JOIN exams e ON e.id = ea.exam_id
  JOIN levels l ON l.id = e.level_id
  WHERE ea.status = 'submitted' AND ea.passed = true AND ea.submitted_at IS NOT NULL
  ORDER BY ea.submitted_at DESC LIMIT 1`;

if (!rows.length) {
  console.log('  NO_ATTEMPT');
  process.exit(0);
}
const r = rows[0];
const result = {
  trackId: r.track_id,
  levelNumber: Number(r.level_number),
  isLastLevel: Number(r.level_number) >= Number(r.track_levels),
  scorePct: Number(r.score_pct),
  hadCodeQuestion: r.had_code === true,
  examId: r.exam_id,
  attemptId: r.id,
  timeLimitSeconds: r.time_limit_seconds === null ? null : Number(r.time_limit_seconds),
  startedAt: r.started_at,
  submittedAt: r.submitted_at,
  status: 'submitted',
};
console.log('  user      :', String(r.user_id).slice(0, 16) + '...');
console.log('  exam      :', r.exam_id, '| level', r.level_number, 'of', r.track_levels);
console.log('  score     :', r.score_pct + '%', '| code question:', result.hadCodeQuestion);

const first = await awardBadges(db, r.user_id, result);
console.log('  pass 1 awarded:', first.length ? first.join(', ') : '(none)');
const second = await awardBadges(db, r.user_id, result);
console.log('  pass 2 awarded:', second.length ? second.join(', ') : '(none)');
console.log('  IDEMPOTENT:', second.length === 0 ? 'yes' : 'NO');

const total = await sqlClient`SELECT count(*)::int AS n FROM user_badges WHERE user_id = ${r.user_id}`;
console.log('  user_badges now:', total[0].n);
const which = await sqlClient`SELECT badge_id FROM user_badges WHERE user_id = ${r.user_id} ORDER BY badge_id`;
if (which.length) console.log('  held      :', which.map((x) => x.badge_id).join(', '));
await sqlClient.end();
"""


def main() -> int:
    parser = argparse.ArgumentParser(description="Award badges for a real user against the live database.")
    args = parser.parse_args()

    script = ROOT / "tools" / "_award_probe.mjs"
    script.write_text(PROGRAM, encoding="utf-8", newline="\n")

    put = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=25", "admin-certivo@139.190.97.11",
         "cat > /tmp/award_probe.mjs"],
        input=PROGRAM.encode("utf-8"), capture_output=True, timeout=300,
    )
    if put.returncode != 0:
        print(f"  upload failed: {(put.stderr or b'').decode('utf-8', 'replace')[:300]}")
        return 1

    run = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=25", "admin-certivo@139.190.97.11",
         "docker cp /tmp/award_probe.mjs certivo-app-1:/app/award_probe.mjs && "
         "docker exec -w /app certivo-app-1 node_modules/.bin/tsx /app/award_probe.mjs"],
        capture_output=True, timeout=600,
    )
    out = ((run.stdout or b"") + (run.stderr or b"")).decode("utf-8", "replace")
    for line in out.strip().splitlines():
        print(f"  {line}" if line.startswith("  ") else f"    {line}")

    if "NO_ATTEMPT" in out:
        print()
        print("  No submitted, passed attempt exists for any user, so awardBadges cannot be exercised")
        print("  against real data. That is the reason user_badges is empty, and it means every")
        print("  criteria type is still unproven in production however good the unit tests are.")
        return 1
    if "IDEMPOTENT: yes" not in out:
        # Report a crash as a crash. The first version checked only for the absence of a second award, so a
        # `TypeError` printed the idempotency warning about a run that never reached the second call.
        if "pass 2 awarded" not in out:
            print()
            print("  the probe did not reach the second award call, so idempotency is unproven")
        else:
            print()
            print("  WARNING: the second pass awarded something, so awarding is not idempotent.")
        return 1
    if "pass 1 awarded: (none)" in out:
        print()
        print("  The probe ran and awarded nothing. That is a real result, not a pass: it means a passed")
        print("  exam produced no badge, so either the criteria did not match or the definitions are absent.")
        return 1
    print()
    print("  award path exercised end to end against the live database")
    return 0


if __name__ == "__main__":
    sys.exit(main())
