#!/bin/bash
# Read the remainder of the level schema, find a working SQL code question to copy, and back up the
# track before any edit.
#
# The backup is the point of this run. /srv/certivo is not a git repository, so there is no undo --
# a malformed write would leave 20 levels of hand-written content unrecoverable.
cd /srv/certivo || exit 1

echo "=== remainder of levelContentJsonSchema ==="
sed -n '230,300p' shared/src/contentSchemas.ts

echo
echo "=== an existing SQL code question, to copy the exact shape ==="
python3 - <<'PY'
import json, glob, pathlib
found = 0
for path in sorted(glob.glob("content/tracks/*/levels/*.json")):
    if "/ai-ml-engineer/" in path:
        continue
    try:
        data = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    except Exception:
        continue
    for q in data.get("questions") or []:
        spec = q.get("code") or {}
        if spec.get("language") == "sql":
            print(f"  --- {path}")
            print(json.dumps(q, indent=4, ensure_ascii=False)[:2200])
            found += 1
            break
    if found >= 1:
        break
if not found:
    print("  no sql code question found anywhere")
PY

echo
echo "=== ai-ml-engineer focus skills per level, from the content itself ==="
python3 - <<'PY'
import json, pathlib
for n in range(1, 21):
    path = pathlib.Path(f"content/tracks/ai-ml-engineer/levels/{n:02d}.json")
    if not path.is_file():
        print(f"  {n:02d}: MISSING")
        continue
    data = json.loads(path.read_text(encoding="utf-8"))
    qs = data.get("questions") or []
    skills = sorted({q.get("skill") for q in qs})
    exam = data.get("exam") or {}
    print(f"  {n:02d}: {len(qs):>3}q  perAttempt={exam.get('questionsPerAttempt')}  "
          f"thresh={exam.get('passThresholdPct')}%  case={(data.get('caseStudy') or {}).get('title','')[:44]}")
    print(f"       skills: {', '.join(skills)}")
PY

echo
echo "=== BACKUP before anything is written ==="
STAMP=$(date +%Y%m%dT%H%M%S)
DEST="/srv/certivo/backups/ai-ml-engineer-$STAMP"
mkdir -p "$DEST"
cp -a content/tracks/ai-ml-engineer "$DEST/"
echo "  copied to $DEST"
ls -1 "$DEST/ai-ml-engineer/levels" | wc -l | sed 's/^/  level files backed up: /'
du -sh "$DEST" 2>/dev/null | sed 's/^/  size: /'
