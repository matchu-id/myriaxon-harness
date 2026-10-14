"""Self-scheduling must be pure Python and cross-platform, with no OS scheduler involved.

The requirement this encodes, in the owner's words: "put whole myriaxon sits on top of python so we are
not vendorlocked". An OS scheduler fails that on every count -- `schtasks` is Windows-only, `cron` and
`systemd` are POSIX-only, and each is a second moving part outside the harness that can be left behind
on a machine after the tool is gone. The first version of this feature used `schtasks` and made the
user's WSL window pop up every five minutes; the only reason anyone noticed was that Windows showed
them.

So these tests assert the PROPERTY rather than the behaviour: no process outside Python is spawned to
decide when a Locus runs, and the entry point that advances due work is importable and callable on any
platform.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import pytest  # noqa: E402

from myriaxon import loci  # noqa: E402


def test_there_is_no_os_scheduler_dependency():
    """The property, asserted directly: Myriaxon never SPAWNS an OS scheduler.

    A source-level check, because the failure mode is a subprocess invocation that no unit test of a
    Python function would ever see.

    Two false positives were removed while writing this, and both are worth recording because a noisy
    check gets deleted rather than fixed:

      * `systemctl` appears legitimately in `conditions.py` as an example of a WAKE-UP CONDITION a user
        may write. That is a feature, not a dependency.
      * a bare `"at"` matched the dictionary key used in about forty places for timestamps.

    So the pattern is anchored to the shape that actually matters: a scheduler binary named as the
    FIRST element of an argv list, which is how every `subprocess` call in this codebase is written.
    """
    import re

    invoked = re.compile(r"""[\[,(]\s*["'](schtasks|crontab|systemctl|launchctl)["']\s*[,\]]""")
    offenders = []
    for path in (REPO / "myriaxon").rglob("*.py"):
        for number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if invoked.search(stripped):
                offenders.append(f"{path.name}:{number}: {stripped[:80]}")
    assert offenders == [], f"Myriaxon spawns an OS scheduler: {offenders}"


def test_the_launcher_uses_a_python_interpreter():
    """Whatever it spawns, it spawns Python. That is what 'sits on top of Python' means."""
    argv = loci.launcher_command(["myriaxon", "loci", "--run"])
    assert argv[1:3] == ["-m", "myriaxon"], argv[:4]
    assert "python" in Path(argv[0]).name.lower(), argv[0]


def test_the_supervisor_interval_is_configurable_and_floored(monkeypatch):
    """A zero interval would spin a thread against the registry as fast as the CPU allows."""
    monkeypatch.setenv(loci.SUPERVISOR_ENV_INTERVAL, "30")
    assert loci.supervisor_interval() == 30.0
    monkeypatch.setenv(loci.SUPERVISOR_ENV_INTERVAL, "0")
    assert loci.supervisor_interval() == 5.0, "a zero interval must be floored, not obeyed"
    monkeypatch.setenv(loci.SUPERVISOR_ENV_INTERVAL, "-99")
    assert loci.supervisor_interval() == 5.0
    monkeypatch.setenv(loci.SUPERVISOR_ENV_INTERVAL, "not a number")
    assert loci.supervisor_interval() == loci.DEFAULT_SUPERVISOR_INTERVAL
    monkeypatch.delenv(loci.SUPERVISOR_ENV_INTERVAL, raising=False)
    assert loci.supervisor_interval() == loci.DEFAULT_SUPERVISOR_INTERVAL


def test_the_supervisor_can_be_disabled_by_environment(monkeypatch):
    """Anything that starts itself must be possible to switch off without editing code."""
    monkeypatch.delenv(loci.SUPERVISOR_ENV_DISABLE, raising=False)
    assert loci.supervisor_enabled() is True
    monkeypatch.setenv(loci.SUPERVISOR_ENV_DISABLE, "1")
    assert loci.supervisor_enabled() is False


def test_run_due_once_is_callable_and_safe_with_nothing_due(monkeypatch):
    """The shared entry point. With nothing due it must be free: no agent, no model call.

    This is the path both the dashboard thread and the headless supervisor use, so a regression here
    would either spin up an agent on every tick or fail to run anything -- and the second is the exact
    bug this whole change exists to fix.
    """
    fake = SimpleNamespace(due=lambda: [])
    monkeypatch.setattr(loci, "get_registry", lambda: fake)
    runs, failures = loci.run_due_once(console=SimpleNamespace(print=lambda *a, **k: None))
    assert (runs, failures) == (0, 0)
