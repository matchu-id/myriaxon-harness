#!/usr/bin/env bash
# Read a Harbor job's own record of what happened, inside WSL.
#
# `jobs/<name>/` holds per-trial `exception.txt`, `result.json` and the agent's own captured output.
# A job that reports a reward of 0.0 is ambiguous on its own: the agent may have failed to start, may
# have started and produced nothing, or may have worked and simply got the answer wrong. Those need
# different fixes, so this dumps the agent's output rather than only the score.
set -u
REPO=/mnt/c/Users/arian/OneDrive/ドキュメント/Myriaxon
cd "$REPO" || exit 2

JOB="${1:-}"
if [ -z "$JOB" ]; then
  echo "usage: wsl_inspect_job.sh <job-name>" >&2
  exit 2
fi

JOB_DIR="jobs/$JOB"
if [ ! -d "$JOB_DIR" ]; then
  echo "no such job. available:"
  ls -1 jobs/ 2>/dev/null | sed 's/^/  /'
  exit 1
fi

echo "=== $JOB ==="
echo "--- files ---"
find "$JOB_DIR" -maxdepth 3 -type f | head -25 | sed 's/^/  /'

echo
echo "--- trial exception.txt, if any ---"
found=0
for f in "$JOB_DIR"/*/exception.txt; do
  [ -f "$f" ] || continue
  found=1
  echo "  $f:"
  head -30 "$f" | sed 's/^/    /'
done
[ "$found" = 0 ] && echo "  none (good: no trial raised)"

echo
echo "--- agent output (this is what the model actually did) ---"
for d in "$JOB_DIR"/*/agent; do
  [ -d "$d" ] || continue
  find "$d" -type f | while read -r f; do
    echo "  --- $f  ($(wc -c < "$f") bytes) ---"
    head -60 "$f" | sed 's/^/    /'
  done
done

echo
echo "--- verifier output (why the score is what it is) ---"
for d in "$JOB_DIR"/*/verifier; do
  [ -d "$d" ] || continue
  find "$d" -type f | while read -r f; do
    echo "  --- $f  ($(wc -c < "$f") bytes) ---"
    head -40 "$f" | sed 's/^/    /'
  done
done

echo
echo "--- reward ---"
cat "$JOB_DIR"/*/verifier/reward.txt 2>/dev/null | sed 's/^/  /' || echo "  (no reward.txt)"

echo
echo "--- result.json stats ---"
"$REPO/.venv-harbor-linux/bin/python" - "$JOB_DIR" <<'PY' 2>/dev/null || echo "  (unreadable)"
import json, sys, pathlib
d = json.loads((pathlib.Path(sys.argv[1]) / 'result.json').read_text(encoding='utf-8'))
stats = d.get('stats', {})
for k in ('n_completed_trials','n_errored_trials','n_input_tokens','n_output_tokens','cost_usd'):
    print(f'  {k}: {stats.get(k)}')
PY
