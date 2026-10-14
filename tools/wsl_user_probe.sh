#!/usr/bin/env bash
# Run the whole adapter sequence as a NON-ROOT user, which is what Harbor does.
#
# Every successful test so far ran `docker exec` as root. Harbor's `exec_as_agent` does not use root --
# it uses the environment's agent user. That is the one variable never exercised, and it explains the
# exact symptom: nothing raises, yet `/app` has no file, because a non-root user cannot write to a
# root-owned directory.
#
# The image's own home directory is used rather than a guessed username, so this fails loudly if the
# image has no non-root user at all.
set -u

IMAGE="alexgshaw/regex-log:20251031"
NAME="myr-user"
REPO=/mnt/c/Users/arian/OneDrive/ドキュメント/Myriaxon
WHEEL=$(ls -1 "$REPO"/.uv/wheel/myriaxon-*.whl 2>/dev/null | head -1)

cleanup() { docker rm -f "$NAME" > /dev/null 2>&1 || true; }
trap cleanup EXIT
cleanup

docker run -d --name "$NAME" -w /app "$IMAGE" sleep 900 > /dev/null || { echo "start failed"; exit 2; }

echo "=== 1. who is in this image? ==="
docker exec "$NAME" sh -c 'cat /etc/passwd | grep -E "^(root|agent|ubuntu|user|app)" || echo "  only root"'
echo "  --- /app ownership ---"
docker exec "$NAME" sh -c 'ls -ld /app; stat -c "%U:%G %a" /app'

echo
echo "=== 2. install as root (the adapter does this with exec_as_root) ==="
docker exec "$NAME" sh -c 'apt-get update -qq && apt-get install -y -qq python3 python3-venv python3-pip' > /tmp/i.log 2>&1
docker exec "$NAME" sh -c 'python3 -m venv /opt/myriaxon-venv' 2>&1 | tail -2
docker cp "$WHEEL" "$NAME:/tmp/$(basename "$WHEEL")" > /dev/null
docker exec "$NAME" /opt/myriaxon-venv/bin/pip install -q --no-cache-dir "/tmp/$(basename "$WHEEL")" > /tmp/p.log 2>&1
echo "  installed: $(docker exec "$NAME" /opt/myriaxon-venv/bin/python -m myriaxon --version 2>&1)"

echo
echo "=== 3. pick the agent user Harbor would use ==="
AGENT_USER=$(docker exec "$NAME" sh -c 'for u in agent ubuntu user app; do id "$u" >/dev/null 2>&1 && { echo "$u"; exit 0; }; done; echo root')
echo "  agent user: $AGENT_USER"

echo
echo "=== 4. config + prompt, as the adapter uploads them ==="
CONFIG_SRC="${1:-}"
if [ -z "$CONFIG_SRC" ] || [ ! -f "$CONFIG_SRC" ]; then echo "  FAILED: pass a config file"; exit 2; fi
docker exec "$NAME" sh -c 'mkdir -p /tmp/myriaxon-config && chmod 755 /tmp/myriaxon-config'
docker cp "$CONFIG_SRC" "$NAME:/tmp/myriaxon-config/config.json" > /dev/null
docker exec "$NAME" sh -c 'chmod 644 /tmp/myriaxon-config/config.json'
printf 'Create a file named userprobe.txt in the current directory containing exactly the word PROBE.\n' > /tmp/up.txt
docker cp /tmp/up.txt "$NAME:/tmp/myriaxon-prompt.txt" > /dev/null
docker exec "$NAME" sh -c 'chmod 644 /tmp/myriaxon-prompt.txt'
echo "  staged"

echo
echo "=== 5. THE DECISIVE TEST: write to /app as $AGENT_USER ==="
docker exec -u "$AGENT_USER" "$NAME" sh -c 'touch /app/.writetest 2>&1 && echo "  WRITE OK -- /app is writable" || echo "  WRITE DENIED -- this is the bug"' || true

echo
echo "=== 5b. apply the adapter's fix: chown /app to the agent user (as root) ==="
docker exec "$NAME" sh -c "chown -R $AGENT_USER /app && echo '  chown done'"
docker exec "$NAME" sh -c "chown -R $AGENT_USER /tmp/myriaxon-config && echo '  config chown done'"
docker exec -u "$AGENT_USER" "$NAME" sh -c 'touch /app/.writetest2 2>&1 && echo "  WRITE OK after chown -- the fix works" || echo "  STILL DENIED -- chown was not enough"'
docker exec "$NAME" sh -c 'rm -f /app/.writetest /app/.writetest2'

echo
echo "=== 6. the turn, as $AGENT_USER ==="
docker exec -u "$AGENT_USER" "$NAME" sh -c 'cd /app && MYRIAXON_CONFIG_DIR=/tmp/myriaxon-config /opt/myriaxon-venv/bin/python -m myriaxon --mode auto -p "$(cat /tmp/myriaxon-prompt.txt)"' 2>&1 | tail -25

echo
echo "=== 7. what ended up in /app? ==="
docker exec "$NAME" sh -c 'ls -la /app/'
docker exec "$NAME" sh -c 'cat /app/userprobe.txt 2>&1 | head -3'

echo
echo "=== done ==="
