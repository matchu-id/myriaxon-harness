#!/usr/bin/env bash
# Reproduce Harbor's exact turn, as root, with the exact config path it uses.
#
# The snapshot showed the turn running correctly -- root, Myriaxon installed, the prompt file present,
# `/app/.myriaxon` created -- yet `/app/regex.txt` never appeared. So the failure is inside the turn,
# and Myriaxon's own log is the only place that says why. This runs it where that log can be read
# afterwards instead of being thrown away with the container.
set -u

IMAGE="alexgshaw/regex-log:20251031"
NAME="myr-exact"
REPO=/mnt/c/Users/arian/OneDrive/ドキュメント/Myriaxon
WHEEL=$(ls -1 "$REPO"/.uv/wheel/myriaxon-*.whl 2>/dev/null | head -1)
CONFIG_SRC="${1:-}"

cleanup() { docker rm -f "$NAME" > /dev/null 2>&1 || true; }
trap cleanup EXIT
cleanup

[ -f "$CONFIG_SRC" ] || { echo "usage: $0 <config.json>"; exit 2; }
docker run -d --name "$NAME" -w /app "$IMAGE" sleep 1800 > /dev/null || exit 2

docker exec "$NAME" sh -c 'apt-get update -qq && apt-get install -y -qq python3 python3-venv python3-pip git' > /dev/null 2>&1
docker exec "$NAME" sh -c 'python3 -m venv /opt/myriaxon-venv'
docker cp "$WHEEL" "$NAME:/tmp/$(basename "$WHEEL")" > /dev/null
docker exec "$NAME" /opt/myriaxon-venv/bin/pip install -q --no-cache-dir "/tmp/$(basename "$WHEEL")" > /dev/null 2>&1

# The config, with the SAME mode Harbor sets: 600 root-owned, in the same directory. My earlier manual
# test used /tmp/cfg with default ownership, so this path was never exercised.
docker exec "$NAME" sh -c 'mkdir -p /tmp/myriaxon-config && chmod 700 /tmp/myriaxon-config'
docker cp "$CONFIG_SRC" "$NAME:/tmp/myriaxon-config/config.json" > /dev/null
docker exec "$NAME" sh -c 'chmod 600 /tmp/myriaxon-config/config.json && chown -R root:root /tmp/myriaxon-config'

# The real instruction, taken from the task rather than shortened -- the shortening is another
# difference from every previous manual test, and regex-log's instruction is long and fenced.
docker cp "$REPO/.uv/tb20/terminal-bench/regex-log/instruction.md" "$NAME:/tmp/myriaxon-prompt.txt" > /dev/null
docker exec "$NAME" sh -c 'chmod 644 /tmp/myriaxon-prompt.txt'

echo "=== config as the turn will see it ==="
docker exec "$NAME" sh -c 'ls -l /tmp/myriaxon-config/; /opt/myriaxon-venv/bin/python -c "import json;d=json.load(open(\"/tmp/myriaxon-config/config.json\"));print(\"  provider\",d[\"provider\"],\"| model\",d[\"model\"],\"| base\",d[\"base_url\"])"'

echo
echo "=== myriaxon with the real instruction (root, 600 config) ==="
docker exec "$NAME" sh -c 'cd /app && MYRIAXON_CONFIG_DIR=/tmp/myriaxon-config /opt/myriaxon-venv/bin/python -m myriaxon --mode auto -p "$(cat /tmp/myriaxon-prompt.txt)"' 2>&1 | tail -30
echo "  exit=$?"

echo
echo "=== /app afterwards ==="
docker exec "$NAME" sh -c 'ls -la /app/'

echo
echo "=== myriaxon's own log -- this is what Harbor never shows ==="
docker exec "$NAME" sh -c 'find /tmp/myriaxon-config /app/.myriaxon -name "*.log" 2>/dev/null | head -3 | while read -r f; do echo "--- $f ---"; tail -40 "$f"; done'

echo
echo "=== done ==="
