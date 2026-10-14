"""The sandbox image is selectable, and the richer image really does add the tools.

The audit found the image hardcoded, which made a capable sandbox unreachable without editing source.
Measured on the default image, a confined agent has `pwsh` and coreutils and nothing else: no python,
no git, no node, no curl. So it can write files and run shell scripts, and cannot build anything.
"""
from __future__ import annotations

import os
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


def test_the_default_image_is_the_powershell_base():
    """Unset means the documented default, not an empty string -- an empty image name would make
    `docker run` fail with a confusing argument error."""
    os.environ.pop(tools.SANDBOX_IMAGE_ENV, None)
    assert tools.sandbox_image() == tools.SANDBOX_DOCKER_IMAGE
    assert tools.sandbox_image().strip()


def test_the_environment_selects_the_image(monkeypatch):
    monkeypatch.setenv(tools.SANDBOX_IMAGE_ENV, "myriaxon-sandbox:latest")
    assert tools.sandbox_image() == "myriaxon-sandbox:latest"


def test_a_blank_override_falls_back_rather_than_breaking(monkeypatch):
    """`MYRIAXON_SANDBOX_IMAGE=` in a shell profile must not produce `docker run ''`."""
    monkeypatch.setenv(tools.SANDBOX_IMAGE_ENV, "   ")
    assert tools.sandbox_image() == tools.SANDBOX_DOCKER_IMAGE


def test_the_argv_carries_the_selected_image(monkeypatch):
    monkeypatch.setenv(tools.SANDBOX_IMAGE_ENV, "custom-image:1")
    argv = tools._sandboxed_shell_argv("echo hi", "/tmp/ws")
    assert "custom-image:1" in argv
    # The isolation flags must survive any image choice -- they are the reason the sandbox exists.
    assert argv[argv.index("--network") + 1] == "none"
    assert "--rm" in argv


def test_the_image_is_read_per_call_not_captured_at_import(monkeypatch):
    """A process that changes the variable must not keep using the old image."""
    monkeypatch.setenv(tools.SANDBOX_IMAGE_ENV, "first:1")
    assert "first:1" in tools._sandboxed_shell_argv("x", "/tmp")
    monkeypatch.setenv(tools.SANDBOX_IMAGE_ENV, "second:2")
    assert "second:2" in tools._sandboxed_shell_argv("x", "/tmp")


@needs_docker
def test_the_built_image_provides_python_and_git(tmp_path):
    """The whole point of the richer image: a confined agent can RUN code, not just write it.

    Skipped rather than failed when `myriaxon-sandbox` has not been built -- the test reports that the
    capability was not exercised instead of turning a missing build into a broken suite.
    """
    image = "myriaxon-sandbox:latest"
    listing = subprocess.run(
        [*(tools.docker_prefix() or ["docker"]), "images", "-q", image],
        capture_output=True, text=True, timeout=120,
    )
    if not (listing.stdout or "").strip():
        pytest.skip(f"{image} is not built; run: docker build -t {image} docker/")

    for program in ("python3", "git", "node"):
        argv = tools._sandboxed_shell_argv(f"{program} --version", str(tmp_path))
        argv[argv.index(tools.sandbox_image())] = image
        completed = subprocess.run(argv, capture_output=True, text=True, timeout=300)
        assert completed.returncode == 0, f"{program} is missing from {image}: {completed.stderr[:200]}"


@needs_docker
def test_the_richer_image_still_cannot_reach_the_network(tmp_path):
    """Capability must not have cost isolation. This is the check that matters most about the change.

    Probes with a raw TCP connect rather than `curl`, and for a specific reason: curl ships IN this
    image, so a curl-based test would silently become a test of whether the network is up rather than
    whether the container can reach it, and it drags the shell's quoting rules into the assertion. A
    `.NET` socket attempt is PowerShell-native and unambiguous: it either connects or it does not.
    """
    image = "myriaxon-sandbox:latest"
    listing = subprocess.run(
        [*(tools.docker_prefix() or ["docker"]), "images", "-q", image],
        capture_output=True, text=True, timeout=120,
    )
    if not (listing.stdout or "").strip():
        pytest.skip(f"{image} is not built")

    # Dollar signs escaped, and that is not cosmetic: the argv goes through `wsl -- ...`, so an
    # unescaped `$c` is expanded by the intermediate shell to an empty string and PowerShell then sees
    # `.Close()` with no object -- a ParserError that reads like a broken container.
    probe = (
        "\\$sock = New-Object System.Net.Sockets.TcpClient; "
        "try { \\$sock.Connect('1.1.1.1', 443); Write-Output 'REACHABLE' } "
        "catch { Write-Output 'BLOCKED' } finally { \\$sock.Close() }"
    )
    argv = tools._sandboxed_shell_argv(probe, str(tmp_path))
    argv[argv.index(tools.sandbox_image())] = image
    completed = subprocess.run(argv, capture_output=True, text=True, timeout=300)
    assert "BLOCKED" in completed.stdout, (
        f"the sandbox reached the network -- isolation is gone for image {image}: "
        f"stdout={completed.stdout!r} stderr={completed.stderr[:200]!r}"
    )
