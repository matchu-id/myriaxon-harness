#!/usr/bin/env bash
# Re-run the exact reproduction with the adaptive-retry fix, and report the observable outcome.
#
# The previous run of this script is what found the bug: same image, same user, same config, same
# instruction, and the turn ended with "Response truncated at the model's max output length." and no
# file. This runs the same thing against the fixed code, so the check is a comparison rather than a
# fresh claim.
set -u

IMAGE="alexgshaw/regex-log:20251031"
NAME="myr-retry"
REPO=/mnt/c/Users/arian/OneDrive/ドキュメント/Myriaxon
WHEEL=$(ls -1 "$REPO"/.uv/wheel/myriaxon-*.whl 2>/dev/null | head -1)
CONFIG_SRC="${1:-}"

cleanup() { docker rm -f "$NAME" > /dev/null 2>&1 || true; }
trap cleanup EXIT
cleanup

[ -f "$CONFIG_SRC" ] || { echo "usage: $0 <config.json>"; exit 2; }
[ -n "$WHEEL" ] || { echo "no wheel"; exit 2; }

docker run -d --name "$NAME" -w /app "$IMAGE" sleep 1800 > /dev/null || exit 2

# Each step is checked. The previous version ran apt with its output discarded and then created a venv
# that silently did not exist -- `python3: not found`, `/opt/myriaxon-venv/bin/python: not found` --
# so the "turn" ran nothing and the script reported a missing file as though the agent had failed.
step() { printf '  %s ... ' "$1"; }
ok()   { echo "ok"; }
die()  { echo "FAILED: $1"; docker exec "$NAME" sh -c 'ls -l /opt 2>/dev/null; which python3 || echo "  no python3"' 2>&1 | head -5; exit 1; }

step "apt"
docker exec "$NAME" sh -c 'apt-get update -qq && apt-get install -y -qq python3 python3-venv python3-pip git' > /tmp/apt.log 2>&1 \
  || die "apt-get failed (see /tmp/apt.log)"
docker exec "$NAME" python3 --version > /dev/null 2>&1 || die "python3 is still missing after apt"
ok

step "venv"
docker exec "$NAME" sh -c 'python3 -m venv /opt/myriaxon-venv' || die "venv creation failed"
docker exec "$NAME" test -x /opt/myriaxon-venv/bin/python || die "the venv has no python"
ok

step "wheel"
docker cp "$WHEEL" "$NAME:/tmp/$(basename "$WHEEL")" > /dev/null || die "wheel copy failed"
docker exec "$NAME" /opt/myriaxon-venv/bin/pip install -q --no-cache-dir "/tmp/$(basename "$WHEEL")" \
  || die "pip install failed"
docker exec "$NAME" /opt/myriaxon-venv/bin/python -m myriaxon --version || die "myriaxon does not run"
ok
echo "  image + wheel ready  ($(basename "$WHEEL"))"

docker exec "$NAME" sh -c 'mkdir -p /tmp/myriaxon-config && chmod 700 /tmp/myriaxon-config'
docker cp "$CONFIG_SRC" "$NAME:/tmp/myriaxon-config/config.json" > /dev/null
docker exec "$NAME" sh -c 'chmod 600 /tmp/myriaxon-config/config.json && chown -R root:root /tmp/myriaxon-config'
docker cp "$REPO/.uv/tb20/terminal-bench/regex-log/instruction.md" "$NAME:/tmp/myriaxon-prompt.txt" > /dev/null
docker exec "$NAME" sh -c 'chmod 644 /tmp/myriaxon-prompt.txt'

echo
echo "=== the turn (full output, unfiltered) ==="
docker exec "$NAME" sh -c 'cd /app && MYRIAXON_CONFIG_DIR=/tmp/myriaxon-config /opt/myriaxon-venv/bin/python -m myriaxon --mode auto -p "$(cat /tmp/myriaxon-prompt.txt)"' 2>&1 | tail -35

echo
echo "=== DID IT WRITE THE FILE? ==="
docker exec "$NAME" sh -c 'if [ -f /app/regex.txt ]; then echo "  regex.txt EXISTS:"; cat /app/regex.txt; else echo "  regex.txt MISSING"; fi'

echo
echo "=== does the task's own verifier pass? ==="
# Two invocation mistakes were made here and both produced a wall of unrelated errors that read as a
# broken verifier:
#   * `sh` instead of `bash` -- the script is `#!/bin/bash` and uses `source`, so dash dies at line 11
#     with "syntax error: unexpected end of file";
#   * the file was copied with CRLF endings, so every line ended in `$'\r': command not found` and
#     even `apt-get update` arrived as `update\r`. `tr -d '\r'` fixes it, and the same applies to the
#     pytest file.
docker exec "$NAME" sh -c 'command -v bash >/dev/null || (apt-get update -qq && apt-get install -y -qq bash)'
docker exec "$NAME" sh -c 'mkdir -p /logs/verifier /tests'
# The task's `test.sh` runs pytest against the ABSOLUTE path `/tests/test_outputs.py`, which is where
# Harbor mounts the task's test directory. Staging it in /tmp produced
# `ERROR: file or directory not found: /tests/test_outputs.py`, then "no tests ran", then a reward of
# 0 -- a failure that looks like the agent's answer being wrong when nothing was ever tested.
for f in tests/test.sh tests/test_outputs.py; do
  docker cp "$REPO/.uv/tb20/terminal-bench/regex-log/$f" "$NAME:/tmp/raw-$(basename "$f")" > /dev/null
done
docker exec "$NAME" sh -c "tr -d '\r' < /tmp/raw-test.sh > /tests/test.sh && chmod +x /tests/test.sh"
docker exec "$NAME" sh -c "tr -d '\r' < /tmp/raw-test_outputs.py > /tests/test_outputs.py"
docker exec "$NAME" sh -c 'ls -l /tests/'
docker exec "$NAME" bash -c 'cd /app && bash /tests/test.sh' 2>&1 | tail -16
echo "  --- reward ---"
docker exec "$NAME" sh -c 'cat /logs/verifier/reward.txt 2>/dev/null || echo "no reward file"'

echo
echo "=== done ==="
