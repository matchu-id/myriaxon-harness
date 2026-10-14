#!/usr/bin/env bash
# Snapshot the task container while a Harbor trial is running.
#
# Harbor records "Command outputs captured" and nothing else, so a failing trial says only that it
# failed. Three runs have now scored 0.0 with no exception, while the same sequence run by hand writes
# the file. The remaining difference has to be in what Harbor sets up, so this looks at the live
# container instead of at Harbor's summary of it: the prompt file, the config, the ownership of /app,
# and the agent's own log if it wrote one.
set -u

REPO=/mnt/c/Users/arian/OneDrive/ドキュメント/Myriaxon
PY="$REPO/.venv-harbor-linux/bin/python"
LOG_DIR="$REPO/.uv/tb-logs"
OUT="$LOG_DIR/snap-$(date +%Y%m%d-%H%M%S).log"

cd "$REPO" || exit 2
export PYTHONPATH="$REPO"
export HOME="$REPO/.uv/home-linux"
export TEMP="$REPO/.uv/tmp-linux"; export TMP="$TEMP"; export TMPDIR="$TEMP"

setsid nohup "$PY" "$REPO/tools/wsl_tb_run.py" --tasks regex-log --job-name tb-snap > "$OUT" 2>&1 &
disown || true
echo "  trial started; probing every 10s"

for round in $(seq 1 24); do
  sleep 10
  cid=$(docker ps -q --filter "name=env-main" | head -1)
  [ -z "$cid" ] && continue

  echo "===================== round $round  $(date +%H:%M:%S) ====================="
  echo "--- /app ownership and contents ---"
  docker exec "$cid" sh -c 'ls -ld /app; ls -la /app' 2>&1 | head -12

  echo "--- the prompt file ---"
  docker exec "$cid" sh -c 'ls -l /tmp/myriaxon-prompt.txt 2>&1; echo "  --- first line ---"; head -1 /tmp/myriaxon-prompt.txt 2>&1' | head -6

  echo "--- the config ---"
  docker exec "$cid" sh -c 'ls -l /tmp/myriaxon-config/ 2>&1; python3 -c "import json;d=json.load(open(\"/tmp/myriaxon-config/config.json\"));print(\"  provider\",d[\"provider\"],\"model\",d[\"model\"])" 2>&1' | head -6

  echo "--- is myriaxon installed ---"
  docker exec "$cid" sh -c '/opt/myriaxon-venv/bin/python -m myriaxon --version 2>&1' | head -2

  echo "--- processes inside ---"
  docker exec "$cid" sh -c 'ps -eo user,pid,args 2>/dev/null | grep -i "myriaxon\|python" | grep -v grep | head -5' 2>&1

  echo "--- myriaxon's own log dir, if it wrote one ---"
  docker exec "$cid" sh -c 'find /app/.myriaxon /tmp/myriaxon-config -type f 2>/dev/null | head -8' 2>&1

  # Once the file exists the trial is effectively decided; stop probing.
  if docker exec "$cid" sh -c 'test -f /app/regex.txt' 2>/dev/null; then
    echo "--- regex.txt EXISTS ---"
    docker exec "$cid" sh -c 'cat /app/regex.txt'
  fi
  echo
done

echo "=== trial output ==="
tail -16 "$OUT"
