#!/usr/bin/env bash
# Probe the WSL Ubuntu environment before installing Docker Engine into it.
#
# Written as a script file rather than a `wsl.exe -- bash -lc "..."` one-liner because PowerShell
# interprets the inner quotes and `$` before WSL ever sees them: the first attempt had PowerShell
# trying to execute `/etc/os-release` and `uname` as cmdlets, and redirecting to `C:\dev\null`.
set -u

echo "  user          : $(whoami)"
echo "  distro        : $(. /etc/os-release 2>/dev/null; echo "${PRETTY_NAME:-unknown}")"
echo "  kernel        : $(uname -r)"
echo "  init (pid 1)  : $(ps -p 1 -o comm= 2>/dev/null || echo unknown)"
echo "  arch          : $(uname -m)"
echo "  docker now    : $(command -v docker || echo 'not installed')"
echo "  systemctl     : $(command -v systemctl || echo 'absent')"

echo
echo "  --- state of systemd ---"
if [ "$(ps -p 1 -o comm= 2>/dev/null)" = "systemd" ]; then
  echo "  systemd is pid 1 (good: the docker service can be enabled normally)"
else
  echo "  systemd is NOT pid 1 -- the daemon will need starting by hand or via /etc/wsl.conf"
  echo "  /etc/wsl.conf contents:"
  sed 's/^/    /' /etc/wsl.conf 2>/dev/null || echo "    (no /etc/wsl.conf)"
fi

echo
echo "  --- sudo without a password? ---"
if sudo -n true 2>/dev/null; then
  echo "  yes: non-interactive sudo works, so an install can run unattended"
else
  echo "  NO: sudo wants a password, which an automated install cannot supply"
fi

echo
echo "  --- network reachability (the install needs this) ---"
if command -v curl >/dev/null 2>&1; then
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 https://download.docker.com/linux/ubuntu/ 2>/dev/null || echo 000)
  echo "  download.docker.com -> HTTP ${code}"
else
  echo "  curl is not installed"
fi
