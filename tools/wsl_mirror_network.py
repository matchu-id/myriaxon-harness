"""Make the WSL Docker daemon reachable from Windows.

Two facts collide here, both measured rather than assumed:

  * inside WSL the daemon listens on 127.0.0.1:2375 and answers `/_ping` with HTTP 200;
  * from Windows that same address gives `WinError 10061, connection refused`.

That is not a misconfiguration. WSL2 runs its distro behind a NAT'd virtual network, so the Linux
`127.0.0.1` and the Windows `127.0.0.1` are different interfaces. Windows has to be told to share its
own, which is what `networkingMode=mirrored` in `.wslconfig` does -- it puts the distro on the host's
network stack, so loopback is genuinely shared and no port forwarding is needed.

The alternative, `netsh interface portproxy`, needs admin and adds a proxy to keep alive. Mirrored
mode needs a `wsl --shutdown` instead, which is cheaper and is the documented mechanism.

`wsl --shutdown` restarts every distro, so this refuses to do it silently: it writes the config,
reports exactly what will be interrupted, and leaves the restart to the caller's decision.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

CONFIG = Path(os.environ.get("USERPROFILE", "")) / ".wslconfig"
WANTED = "networkingMode=mirrored"


def read_config() -> str:
    try:
        return CONFIG.read_text(encoding="utf-8") if CONFIG.is_file() else ""
    except Exception:  # noqa: BLE001
        return ""


def main() -> int:
    print(f"  config file : {CONFIG}")
    existing = read_config()
    print(f"  exists      : {CONFIG.is_file()}")
    if existing.strip():
        print("  current contents:")
        for line in existing.splitlines():
            print(f"    {line}")

    if WANTED in existing.replace(" ", "").lower():
        print("\n  mirrored networking is already configured")
    else:
        # A [wsl2] section is what the key belongs to. Appending preserves whatever was already
        # there: this file is shared with every other WSL setting the user has, and rewriting it
        # wholesale would silently drop them.
        block = ""
        if "[wsl2]" not in existing:
            block += "\n[wsl2]\n"
        if existing and not existing.endswith("\n"):
            block += "\n"
        block += f"{WANTED}\n"
        try:
            with CONFIG.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(block)
        except Exception as exc:  # noqa: BLE001
            print(f"\n  could not write {CONFIG}: {type(exc).__name__}: {exc}")
            return 1
        print(f"\n  appended: {WANTED}")

    print("\n  --- what happens next ---")
    print("  `wsl --shutdown` is required for this to take effect, and it stops EVERY distro:")
    result = subprocess.run(["wsl.exe", "-l", "-q"], capture_output=True, text=True, timeout=60)
    distros = [d.strip() for d in result.stdout.replace("\x00", "").splitlines() if d.strip()]
    for name in distros:
        print(f"    - {name} will stop (any running process in it is killed)")
    print()
    print("  Nothing in this repo is running inside WSL, so nothing of ours is lost.")
    print("  After the shutdown the Docker daemon restarts with the distro (docker.service is enabled).")
    print()
    print("  Run it with:   wsl --shutdown")
    print("  Then verify:   curl http://127.0.0.1:2375/_ping   (expect 'OK')")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
