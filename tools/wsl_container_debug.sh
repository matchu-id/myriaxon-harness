#!/usr/bin/env bash
# Run the adapter's exact container command by hand, so the failure can be read.
#
# Harbor's trial.log records the command and then the words "Command outputs captured" -- it does not
# show the output. A four-minute trial therefore reports one bit, and the agent's output directory has
# been empty across two runs. This reproduces the identical sequence in the identical image and prints
# everything, which is what the trial log refuses to do.
set -u

IMAGE="alexgshaw/regex-log:20251031"
NAME="myr-dbg"
REPO=/mnt/c/Users/arian/OneDrive/ドキュメント/Myriaxon
WHEEL="$REPO/.uv/wheel/myriaxon-1.2.0-py3-none-any.whl"
LOG=/tmp/myr-dbg.log
exec > >(tee "$LOG") 2>&1

cleanup() { docker rm -f "$NAME" > /dev/null 2>&1 || true; }
trap cleanup EXIT
cleanup

echo "=== 1. start the task image exactly as Harbor does ==="
docker run -d --name "$NAME" -w /app "$IMAGE" sleep 1800 > /dev/null || { echo "start failed"; exit 2; }
echo "  running"

echo
echo "=== 2. install, mirroring the adapter's install() ==="
docker exec "$NAME" sh -c 'apt-get update -qq && apt-get install -y -qq python3 python3-pip python3-venv git' 2>&1 | tail -2
docker exec "$NAME" sh -c 'python3 -m venv /opt/myriaxon-venv' 2>&1 | tail -2
# The filename must be a valid wheel name -- `<distribution>-<version>-<pytag>-<abitag>-<platform>.whl`.
# Copying it to `/tmp/m.whl` failed with `m.whl is not a valid wheel filename`, and the failure was easy
# to miss because the surrounding steps still reported success: the venv was created, pip printed one
# line, and the install was silently skipped. The adapter uploads the real filename for this reason.
docker cp "$WHEEL" "$NAME:/tmp/$(basename "$WHEEL")" > /dev/null 2>&1
docker exec "$NAME" /opt/myriaxon-venv/bin/pip install --no-cache-dir "/tmp/$(basename "$WHEEL")" 2>&1 | tail -4
docker exec "$NAME" /opt/myriaxon-venv/bin/python -c 'import myriaxon, sys; print("  installed myriaxon", myriaxon.__version__ if hasattr(myriaxon,"__version__") else "ok")' 2>&1 | tail -3

echo
echo "=== 3. the config the adapter uploads (written on the Windows side, copied in) ==="
# Environment variables do not cross from the Windows process into the WSL distro, which is why this
# file is built outside and copied rather than assembled in here from $MYRIAXON_API_KEY.
CONFIG_SRC="${1:-}"
if [ -z "$CONFIG_SRC" ] || [ ! -f "$CONFIG_SRC" ]; then
  echo "  FAILED: no config file passed as \$1"
  exit 2
fi
docker exec "$NAME" sh -c 'mkdir -p /tmp/myriaxon-config && chmod 700 /tmp/myriaxon-config'
docker cp "$CONFIG_SRC" "$NAME:/tmp/myriaxon-config/config.json" > /dev/null 2>&1
docker exec "$NAME" sh -c 'chmod 600 /tmp/myriaxon-config/config.json && echo "  config written"'
docker exec "$NAME" sh -c 'python3 -c "import json;d=json.load(open(\"/tmp/myriaxon-config/config.json\"));print(\"  provider:\",d[\"provider\"],\"| model:\",d[\"model\"],\"| key chars:\",len(d[\"api_key\"]))"'

echo
echo "=== 4. version check, the adapter's first step in run() ==="
docker exec "$NAME" /opt/myriaxon-venv/bin/python -m myriaxon --version 2>&1 | tail -5

echo
echo "=== 5. the EXACT command the adapter runs ==="
docker exec "$NAME" sh -c 'cd /app && MYRIAXON_CONFIG_DIR=/tmp/myriaxon-config /opt/myriaxon-venv/bin/python -m myriaxon --mode auto -p "Write a regex expression that matches dates in the format YYYY-MM-DD appearing in lines that contain an IPv4 address in a log file. Save your regex in /app/regex.txt"' 2>&1 | tail -40
echo "  (exit above: $?)"

echo
echo "=== 6. anything written? ==="
docker exec "$NAME" sh -c 'ls -la /app/ | head -12; echo "--- regex.txt ---"; cat /app/regex.txt 2>&1 | head -5'

echo
echo "=== done; full log at $LOG ==="
