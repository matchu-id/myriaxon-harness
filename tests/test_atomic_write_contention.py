"""The atomic write must survive Windows file contention, because losing a write is silent.

Found by chasing an intermittent failure rather than by reasoning: one full-suite run failed with
`Permission denied: ... loci.json`, then passed on every re-run and six repeats of the same file.

Why that matters more than it looks: `LocusRegistry.save` catches OSError and only logs a warning, so
a rename that fails means the change is DISCARDED and the caller never learns. A goal marked done, an
autonomy mode switched, a failure ledger reset -- all of them can be lost this way, and the user's own
`cowork` runs were writing this file concurrently when it happened.

The contention is reproduced here by holding the target open with a real exclusive handle, which is
what another process's open does on Windows. On POSIX the same test exercises the retry path
harmlessly (the rename succeeds immediately), so it is not a Windows-only test that never runs.
"""
from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import pytest  # noqa: E402

from myriaxon.atomic_io import _REPLACE_ATTEMPTS, atomic_write_text  # noqa: E402


def test_a_plain_write_still_works(tmp_path):
    target = tmp_path / "store.json"
    atomic_write_text(target, '{"a": 1}')
    assert target.read_text(encoding="utf-8") == '{"a": 1}'


def test_a_write_survives_the_target_being_locked_briefly(tmp_path):
    """The real scenario: another process has it open for a few milliseconds."""
    target = tmp_path / "store.json"
    atomic_write_text(target, "first")

    hold_seconds = 0.05
    release = threading.Event()

    def holder():
        with open(target, "rb"):
            time.sleep(hold_seconds)
        release.set()

    thread = threading.Thread(target=holder, daemon=True)
    thread.start()
    time.sleep(0.01)  # let the handle actually open before the write tries to replace

    atomic_write_text(target, "second")
    thread.join(timeout=5)
    # The write must land, not be dropped. This is the assertion the intermittent failure was about.
    assert target.read_text(encoding="utf-8") == "second"


def test_repeated_writes_under_contention_all_land(tmp_path):
    """Many writes while a reader keeps re-opening: every one must succeed and the last must win."""
    target = tmp_path / "store.json"
    stop = threading.Event()

    def reader():
        while not stop.is_set():
            try:
                with open(target, "rb"):
                    pass
            except OSError:
                pass

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    try:
        for index in range(40):
            atomic_write_text(target, f"value-{index}")
    finally:
        stop.set()
        thread.join(timeout=5)
    assert target.read_text(encoding="utf-8") == "value-39"


def test_no_temp_files_are_left_behind(tmp_path):
    """A failed or retried write must not litter the directory it writes into."""
    target = tmp_path / "store.json"
    for index in range(5):
        atomic_write_text(target, f"v{index}")
    leftovers = [p.name for p in tmp_path.iterdir() if p.name != "store.json"]
    assert leftovers == [], f"temp files were left behind: {leftovers}"


def test_a_permanent_failure_still_raises(tmp_path, monkeypatch):
    """The retry must not swallow a real error: only contention is transient."""
    target = tmp_path / "store.json"
    calls = {"n": 0}

    def always_denied(src, dst):
        calls["n"] += 1
        raise PermissionError("denied forever")

    monkeypatch.setattr(os, "replace", always_denied)
    with pytest.raises(PermissionError):
        atomic_write_text(target, "x")
    # It must actually have retried rather than giving up on the first attempt.
    assert calls["n"] == _REPLACE_ATTEMPTS
    assert not target.exists()


def test_a_non_permission_error_is_not_retried(tmp_path, monkeypatch):
    """Retrying a genuine error would turn a fast failure into a slow one."""
    target = tmp_path / "store.json"
    calls = {"n": 0}

    def is_a_directory(src, dst):
        calls["n"] += 1
        raise IsADirectoryError("nope")

    monkeypatch.setattr(os, "replace", is_a_directory)
    with pytest.raises(IsADirectoryError):
        atomic_write_text(target, "x")
    assert calls["n"] == 1
