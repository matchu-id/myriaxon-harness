#!/usr/bin/env bash
# Install the Harbor benchmark framework inside WSL, where Docker is native.
#
# Background, because the location is deliberate: Harbor was installed on Windows first, and driving
# the WSL daemon from there did not work. Loopback forwarding gave HTTP 200 and connection-refused
# alternately with nothing changed between attempts, and the WSL interface address timed out because
# the Hyper-V firewall drops inbound traffic and needs admin to change. Meanwhile Harbor shells out to
# the `docker` CLI and hard-requires a working daemon, so "the daemon is up" is not the same as
# "Harbor can use it".
#
# Inside WSL none of that applies: /var/run/docker.sock is local, no network, no firewall, no admin.
# The Windows-side Harbor install is left alone rather than deleted -- it still works for the Daytona
# backend, which needs no Docker at all.
set -u

REPO=/mnt/c/Users/arian/OneDrive/ドキュメント/Myriaxon
VENV="$REPO/.venv-harbor-linux"
LOG=/tmp/myriaxon-harbor-setup.log
exec > >(tee -a "$LOG") 2>&1

echo "=== Harbor setup inside WSL, $(date -Is) ==="

step() { echo; echo "--- $* ---"; }

step "1. python version (Harbor needs >= 3.12)"
python3 --version
PYV=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
echo "  detected: $PYV"
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3,12) else 1)'; then
  echo "FAILED: python3 is older than 3.12; Harbor will not install"
  exit 2
fi

step "2. system packages"
if sudo -n true 2>/dev/null; then
  sudo apt-get update -qq
  sudo apt-get install -y -qq python3-venv python3-pip curl
  echo "installed"
else
  echo "  no passwordless sudo; assuming python3-venv is already present"
fi

step "3. create the virtualenv at $VENV"
if [ -x "$VENV/bin/python" ]; then
  echo "  already exists"
else
  python3 -m venv "$VENV" || { echo "FAILED: venv creation failed"; exit 2; }
  echo "  created"
fi

step "4. install harbor"
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet harbor
"$VENV/bin/python" -c "import harbor; print('  harbor', harbor.__version__)"

step "5. confirm the docker CLI is reachable and the daemon answers"
if ! command -v docker >/dev/null 2>&1; then
  echo "  docker CLI not on PATH; checking the system location"
fi
docker version --format '  client {{.Client.Version}} | server {{.Server.Version}}' 2>&1 | sed 's/^/  /'
docker run --rm hello-world 2>&1 | grep -m1 'Hello from Docker' | sed 's/^/  /' || echo "  container run FAILED"

step "6. does harbor see its own CLI?"
"$VENV/bin/harbor" --version 2>&1 | head -3 | sed 's/^/  /'

echo
echo "=== finished $(date -Is) ==="
