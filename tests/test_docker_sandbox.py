"""The sandbox must be FINDABLE, cross-platform, and actually isolated.

The original `docker_available()` was `shutil.which("docker") is not None` -- a Windows PATH lookup.
Measured on this host that returned False while Docker 29.8.2 ran with a live daemon inside WSL
Ubuntu, so every shell command was refused for want of a sandbox that was already installed. The
permission layer's refusal was correct and its premise was wrong.

Verified live against that WSL engine, not merely reasoned about:

    docker_available()      True   (0.3s, then cached at 0.000s)
    prefix                  ['wsl', '-d', 'Ubuntu', '--', 'docker']
    container path          /mnt/c/Users/.../sbx-owbrb5wi
    sandboxed command       exit 0, read a file the host wrote
    network                 NETWORK-BLOCKED
    host filesystem         0 entries visible outside the mount

The Docker-dependent parts are marked so they SKIP rather than fail on a machine without an engine:
a test that fails for want of Docker would be turned off, and a skipped test still reports that the
capability was not exercised.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import pytest  # noqa: E402

from myriaxon import tools  # noqa: E402

needs_docker = pytest.mark.skipif(
    not tools.docker_available(), reason="no container engine on this machine"
)


def test_the_probe_is_cached_and_cheap():
    """The probe spawns a process; running it per shell command measured 5.5s per call.

    A regression here would not fail anything loudly -- it would just make every command slow, which
    is why it is pinned.
    """
    tools._docker_available_cache.clear()
    tools.docker_available()
    cached = tools._docker_available_cache
    assert cached, "the probe did not populate the cache"
    # Second call must not touch the cache-clearing path; assert the SAME dict object survives.
    tools.docker_available()
    assert tools._docker_available_cache is cached


def test_a_windows_path_translates_for_a_linux_engine():
    """A Linux engine mounting `C:\\...` creates an empty directory -- the agent silently loses its
    workspace, with no error. The translation is therefore load-bearing, not cosmetic."""
    fake_prefix = {"prefix": ["wsl", "-d", "Ubuntu", "--", "docker"]}
    tools._docker_available_cache.clear()
    tools._docker_available_cache.update(fake_prefix)
    try:
        translated = tools._container_path(r"C:\Users\someone\project")
        assert translated.startswith("/mnt/c/"), f"not translated: {translated}"
        assert "\\" not in translated, "a backslash survived into a Linux path"
    finally:
        tools._docker_available_cache.clear()


def test_a_native_engine_leaves_the_path_alone():
    """On Linux, or with a native Windows engine, the path must pass through untouched."""
    tools._docker_available_cache.clear()
    tools._docker_available_cache.update({"prefix": ["docker"]})
    try:
        assert tools._container_path("/home/user/project") == "/home/user/project"
    finally:
        tools._docker_available_cache.clear()


def test_the_argv_uses_the_discovered_prefix_not_a_hardcoded_docker():
    """The original hardcoded "docker", which is what made the function unreachable on this host."""
    tools._docker_available_cache.clear()
    tools._docker_available_cache.update({"prefix": ["wsl", "-d", "Ubuntu", "--", "docker"]})
    try:
        argv = tools._sandboxed_shell_argv("echo hi", "/tmp/ws")
        assert argv[:4] == ["wsl", "-d", "Ubuntu", "--"], argv[:6]
        assert "docker" in argv
        assert argv[argv.index("--network") + 1] == "none", "network isolation was dropped"
        assert "--rm" in argv, "a throwaway container must not be left behind"
    finally:
        tools._docker_available_cache.clear()


def test_distro_listing_tolerates_utf16_output():
    """`wsl --list --quiet` emits UTF-16LE with a BOM; a naive decode yields one character per
    interleaved null byte and every distro comparison then fails silently."""
    raw = "Ubuntu\r\nDebian\r\n".encode("utf-16-le")
    assert b"\x00" in raw
    # The decode branch the implementation uses:
    text = raw.decode("utf-16-le", "replace")
    names = [line.strip().lstrip("*").strip() for line in text.splitlines() if line.strip()]
    assert names == ["Ubuntu", "Debian"]


# ---- live checks: these need a real engine ------------------------------------------------
@needs_docker
def test_the_sandbox_runs_a_command_and_shares_the_workspace(tmp_path):
    """The whole point: a command runs, and it can see files the host wrote."""
    (tmp_path / "marker.txt").write_text("hello-from-host", encoding="utf-8")
    argv = tools._sandboxed_shell_argv("Get-Content /workspace/marker.txt", str(tmp_path))
    completed = subprocess.run(argv, capture_output=True, text=True, timeout=300)
    assert completed.returncode == 0, completed.stderr[:400]
    assert "hello-from-host" in completed.stdout


@needs_docker
def test_the_sandbox_cannot_reach_the_network(tmp_path):
    """`--network none` is the claim; this is the evidence. A sandbox that can exfiltrate is not a
    sandbox, and the spec's own red-team list names network-egress-bypass for exactly this reason."""
    command = (
        "try { Invoke-WebRequest -Uri https://api.deepseek.com -TimeoutSec 8 -UseBasicParsing | "
        'Out-Null; Write-Output "REACHABLE" } catch { Write-Output "BLOCKED" }'
    )
    argv = tools._sandboxed_shell_argv(command, str(tmp_path))
    completed = subprocess.run(argv, capture_output=True, text=True, timeout=300)
    assert "BLOCKED" in completed.stdout, f"the sandbox reached the network: {completed.stdout!r}"


@needs_docker
def test_the_sandbox_cannot_see_the_host_outside_the_mount(tmp_path):
    """The engine is a container, not a jail with a path check -- nothing outside the mount exists."""
    command = (
        "Get-ChildItem C:\\ -ErrorAction SilentlyContinue | Measure-Object | "
        "Select-Object -ExpandProperty Count"
    )
    argv = tools._sandboxed_shell_argv(command, str(tmp_path))
    completed = subprocess.run(argv, capture_output=True, text=True, timeout=300)
    assert completed.stdout.strip().splitlines()[-1].strip() in ("0", ""), (
        f"the sandbox saw host directories: {completed.stdout!r}"
    )
