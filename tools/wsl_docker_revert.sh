#!/usr/bin/env bash
# Put the WSL Docker daemon back on its Unix socket only.
#
# The TCP listener existed to let Windows reach the daemon. Measured, that never worked: binding to
# loopback gave intermittent HTTP 200 / connection-refused, and binding the WSL interface gave
# `i/o timeout` from Windows because the Hyper-V firewall drops inbound traffic and fixing that needs
# admin. Meanwhile an unauthenticated Docker API on a network interface is root-equivalent.
#
# So the exposure is removed rather than left in place "in case it helps". Harbor will run inside WSL
# instead, where the Unix socket is native, local, and needs no network at all.
set -u

DROPIN=/etc/systemd/system/docker.service.d/override.conf

echo "=== reverting docker to the Unix socket ==="
if ! sudo -n true 2>/dev/null; then
  echo "FAILED: sudo needs a password."
  exit 2
fi

echo "--- 1. remove the TCP drop-in ---"
if [ -f "$DROPIN" ]; then
  sudo rm -f "$DROPIN"
  echo "removed: $DROPIN"
else
  echo "nothing to remove"
fi

echo "--- 2. remove the firewall rules it added ---"
sudo iptables -F DOCKER-USER 2>/dev/null || true
echo "DOCKER-USER chain flushed"

echo "--- 3. restart ---"
sudo systemctl daemon-reload
sudo systemctl restart docker
sleep 5

echo "--- 4. verify: socket only, no TCP listener ---"
sudo docker info --format '  server version : {{.ServerVersion}}' 2>/dev/null || echo "  daemon did NOT come up"
if ss -ltn 2>/dev/null | grep -q ":2375"; then
  echo "  WARNING: port 2375 is STILL listening -- the revert did not take"
  ss -ltn | grep ":2375" | sed 's/^/    /'
else
  echo "  port 2375 is closed (good)"
fi
ls -l /var/run/docker.sock 2>/dev/null | sed 's/^/  /' || echo "  socket missing"

echo "--- 5. still works locally? ---"
sudo docker run --rm hello-world 2>&1 | grep -m1 'Hello from Docker' || echo "  container run FAILED"

echo
echo "=== finished ==="
