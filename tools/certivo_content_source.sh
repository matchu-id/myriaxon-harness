#!/bin/bash
# Find out whether Certivo's question content lives in the JSON files or the database.
#
# This decides where the rework has to be applied. If the seeder loads the files into tables, editing
# the files and re-seeding is the correct path. If the database is the source of truth, editing files
# would change nothing the app serves -- a silent no-op that looks like a successful edit.
cd /srv/certivo || exit 1

echo "=== who reads content/tracks ==="
grep -rn "content/tracks" --include=*.ts --include=*.mjs --include=*.js . 2>/dev/null \
  | grep -v node_modules | head -12
echo
echo "=== seed scripts ==="
ls -1 scripts/ 2>/dev/null | head -20
echo
echo "=== anything named seed ==="
find . -maxdepth 3 -name "*seed*" -not -path "./node_modules/*" 2>/dev/null | head -10
echo
echo "=== does the seeder insert questions? ==="
for f in $(grep -rl "content/tracks" --include=*.ts --include=*.mjs --include=*.js . 2>/dev/null | grep -v node_modules | head -3); do
  echo "  --- $f"
  grep -n "insert\|questions\|kind\|codeSpec\|essaySpec" "$f" 2>/dev/null | head -12 | sed 's/^/    /'
done
echo
echo "=== actual row counts in postgres ==="
docker exec certivo-db-1 psql -U postgres -d certivo -t -c "select 'tracks='||(select count(*) from tracks)||' levels='||(select count(*) from levels)||' questions='||(select count(*) from questions)||' options='||(select count(*) from question_options);" 2>&1 | head -4
echo
echo "=== question kinds actually stored, for ai-ml-engineer ==="
docker exec certivo-db-1 psql -U postgres -d certivo -t -c "select q.kind, count(*) from questions q join levels l on l.id=q.level_id join tracks t on t.id=l.track_id where t.slug='ai-ml-engineer' group by q.kind;" 2>&1 | head -6
echo
echo "=== same for data-analyst, which has been reworked ==="
docker exec certivo-db-1 psql -U postgres -d certivo -t -c "select q.kind, count(*) from questions q join levels l on l.id=q.level_id join tracks t on t.id=l.track_id where t.slug='data-analyst' group by q.kind;" 2>&1 | head -6
