#!/usr/bin/env bash
# Watch what Harbor actually does to containers during a trial.
#
# One hypothesis explains every symptom so far: the turn runs, produces no output Harbor captures, and
# the verifier then reports `/app/regex.txt` missing even though a hand-run against the same image
# writes it. If the agent and the verifier run in *different* containers, the agent's files are simply
# not there to be verified, and the reward is 0 with no exception at any point.
#
# This starts a Harbor trial and samples `docker ps` every two seconds while it runs, recording every
# container that appears. If two containers exist at once during verification, the hypothesis holds.
set -u

REPO=/mnt/c/Users/arian/OneDrive/ドキュメント/Myriaxon
LOG_DIR="$REPO/.uv/tb-logs"
PY="$REPO/.venv-harbor-linux/bin/python"
WATCH=/tmp/harbor-containers.log
OUT="$LOG_DIR/watch-$(date +%Y%m%d-%H%M%S).log"

: > "$WATCH"

echo "=== starting a Harbor trial ==="
cd "$REPO" || exit 2
export PYTHONPATH="$REPO"
export HOME="$REPO/.uv/home-linux"
export TEMP="$REPO/.uv/tmp-linux"; export TMP="$TEMP"; export TMPDIR="$TEMP"

setsid nohup "$PY" "$REPO/tools/wsl_tb_run.py" --tasks regex-log --job-name tb-watch > "$OUT" 2>&1 &
disown || true

echo "  trial log: $OUT"
echo "  sampling containers every 2s ..."

for _ in $(seq 1 130); do
  line=$(docker ps --format '{{.Names}} | {{.Image}} | {{.Status}}' 2>/dev/null | grep -v '^$' | tr '\n' ';')
  stamp=$(date +%H:%M:%S)
  echo "$stamp :: ${line:-none}" >> "$WATCH"
  sleep 2
done

echo
echo "=== distinct container states seen ==="
awk -F':: ' '{print $2}' "$WATCH" | sort -u | head -20

echo
echo "=== moments where TWO OR MORE containers ran at once ==="
awk -F':: ' '{n=gsub(/;/,";"); if (n >= 2) print}' "$WATCH" | head -20
echo "  (if this is empty, the agent and verifier never overlapped)"

echo
echo "=== last 12 samples ==="
tail -12 "$WATCH"

echo
echo "=== completed job ==="
tail -14 "$OUT"
