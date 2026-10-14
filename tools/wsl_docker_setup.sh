#!/usr/bin/env bash
# Install Docker Engine inside WSL Ubuntu, unattended, and prove it works.
#
# Docker Desktop is being removed, so this is the engine directly -- no Desktop, no docker-desktop
# distro. The user's WSL account needs to reach the daemon without sudo, which is what the docker
# group membership at the end is for.
#
# Every step logs what it is doing, because this runs detached: if it fails, the log is the only
# evidence, and "it did not work" without a line number is not a diagnosis.
set -u

LOG=/tmp/myriaxon-docker-install.log
exec > >(tee -a "$LOG") 2>&1

echo "=== Docker Engine install started $(date -Is) ==="
echo "user: $(whoami)   distro: $(. /etc/os-release 2>/dev/null; echo "${PRETTY_NAME:-unknown}")"

step() { echo; echo "--- $* ---"; }

step "1. confirm we can use sudo without a password"
if ! sudo -n true 2>/dev/null; then
  echo "FAILED: sudo requires a password. An unattended install cannot supply one."
  echo "Run this script yourself in the Ubuntu shell:"
  echo "  sudo bash /mnt/c/Users/arian/OneDrive/ドキュメント/Myriaxon/tools/wsl_docker_setup.sh"
  exit 2
fi
echo "ok: non-interactive sudo works"

step "2. remove any conflicting distribution packages"
sudo apt-get remove -y -qq docker docker-engine docker.io containerd runc 2>/dev/null || true
echo "done (nothing to remove is also fine)"

step "3. apt prerequisites"
sudo apt-get update -qq
sudo apt-get install -y -qq ca-certificates curl gnupg lsb-release
echo "done"

step "4. add Docker's official GPG key and repository"
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
  | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg --yes
sudo chmod a+r /etc/apt/keyrings/docker.gpg
. /etc/os-release
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
echo "repository: ${VERSION_CODENAME} stable"

step "5. install docker engine, cli and containerd"
sudo apt-get update -qq
sudo apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
echo "installed: $(docker --version 2>/dev/null || echo 'docker not on PATH yet')"

step "6. start the daemon"
# systemd may or may not be pid 1 in this distro. Both routes are attempted rather than assuming,
# because the failure mode of guessing wrong is a silently dead daemon.
if [ "$(ps -p 1 -o comm= 2>/dev/null)" = "systemd" ]; then
  sudo systemctl enable --now docker && echo "started via systemctl"
else
  echo "systemd is not pid 1; starting dockerd directly"
  if ! pgrep -x dockerd > /dev/null; then
    sudo nohup dockerd > /tmp/dockerd.log 2>&1 &
    sleep 12
  fi
fi

step "7. let this user use docker without sudo"
sudo usermod -aG docker "$(whoami)" || true
echo "added $(whoami) to the docker group (takes effect in a NEW shell)"

step "8. verify the daemon actually answers"
for i in $(seq 1 20); do
  if sudo docker info > /dev/null 2>&1; then
    echo "daemon is up after ${i} attempt(s)"
    break
  fi
  sleep 3
done
sudo docker info --format '  server version : {{.ServerVersion}}' 2>/dev/null || echo "  daemon did NOT come up"

step "9. run a real Linux container (the only proof that matters)"
sudo docker run --rm hello-world 2>&1 | head -20

step "10. expose the daemon to Windows"
echo "  the WSL socket lives at /var/run/docker.sock"
echo "  Windows reaches it via: npipe:////./pipe/docker_engine only with Docker Desktop,"
echo "  or by pointing DOCKER_HOST at a TCP endpoint. Current socket state:"
ls -l /var/run/docker.sock 2>/dev/null || echo "  /var/run/docker.sock absent"

echo
echo "=== finished $(date -Is) ==="
