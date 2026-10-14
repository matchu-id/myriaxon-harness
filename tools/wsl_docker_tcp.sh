#!/usr/bin/env bash
# Expose the WSL Docker daemon to Windows over TCP, so the Windows Docker CLI and Harbor can reach it.
#
# Why this is needed: inside WSL the daemon listens on a Unix socket (/var/run/docker.sock). The
# Windows Docker CLI speaks to a Windows named pipe, and those are different transports. A TCP
# listener on the WSL side is the standard bridge -- Harbor reads DOCKER_HOST, so pointing it at
# this endpoint is all the integration that is required.
#
# SECURITY, stated because this is a real exposure and not a formality:
# the listener is bound to 127.0.0.1 only, so nothing outside this machine can reach it. It is still
# an unauthenticated root-equivalent endpoint for anything running as this user, which is the same
# trust level the Unix socket already grants. The script does NOT bind 0.0.0.0 and does not enable a
# public TLS port; doing either would hand root on this machine to the local network.
set -u

PORT="${1:-2375}"
DROPIN=/etc/systemd/system/docker.service.d/override.conf
LOG=/tmp/myriaxon-docker-tcp.log
exec > >(tee -a "$LOG") 2>&1

echo "=== exposing the docker daemon on 127.0.0.1:${PORT} ==="

if ! sudo -n true 2>/dev/null; then
  echo "FAILED: sudo needs a password."
  exit 2
fi

echo "--- 1. write the systemd drop-in ---"
sudo mkdir -p /etc/systemd/system/docker.service.d
sudo tee "$DROPIN" > /dev/null <<EOF
[Service]
ExecStart=
ExecStart=/usr/bin/dockerd -H fd:// -H tcp://127.0.0.1:${PORT}
EOF
echo "written: $DROPIN"

echo "--- 2. reload and restart ---"
sudo systemctl daemon-reload
sudo systemctl restart docker
sleep 6

echo "--- 3. is the daemon up? ---"
sudo docker info --format '  server version : {{.ServerVersion}}' 2>/dev/null || echo "  daemon did NOT come up"

echo "--- 4. is the TCP port listening on loopback only? ---"
if ss -ltn 2>/dev/null | grep -q "127.0.0.1:${PORT}"; then
  echo "  listening on 127.0.0.1:${PORT}  (loopback only -- good)"
  ss -ltn | grep ":${PORT}" | sed 's/^/    /'
elif ss -ltn 2>/dev/null | grep -q ":${PORT}"; then
  echo "  WARNING: listening on something other than loopback:"
  ss -ltn | grep ":${PORT}" | sed 's/^/    /'
  echo "  this may expose root-equivalent docker control to your network"
else
  echo "  port ${PORT} is NOT listening -- the drop-in did not take effect"
fi

echo "--- 5. does the API answer over TCP? ---"
code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "http://127.0.0.1:${PORT}/_ping" 2>/dev/null || echo 000)
echo "  GET /_ping -> HTTP ${code}"

echo
echo "Windows should now use:"
echo "  \$env:DOCKER_HOST = 'tcp://127.0.0.1:${PORT}'"
echo
echo "=== finished ==="
