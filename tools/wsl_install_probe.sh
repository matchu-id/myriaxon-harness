#!/usr/bin/env bash
# Answer one question precisely: inside the real task image, does the Myriaxon wheel install and does
# the CLI run?
#
# This is the step Harbor's two failing runs never proved. Its `run()` has an install check that should
# raise "Myriaxon is not runnable in the container", and no trial raised -- yet the agent produced no
# output and `/app/regex.txt` was never written. So either the module imported and the turn silently
# did nothing, or the check passed without checking. Printing the exact pip output and the exact
# `--version` output separates those two.
set -u

IMAGE="alexgshaw/regex-log:20251031"
NAME="myr-probe"
REPO=/mnt/c/Users/arian/OneDrive/ドキュメント/Myriaxon
WHEEL=$(ls -1 "$REPO"/.uv/wheel/myriaxon-*.whl 2>/dev/null | head -1)

if [ -z "$WHEEL" ]; then echo "no wheel in .uv/wheel"; exit 2; fi
echo "  wheel: $(basename "$WHEEL")  ($(stat -c%s "$WHEEL") bytes)"

cleanup() { docker rm -f "$NAME" > /dev/null 2>&1 || true; }
trap cleanup EXIT
cleanup

docker run -d --name "$NAME" -w /app "$IMAGE" sleep 900 > /dev/null || { echo "start failed"; exit 2; }

echo
echo "=== A. does the image already have python3? ==="
docker exec "$NAME" sh -c 'python3 --version 2>&1 || echo "  none"'

echo
echo "=== B. the adapter's exact install sequence, verbatim ==="
docker exec "$NAME" sh -c 'apt-get update -qq && apt-get install -y -qq python3 python3-pip python3-venv git' > /tmp/apt.log 2>&1
echo "  apt exit=$? (log $(wc -l < /tmp/apt.log) lines)"
docker exec "$NAME" sh -c 'python3 -m venv /opt/myriaxon-venv'
echo "  venv exit=$?"

# The filename is preserved. `m.whl` failed with "not a valid wheel filename", and pip validates the
# name rather than the contents.
docker cp "$WHEEL" "$NAME:/tmp/$(basename "$WHEEL")" > /dev/null
echo "  --- pip install output, full ---"
docker exec "$NAME" /opt/myriaxon-venv/bin/pip install --no-cache-dir "/tmp/$(basename "$WHEEL")" 2>&1 | tail -12
echo "  pip exit=${PIPESTATUS[0]}"

echo
echo "=== C. the check Harbor's run() performs ==="
docker exec "$NAME" /opt/myriaxon-venv/bin/python -m myriaxon --version 2>&1 | tail -6
echo "  version exit=${PIPESTATUS[0]}"

echo
echo "=== D. is the package actually importable, and where from? ==="
docker exec "$NAME" /opt/myriaxon-venv/bin/python -c 'import myriaxon, sys; print("  module:", myriaxon.__file__); print("  python:", sys.version.split()[0])' 2>&1 | tail -4

echo
echo "=== E. installed distributions named myriaxon ==="
docker exec "$NAME" /opt/myriaxon-venv/bin/pip list 2>/dev/null | grep -i myriaxon || echo "  NONE -- the install did not register"

echo
echo "=== done ==="
