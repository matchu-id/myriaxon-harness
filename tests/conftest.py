"""Session-wide test setup.

One job, and it must happen before anything imports the package: **point Myriaxon's state at a
scratch directory.**

`constants.CONFIG_DIR` is resolved at import time, from `MYRIAXON_CONFIG_DIR`. So whichever test
module happens to import the package first decides where *every* test writes -- and several of them
write real state: the dots tests create agents, the panel tests drive a real server that creates
sessions and uploads, the installer tests write a redirect into site-packages.

Measured, twice: without this, a full run left eight `Dot-N` agents in the developer's own
`~/.myriaxon/dots.json`, and a later run then failed with "a dot named Dot-6 already exists" --
a test-isolation bug that presents as a product bug, and which quietly edits the user's real
configuration while doing it.

Setting it here rather than in each module also removes the ordering dependency: `conftest.py` is
imported by pytest before it collects anything, so there is no module that can get there first.
"""

from __future__ import annotations

import atexit
import os
import shutil
import tempfile

_SCRATCH = tempfile.mkdtemp(prefix="myriaxon-tests-")

# Set, not defaulted: a developer who happens to have MYRIAXON_CONFIG_DIR exported is exactly the
# person whose real configuration these tests would otherwise edit.
os.environ["MYRIAXON_CONFIG_DIR"] = _SCRATCH

# A key is required for the config to resolve at all; a placeholder keeps the real one untouched and
# makes it obvious in a traceback that a test leaked a request.
os.environ.setdefault("MYRIAXON_API_KEY", "sk-ant-test-placeholder")
os.environ.setdefault("MYRIAXON_PROVIDER", "anthropic")


@atexit.register
def _cleanup() -> None:
    shutil.rmtree(_SCRATCH, ignore_errors=True)


# --- working directory guard ---------------------------------------------------------------
# `serve()` calls os.chdir() into the workspace it is given, deliberately: the agent's tools resolve
# relative paths against the process working directory. That is correct for the product, and for a
# test suite it is a trap -- a test that starts a dashboard leaves the whole process sitting in a
# temporary directory, and every later test that touches a relative path then fails for a reason
# that has nothing to do with what it is checking.
#
# Measured: five tests failed in a full run and all of them passed when their own file was run
# alone. Restoring the directory around every test removes the whole class of failure, rather than
# requiring each test that starts a server to remember.
import os as _os  # noqa: E402

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _restore_working_directory():
    original = _os.getcwd()
    try:
        yield
    finally:
        try:
            _os.chdir(original)
        except OSError:
            # The original directory was removed underneath us; nothing useful to restore to, and
            # failing teardown here would hide the real test result.
            pass


# --- registry guard ------------------------------------------------------------------------
# `loci.get_registry()` caches a module-level singleton. A test that points it at a scratch file
# leaves that cached path behind, so the *next* test file gets a registry from somewhere else --
# and because the dashboard reads the always-on agents through the same accessor, the panel tests then
# fail with "no locus named ..." for agents the API had just created. It presented as flaky: the same
# suite produced a different failure set on each run.
#
# Pointing the accessor at a fresh registry for every test removes the ordering dependency
# entirely, rather than requiring each test to remember to clean up.
#
# (This guard used to patch `myriaxon.dots`. After the rename to `loci` it imported a module that no
# longer existed, so every test that used a fixture errored at setup.)
@pytest.fixture(autouse=True)
def _fresh_locus_registry():
    from myriaxon import loci

    original = loci.get_registry
    loci.get_registry = lambda: loci.LocusRegistry()
    try:
        yield
    finally:
        loci.get_registry = original


# --- repairing a closed stdout, because that is what the flake actually is ----------------------
# The symptom is `ValueError: I/O operation on closed file` raised from inside Rich, and it presents as an
# INTERMITTENT failure in whichever test happens to write to a console next:
#
#   run 1: FAILED test_ollama_wiring.py::test_the_ollama_flag_without_ollama_explains_and_exits
#   run 2: FAILED test_dots_panel.py::TestLociPanelHTTP::test_goal_add_and_status
#
# `sys.stdout` itself has been closed by something earlier in the run. Once that happens every later test that
# prints fails, which is why no single test file reproduces it and why it needs an accumulated run.
#
# THE EXPLANATION PREVIOUSLY RECORDED HERE WAS WRONG, and the correction matters because it pointed the fix in
# the wrong direction. The note claimed Rich "caches the resolved file" at construction, so a Console built in
# one test would write to a closed capture in the next. That was MEASURED and is FALSE:
#
#   tests/test_zz_survivor_experiment.py builds a Console in one test, writes to it from the next, and finds
#   `console.file is sys.stdout` still true and the write landing correctly in the new test's capture.
#
# Rich resolves `sys.stdout` at WRITE time, so a stale Console is harmless and no amount of rebuilding consoles
# would have helped. The stream has to be repaired, not the console.
#
# This fixture does that: if the stream is closed at the START of a test, it restores the interpreter's own
# `__stdout__` and warns. It repairs rather than asserts, because a repaired stream still lets the test in
# question report its real result -- whereas an unhandled ValueError turns one broken stream into a cascade of
# unrelated failures that hides which test broke it.
#
# This does NOT identify the culprit. It stops one closed stream from failing every subsequent test, and it
# names the first test to notice, which narrows the search. Recorded honestly as a mitigation.
def _stream_is_usable(stream) -> bool:
    """Whether a standard stream can still be written to."""
    if stream is None:
        return False
    try:
        if stream.closed:
            return False
        stream.flush()
        return True
    except (ValueError, OSError):
        return False


# --- environment isolation, which is what the ordering flake actually was -----------------------
# `myriaxon.cli.main()` calls `load_dotenv_into_environ()`, and that loader -- correctly, for production --
# never overwrites a variable that is already set, so **the first `main()` in a test session permanently
# injects the repository's real `.env` into `os.environ`** and nothing ever takes it out.
#
# Every test after that therefore runs against whatever the developer's `.env` happens to contain. On this
# machine that file sets `MYRIAXON_PROVIDER`, API keys and a context window, so behaviour that depends on
# ambient configuration starts differing partway through a run. That is the whole shape of the reported flake:
#
#   * it needs an ACCUMULATED run, because it takes one `main()` call to leak;
#   * no single test file reproduces it, because one file may not call `main()` before the failing test;
#   * the failing test MOVES between runs -- observed across this session on
#     `test_the_ollama_flag_without_ollama_explains_and_exits`, `test_a_saved_ollama_choice_is_kept`, and
#     `test_dots_panel.py::TestLociPanelHTTP::test_goal_add_and_status`;
#   * and it presents as an empty capture, because a config that resolves successfully prints nothing and the
#     test expecting `SystemExit` then asserts against `''`.
#
# Snapshotting `os.environ` around every test removes the dependency entirely rather than asking 58 test files
# to remember to clean up after themselves -- the same reasoning as `_fresh_locus_registry` above.
#
# `monkeypatch` cannot be used for this: it only undoes what it was asked to set, and the leak happens deep
# inside a call the test makes rather than at a line the test controls.
@pytest.fixture(autouse=True)
def _isolate_environment():
    import os as _os_env

    before = dict(_os_env.environ)
    try:
        yield
    finally:
        for key in list(_os_env.environ):
            if key not in before:
                # Added during the test (by the dotenv loader or a stray setenv): remove it, so the next test
                # sees the environment this one started with.
                _os_env.environ.pop(key, None)
        for key, value in before.items():
            if _os_env.environ.get(key) != value:
                _os_env.environ[key] = value


@pytest.fixture(autouse=True)
def _repair_closed_standard_streams():
    import sys as _sys
    import warnings as _warnings

    repaired = []
    for name in ("stdout", "stderr"):
        current = getattr(_sys, name, None)
        if _stream_is_usable(current):
            continue
        fallback = getattr(_sys, f"__{name}__", None)
        if fallback is not None and _stream_is_usable(fallback):
            setattr(_sys, name, fallback)
            repaired.append(name)
    if repaired:
        _warnings.warn(
            f"standard stream(s) {', '.join(repaired)} were CLOSED at the start of this test and have been "
            f"restored. Something earlier in the run closed them; the first test to notice is the one that "
            f"just reported this warning.",
            stacklevel=1,
        )
    yield


# --- TEMPORARY INSTRUMENTATION: which stream does a Console actually write to? ------------------
# Enabled only with MYRIAXON_TRACE_CONSOLE=1 so it costs nothing in a normal run. It answers ONE question the
# `capsys` failure makes unavoidable: pytest reports `Captured stdout call` CONTAINING the message while
# `capsys.readouterr().out` is EMPTY, so the write reached file descriptor 1 but not the object pytest
# installed as `sys.stdout` for this test. That means the Console held a `file` from an earlier capture
# generation. This records the identity so the stale holder can be found instead of guessed at.
if _os.environ.get("MYRIAXON_TRACE_CONSOLE") == "1":  # pragma: no cover - diagnostics only
    import sys as _sys_trace

    from rich.console import Console as _Console

    _TRACE = {"calls": 0, "records": []}

    def _tracing_print(self, *args, **kwargs):  # noqa: ANN001
        if _TRACE["calls"] < 40:
            _TRACE["calls"] += 1
            _TRACE["records"].append({
                "file_is_sys_stdout": self.file is _sys_trace.stdout,
                "file_is_sys_stderr": self.file is _sys_trace.stderr,
                "file_type": type(self.file).__name__,
                "file_id": id(self.file),
                "stdout_id": id(_sys_trace.stdout),
                "quiet": self.quiet,
            })
        return _ORIGINAL_PRINT(self, *args, **kwargs)

    _ORIGINAL_PRINT = _Console.print
    _Console.print = _tracing_print

    def _dump_trace(*_args):  # noqa: ANN001
        import json as _json

        stale = [r for r in _TRACE["records"] if not r["file_is_sys_stdout"]]
        _sys_trace.__stderr__.write("\n=== CONSOLE TRACE ===\n")
        _sys_trace.__stderr__.write(
            f"prints seen: {len(_TRACE['records'])}  stale-stream writes: {len(stale)}\n")
        _sys_trace.__stderr__.write(_json.dumps(_TRACE["records"][:12], indent=2) + "\n")
        _sys_trace.__stderr__.write("=== END TRACE ===\n")
        _sys_trace.__stderr__.flush()

    import atexit as _atexit

    _atexit.register(_dump_trace)


# --- NOT adding a stable-output-stream fixture here ---------------------------------------------
# This is a record of a fix that was TRIED and REVERTED, left because the next person will have the
# same idea.
#
# The problem it aimed at is real: `rich.Console(quiet=True)` still binds `sys.stdout` -- `quiet`
# suppresses the live display and spinner, not the stream -- and four places in the package build one
# that way. Rich caches the resolved file, pytest closes and replaces its capture between tests, and
# the result is `ValueError: I/O operation on closed file` from inside Rich.
#
# The attempted fix was an autouse fixture replacing `sys.stdout`/`sys.stderr` with a wrapper that is
# never closed and forwards to the current stream. It was reverted because it made things WORSE, and
# the measurement says so plainly:
#
#     with the fixture      : run 1 failed on test_the_ollama_flag_without_ollama_explains_and_exits
#                             run 2 failed on test_use_command_saves_the_model_and_warns_...
#     with it disabled      : 660 passed
#
# Two different failures on consecutive runs is the signature of interference, not repair. The likely
# mechanism is that pytest's `capsys` reads the real descriptor while the wrapper writes through a
# different path, so output stopped being captured where tests expect it.
#
# --- RESOLVED: the flake was in the TEST, not the harness ----------------------------------------
# The ordering fragility above is now FIXED, and the fix was in `test_ollama_wiring.py` rather than here.
#
# Four hypotheses about a stale or closed stream were tested and all four were FALSE:
#
#   1. Rich capturing `sys.stdout` at construction   -- `console.file is sys.stdout` still true at write time
#   2. A Console surviving into the next test        -- a Console built in one test writes correctly in the next
#   3. `sys.stdout` being closed                     -- the repair fixture below never fired
#   4. `.env` leaking into `os.environ`              -- isolated for every test; the flake persisted
#
# What the failure report actually showed is the whole answer:
#
#     E  assert ('Ollama' in '')
#     Captured stdout call
#       Ollama is installed but not answering at http://127.0.0.1:9. Start it with: ...
#
# The message WAS emitted. pytest captured it at the file-descriptor level and displayed it in the very
# failure that claimed the string was empty. `capsys` reads the object pytest installs as `sys.stdout` for
# one test, and a Rich `Console` resolves its stream through state pytest rewrites between tests, so which
# capture receives the write depends on run ordering.
#
# So the assertion was testing pytest's capture plumbing, not the product. The test now binds its Console to
# a `StringIO` it owns and asserts on that, which no ordering can defeat. Every other test in this suite
# already asserts on returned values rather than on captured console output.
#
# Two guards below are kept because they are worth having on their own merits, NOT because they fixed this:
# environment isolation (a leaked `.env` really does change behaviour part-way through a run) and the
# closed-stream repair (it turns a cascade of unrelated failures into one named warning). Both are honest
# mitigations, and neither is claimed as the fix.


def _stream_is_usable(stream) -> bool:
    """Whether a standard stream can still be written to."""
    if stream is None:
        return False
    try:
        if stream.closed:
            return False
        stream.flush()
        return True
    except (ValueError, OSError):
        return False


@pytest.fixture(autouse=True)
def _repair_closed_standard_streams():
    import sys as _sys
    import warnings as _warnings

    repaired = []
    for name in ("stdout", "stderr"):
        current = getattr(_sys, name, None)
        if _stream_is_usable(current):
            continue
        fallback = getattr(_sys, f"__{name}__", None)
        if fallback is not None and _stream_is_usable(fallback):
            setattr(_sys, name, fallback)
            repaired.append(name)
    if repaired:
        _warnings.warn(
            f"standard stream(s) {', '.join(repaired)} were CLOSED at the start of this test and have been "
            f"restored. Something earlier in the run closed them; the first test to notice is the one that "
            f"just reported this warning.",
            stacklevel=1,
        )
    yield
