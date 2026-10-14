#!/bin/bash
# Establish how content gets from the JSON files into the running app.
#
# This has to be settled before any edit. If the seeder reads `content/tracks` into Postgres, editing
# the files is correct. If the database is the source of truth, editing files is a silent no-op that
# looks like a successful change -- the worst possible outcome for a content rework.
cd /srv/certivo || exit 1

echo "=== publish-tracks.ts, the content pipeline ==="
sed -n '1,60p' content/scripts/publish-tracks.ts 2>/dev/null

echo
echo "=== how is it invoked? ==="
grep -rn "publish-tracks\|publishTracks" package.json Dockerfile docker-compose.yml scripts/ 2>/dev/null | head -10

echo
echo "=== db credentials, from compose ==="
grep -n "POSTGRES\|DATABASE_URL\|PG" docker-compose.yml 2>/dev/null | head -12

echo
echo "=== table row counts, with the compose credentials ==="
DBU=$(grep -oP 'POSTGRES_USER:\s*\K\S+' docker-compose.yml | head -1)
DBN=$(grep -oP 'POSTGRES_DB:\s*\K\S+' docker-compose.yml | head -1)
echo "  user=$DBU db=$DBN"
docker exec certivo-db-1 psql -U "${DBU:-postgres}" -d "${DBN:-certivo}" -t -c \
  "select 'tracks='||(select count(*) from tracks)||' levels='||(select count(*) from levels)||' questions='||(select count(*) from questions)||' options='||(select count(*) from question_options);" 2>&1 | head -3

echo
echo "=== kinds stored for ai-ml-engineer ==="
docker exec certivo-db-1 psql -U "${DBU:-postgres}" -d "${DBN:-certivo}" -t -c \
  "select q.kind, count(*) from questions q join levels l on l.id=q.level_id join tracks t on t.id=l.track_id where t.slug='ai-ml-engineer' group by q.kind;" 2>&1 | head -5
