
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
