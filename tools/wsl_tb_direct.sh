#!/usr/bin/env bash
# Run the direct Terminal-Bench runner inside WSL, where docker is native.
#
# `wsl.exe` cannot be waited on from the agent's session -- a minimal `wsl.exe -d Ubuntu -- echo hello`
# with a 30-second cap timed out while the same call from an ordinary shell returns at once. So the run
# is detached with setsid and its output goes to a log that is polled, exactly like the Harbor path.
#
# PYTHONPATH is set to the repo because Myriaxon is imported from source here, not from the wheel.
set -u

REPO=/mnt/c/Users/arian/OneDrive/ドキュメント/Myriaxon
PY="$REPO/.venv-harbor-linux/bin/python"
LOG_DIR="$REPO/.uv/tb-logs"
mkdir -p "$LOG_DIR"

if [ "${1:-}" = "--status" ]; then
  newest=$(ls -1t "$LOG_DIR"/direct-*.log 2>/dev/null | head -1)
  [ -n "$newest" ] && { echo "=== $newest ==="; tail -40 "$newest"; } || echo "  no direct runs yet"
  echo
  echo "=== running? ==="
  pgrep -af "tb_direct.py" | sed 's/^/  /' || echo "  no run in progress"
  exit 0
fi

if [ ! -x "$PY" ]; then
  echo "FAILED: $PY is missing" >&2
  exit 2
fi

LOG="$LOG_DIR/direct-$(date +%Y%m%d-%H%M%S).log"
export PYTHONPATH="$REPO${PYTHONPATH:+:$PYTHONPATH}"
export MYRIAXON_CONFIG_DIR="$REPO/.myriaxon"

echo "  launching detached; log: $LOG"
setsid nohup "$PY" "$REPO/tools/tb_direct.py" "$@" > "$LOG" 2>&1 &
disown || true

sleep 20
echo
echo "=== first output ==="
tail -20 "$LOG" | sed 's/^/  /'
