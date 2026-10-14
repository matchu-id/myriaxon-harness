#!/usr/bin/env bash
# Bind the WSL Docker daemon to the WSL interface, with a firewall rule so only Windows can reach it.
#
# Why not loopback-only: measured repeatedly, WSL2's automatic localhost forwarding to Windows is
# intermittent -- `http://127.0.0.1:2375/_ping` returned HTTP 200, then connection-refused, then 200
# again with nothing changed in between. A benchmark harness cannot be built on that.
#
# Why not simply 0.0.0.0: an unauthenticated Docker API is root-equivalent. Binding it to every
# interface would hand root on this machine to anything that can route to 172.26.x.x, which on a
# laptop means other VMs and anything bridged.
#
# So: bind the WSL NIC, then use iptables to ACCEPT only the Windows host (the default gateway of
# this NAT'd network) and DROP everything else on that port. Windows reaches it at the eth0 address;
# nothing else can.
set -u

PORT="${1:-2375}"
DROPIN=/etc/systemd/system/docker.service.d/override.conf
LOG=/tmp/myriaxon-docker-bind.log
exec > >(tee -a "$LOG") 2>&1

echo "=== binding docker to the WSL interface on port ${PORT}, Windows-only ==="

if ! sudo -n true 2>/dev/null; then
  echo "FAILED: sudo needs a password."
  exit 2
fi

GATEWAY=$(ip route | awk '/^default/ {print $3; exit}')
WSL_IP=$(ip -4 addr show eth0 | awk '/inet / {print $2}' | cut -d/ -f1)
echo "  wsl address : ${WSL_IP}"
echo "  gateway     : ${GATEWAY}   (this is the Windows host)"
if [ -z "$GATEWAY" ] || [ -z "$WSL_IP" ]; then
  echo "FAILED: could not determine the addresses"
  exit 2
fi

echo "--- 1. systemd drop-in ---"
sudo mkdir -p /etc/systemd/system/docker.service.d
sudo tee "$DROPIN" > /dev/null <<EOF
[Service]
ExecStart=
ExecStart=/usr/bin/dockerd -H fd:// -H tcp://0.0.0.0:${PORT}
EOF
echo "written: $DROPIN"

echo "--- 2. firewall: allow only the Windows host, drop the rest ---"
# Docker inserts its own rules into the DOCKER-USER chain on startup, so the rule is placed there
# rather than in INPUT -- a rule in INPUT would be evaluated after Docker's own and never see the
# traffic. Both directions of the connection are permitted because the reply must get back out.
sudo iptables -F DOCKER-USER 2>/dev/null || true
sudo iptables -A DOCKER-USER -i eth0 -s "$GATEWAY" -p tcp --dport "$PORT" -j RETURN
sudo iptables -A DOCKER-USER -i eth0 -p tcp --dport "$PORT" -j DROP
echo "  allowed: ${GATEWAY}    dropped: everyone else on port ${PORT}"

echo "--- 3. restart the daemon ---"
sudo systemctl daemon-reload
sudo systemctl restart docker
sleep 6

echo "--- 4. daemon state ---"
sudo docker info --format '  server version : {{.ServerVersion}}' 2>/dev/null || echo "  daemon did NOT come up"
ss -ltn 2>/dev/null | grep ":${PORT}" | sed 's/^/  /' || echo "  port ${PORT} not listening"

echo "--- 5. reachable locally? ---"
code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "http://127.0.0.1:${PORT}/_ping" 2>/dev/null || echo 000)
echo "  GET /_ping -> HTTP ${code}"

echo
echo "From Windows use:"
echo "  \$env:DOCKER_HOST = 'tcp://${WSL_IP}:${PORT}'"
echo
echo "=== finished ==="
