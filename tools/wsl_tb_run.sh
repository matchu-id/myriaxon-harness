#!/usr/bin/env bash
# Launch the Terminal-Bench suite inside WSL, detached, and report where the log is.
#
# Runs detached on purpose. `wsl.exe` cannot be waited on from the agent's session -- a minimal
# `wsl.exe -d Ubuntu -- echo hello` with a 30-second cap timed out, while the same command from an
# ordinary shell returns at once. A foreground run would therefore be killed the moment the caller
# gave up, mid-trial, leaving a half-built container and no result.
#
# `setsid` is what makes the child independent: without it the process stays in the controlling
# terminal's session and dies when that terminal goes away.
#
# Usage:
#   wsl_tb_run.sh --tasks mailman regex-log
#   wsl_tb_run.sh --all --n-concurrent 3
#   wsl_tb_run.sh --status
set -u

REPO=/mnt/c/Users/arian/OneDrive/ドキュメント/Myriaxon
PY="$REPO/.venv-harbor-linux/bin/python"
LOG_DIR="$REPO/.uv/tb-logs"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/run-$(date +%Y%m%d-%H%M%S).log"

if [ "${1:-}" = "--status" ]; then
  echo "=== recent runs ==="
  ls -1t "$LOG_DIR" 2>/dev/null | head -5 | sed 's/^/  /' || echo "  none"
  newest=$(ls -1t "$LOG_DIR"/*.log 2>/dev/null | head -1)
  if [ -n "$newest" ]; then
    echo
    echo "=== tail of $newest ==="
    tail -30 "$newest" | sed 's/^/  /'
  fi
  echo
  echo "=== is a run in progress? ==="
  pgrep -af "harbor run" | sed 's/^/  /' || echo "  no harbor process running"
  exit 0
fi

if [ ! -x "$PY" ]; then
  echo "FAILED: $PY is missing. Run tools/wsl_harbor_setup.sh first." >&2
  exit 2
fi

echo "  launching detached; log: $LOG"
# PYTHONPATH is what lets Harbor resolve the custom agent. Passing
# `-a tools.harbor_myriaxon:MyriaxonAgent` made Harbor import the module by name, and it failed with
# `No module named 'tools'` -- Harbor's own process has no reason to have this repo on its path, and
# adding `tools/__init__.py` alone was not enough because the *parent* directory is what must be
# importable. Setting it here rather than inside the Python entry point keeps it correct for the
# subprocesses Harbor spawns as well.
export PYTHONPATH="$REPO${PYTHONPATH:+:$PYTHONPATH}"
setsid nohup "$PY" "$REPO/tools/wsl_tb_run.py" "$@" > "$LOG" 2>&1 &
disown || true

sleep 12
echo
echo "=== first output ==="
tail -20 "$LOG" | sed 's/^/  /'
echo
echo "  poll with:  tools/wsl_tb_run.sh --status"
