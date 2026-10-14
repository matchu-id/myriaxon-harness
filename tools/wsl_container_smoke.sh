#!/usr/bin/env bash
# Prove that the Myriaxon CLI runs inside the real Terminal-Bench task image.
#
# This exists because the first Harbor run produced an empty agent-output directory: Myriaxon's command
# executed in the container and wrote nothing. The same invocation works on the Windows host, so the
# difference is the container, and a Harbor trial takes four minutes to tell us one bit. This talks to
# the container directly so the failure can actually be read.
#
# Run with LF endings; a CRLF copy made docker read the image name as `alexgshaw/regex-log:20251031\r`,
# which surfaced as `docker: invalid reference format`.
set -u

IMAGE="alexgshaw/regex-log:20251031"
NAME="myr-smoke"
REPO=/mnt/c/Users/arian/OneDrive/ドキュメント/Myriaxon
WHEEL="$REPO/.uv/wheel/myriaxon-1.2.0-py3-none-any.whl"

cleanup() { docker rm -f "$NAME" > /dev/null 2>&1 || true; }
trap cleanup EXIT
cleanup

echo "=== 1. start the real task image ==="
docker run -d --name "$NAME" -w /app "$IMAGE" sleep 1200 > /dev/null || {
  echo "FAILED to start $IMAGE"; exit 2; }
echo "  started"

echo
echo "=== 2. what is in /app, and does cd /app work? ==="
docker exec "$NAME" sh -c 'ls -la /app 2>&1 | head -6; cd /app 2>/dev/null && echo "  cd /app OK -> $(pwd)" || echo "  cd /app FAILED"'

echo
echo "=== 3. python available in the image? ==="
docker exec "$NAME" sh -c 'python3 --version 2>&1 || echo "  no python3"'

echo
echo "=== 4. install python3-venv and the wheel ==="
docker exec "$NAME" sh -c 'apt-get update -qq && apt-get install -y -qq python3 python3-venv python3-pip' 2>&1 | tail -2
docker exec "$NAME" sh -c 'python3 -m venv /opt/mv' 2>&1 | tail -2
docker cp "$WHEEL" "$NAME:/tmp/m.whl" 2>&1 | tail -1
docker exec "$NAME" /opt/mv/bin/pip install --quiet --no-cache-dir /tmp/m.whl 2>&1 | tail -3

echo
echo "=== 5. does the CLI even start? ==="
docker exec "$NAME" /opt/mv/bin/python -m myriaxon --version 2>&1 | tail -4

echo
echo "=== 6. run a real turn, writing config INSIDE the container ==="
# The key is passed through the environment of `docker exec`, not through a command line, so it does
# not land in this script or in any log of it.
docker exec -e MYRIAXON_API_KEY="$MYRIAXON_API_KEY" \
  -e MYRIAXON_BASE_URL="$MYRIAXON_BASE_URL" \
  -e MYRIAXON_MODEL="$MYRIAXON_MODEL" \
  "$NAME" sh -c 'mkdir -p /tmp/cfg && python3 - <<PY
import json, os
cfg = {
    "provider": "openai_compat",
    "api_key": os.environ["MYRIAXON_API_KEY"],
    "base_url": os.environ["MYRIAXON_BASE_URL"],
    "model": os.environ["MYRIAXON_MODEL"],
}
open("/tmp/cfg/config.json", "w").write(json.dumps(cfg, indent=2))
print("  config written")
PY'

echo "  --- the turn ---"
docker exec -e MYRIAXON_CONFIG_DIR=/tmp/cfg "$NAME" sh -c \
  'cd /app && /opt/mv/bin/python -m myriaxon --mode auto -p "Create a file named smoke.txt containing exactly the word HELLO. Nothing else." 2>&1' | tail -40

echo
echo "=== 7. did it write the file? ==="
docker exec "$NAME" sh -c 'ls -la /app/smoke.txt 2>&1 && echo "--- contents ---" && cat /app/smoke.txt 2>&1'

echo
echo "=== done ==="
