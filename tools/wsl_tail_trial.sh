#!/usr/bin/env bash
# Show the tail of a Harbor trial log, with any model credential masked.
#
# Masking is not decoration. The adapter's first version put the model key into a shell command, and
# Harbor logs commands verbatim -- so reading a log carelessly puts a live credential back on screen.
# The adapter no longer does that, but old logs and any future regression would, so every read of
# these files goes through this filter.
set -u
REPO=/mnt/c/Users/arian/OneDrive/ドキュメント/Myriaxon
cd "$REPO" || exit 2

JOB="${1:-}"
LINES="${2:-80}"
if [ -z "$JOB" ]; then
  echo "usage: wsl_tail_trial.sh <job-name> [lines]" >&2
  exit 2
fi

mask() {
  sed -E 's/("api_key"[[:space:]]*:[[:space:]]*")[^"]+/\1***REDACTED***/g; s/sk_live_[A-Za-z0-9_-]+/***REDACTED***/g; s/sk-[A-Za-z0-9_-]{16,}/***REDACTED***/g'
}

for f in jobs/"$JOB"/*/trial.log; do
  [ -f "$f" ] || continue
  echo "=== $f (last $LINES lines) ==="
  tail -"$LINES" "$f" | mask | sed 's/^/  /'
done

echo
echo "=== job.log (last 20) ==="
tail -20 jobs/"$JOB"/job.log 2>/dev/null | mask | sed 's/^/  /' || echo "  (none)"
