#!/bin/bash
# Certivo: version-control state and whether the running app is healthy.
cd /srv/certivo 2>/dev/null || exit 1

echo "=== is this a git repo? ==="
if [ -d .git ]; then
  echo "  yes"
  git log --oneline -3 2>/dev/null
  echo "  branch: $(git rev-parse --abbrev-ref HEAD 2>/dev/null)"
  echo "  changed: $(git status --porcelain 2>/dev/null | wc -l) file(s)"
else
  echo "  NO .git -- every edit Claude Code made is unsaved, with no history to fall back on"
fi
echo
echo "=== backups directory, if that is the safety net instead ==="
ls -1t backups/ 2>/dev/null | head -8
echo "  total: $(ls -1 backups/ 2>/dev/null | wc -l) entries"
echo
echo "=== app health ==="
docker ps --format '  {{.Names}}  {{.Status}}  {{.Ports}}' 2>/dev/null
echo
echo "=== does it answer? ==="
curl -s -o /dev/null -w '  http://127.0.0.1:3000 -> %{http_code}\n' --max-time 8 http://127.0.0.1:3000/ 2>/dev/null
echo
echo "=== recent app log, for errors ==="
docker logs --tail 15 certivo-app-1 2>&1 | sed 's/^/  /'
echo
echo "=== content validation: are the level files well formed? ==="
count=0; bad=0
for f in content/tracks/*/levels/*.json; do
  [ -f "$f" ] || continue
  count=$((count+1))
  python3 -c "import json,sys; json.load(open(sys.argv[1]))" "$f" 2>/dev/null || { bad=$((bad+1)); echo "  INVALID: $f"; }
done
echo "  checked $count level file(s), $bad invalid"
