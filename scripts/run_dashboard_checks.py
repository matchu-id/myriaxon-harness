"""Run the dashboard server and every dashboard check in one process lifetime.

    python scripts/run_dashboard_checks.py

Two environment constraints shaped this file:

  * A server started as a detached background job is terminated between tool invocations, so its
    checks fail with "connection refused" for reasons unrelated to the code. The server therefore
    runs on a thread inside this process, which keeps it alive for the whole run.
  * Child processes with piped stdio fail in this sandbox (EPERM on the pipe), so every check is
    invoked IN-PROCESS by calling its main() rather than through subprocess. That also means output
    interleaves normally instead of being buffered until each child exits.
"""
import importlib.util
import os
import sys
import threading
import time
import traceback
import urllib.request
import warnings
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO / "scripts"
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(SCRIPTS_DIR))
warnings.filterwarnings("ignore")

PORT = 8791
WORKSPACE = REPO / "bench-out" / "checks"

# Isolate the user-level config for this run, BEFORE myriaxon.constants is imported.
#
# Two reasons, both measured:
#   * Suites interfere through shared state. check_rules_ui writes a `write_file: deny` rule to the
#     user settings file; a later suite that writes a file then fails for a reason that has nothing
#     to do with what it is testing. That is exactly how the plugin-shadowing security check failed
#     inside this runner while passing standalone.
#   * On this machine `~/.myriaxon` is not writable, so the user config falls back into the project --
#     inside the workspace, which the sandbox permits, but shared across runs.
# Pointing it at a per-run directory inside the workspace makes each run independent and writable,
# and leaves the developer's real policy file untouched.
_USER_CONFIG = REPO / "bench-out" / "checks-userconfig"
_USER_CONFIG.mkdir(parents=True, exist_ok=True)
os.environ["MYRIAXON_USER_CONFIG_DIR"] = str(_USER_CONFIG)

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from myriaxon.dotenv_support import load_dotenv_into_environ  # noqa: E402

load_dotenv_into_environ()


def start_server(port: int) -> dict:
    from rich.console import Console

    from myriaxon.config import resolve_config
    from myriaxon.dashboard.server import serve

    WORKSPACE.mkdir(parents=True, exist_ok=True)
    config = resolve_config(Console(quiet=True))
    state: dict = {}

    def run():
        try:
            serve(WORKSPACE, config=config, host="127.0.0.1", port=port, mode="auto_edit", open_browser=False)
        except BaseException:  # noqa: BLE001
            state["traceback"] = traceback.format_exc()

    threading.Thread(target=run, daemon=True, name="dashboard-checks").start()
    return state


def wait_ready(port: int, timeout: float = 40.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=3).read()
            return True
        except Exception:  # noqa: BLE001
            time.sleep(0.5)
    return False


def run_module(name: str, argv: list[str]) -> bool:
    """Run a check script's main() in this process and report whether it passed.

    The working directory is saved and restored around every check. This is not tidiness: an
    in-process dashboard server calls `os.chdir()` into its workspace by design, and the restore in
    `serve()` lives in a `finally` that only runs when `serve_forever` returns -- which never happens
    for a server left running on a daemon thread. Measured consequence: after the launch check, every
    later suite ran from that workspace instead of the repository, and the plugin-shadowing check
    failed with "outside the project directory" for a reason that had nothing to do with plugins.
    """
    import importlib.util

    original_cwd = Path.cwd()
    path = SCRIPTS_DIR / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, str(path))
    if spec is None or spec.loader is None:
        print(f"  could not load {path}")
        return False
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    saved_argv = sys.argv
    try:
        spec.loader.exec_module(module)
        sys.argv = [name, *argv]
        code = module.main()
    except SystemExit as exc:
        code = exc.code or 0
    except BaseException:  # noqa: BLE001
        traceback.print_exc()
        code = 1
    finally:
        sys.argv = saved_argv
        try:
            os.chdir(original_cwd)
        except OSError:
            pass
    return code == 0


def run_pytest() -> bool:
    import pytest

    code = pytest.main([
        str(REPO / "tests" / "test_model_adapt.py"),
        str(REPO / "tests" / "test_bench_harness.py"),
        "-q", "--no-header", "-p", "no:cacheprovider",
    ])
    return int(code) == 0


def main() -> int:
    results: list[tuple[str, bool]] = []

    print("=" * 60)
    print("offline unit suites")
    print("=" * 60)
    ok = run_pytest()
    results.append(("unit tests", ok))

    print()
    print("=" * 60)
    print("markdown renderer (extracted and run under node)")
    print("=" * 60)
    ok = run_module("check_markdown_render", [])
    results.append(("markdown renderer", ok))

    print()
    print("=" * 60)
    print("history handoff across a provider rebuild")
    print("=" * 60)
    ok = run_module("check_history_handoff", [])
    results.append(("history handoff", ok))

    print()
    print("=" * 60)
    print("harness launch from a non-writable directory (the reported crash)")
    print("=" * 60)
    ok = run_module("check_harness_launch", [])
    results.append(("harness launch", ok))

    print()
    print("=" * 60)
    print("plugin API (offline, with fixtures)")
    print("=" * 60)
    ok = run_module("check_plugins", [])
    results.append(("plugin API", ok))

    print()
    print("=" * 60)
    print("plugin shadowing of built-in tools (security)")
    print("=" * 60)
    ok = run_module("check_plugin_shadowing", [])
    results.append(("plugin shadowing", ok))

    print()
    print("=" * 60)
    print("live MCP: a real server over remote HTTP")
    print("=" * 60)
    ok = run_module("check_mcp_remote_live", [])
    results.append(("live MCP (remote)", ok))

    print()
    print("=" * 60)
    print("messaging / WhatsApp (real HTTP, real agent turn)")
    print("=" * 60)
    ok = run_module("check_messaging", [])
    results.append(("messaging engine", ok))

    print()
    print("=" * 60)
    print("live MCP: a real server over stdio")
    print("=" * 60)
    ok = run_module("check_mcp_live", [])
    results.append(("live MCP (stdio)", ok))

    print()
    print("=" * 60)
    print(f"dashboard server on port {PORT}")
    print("=" * 60)
    state = start_server(PORT)
    if not wait_ready(PORT):
        print("server did not become ready")
        if state.get("traceback"):
            print(state["traceback"][-1500:])
        results.append(("server startup", False))
        return 1
    print("ready\n")
    results.append(("server startup", True))

    checks = [
        ("UI markup", "check_ui_markup"),
        ("core e2e", "check_dashboard_e2e"),
        ("commands + vision", "check_dashboard_features"),
        ("provider + sessions", "check_dashboard_provider_sessions"),
        ("session delete + projects", "check_sessions_projects"),
        ("settings + workflow", "check_settings"),
        ("custom endpoints + vendor switching", "check_endpoints"),
        ("MCP server management", "check_mcp_ui"),
        ("MCP preconfigured catalog", "check_mcp_catalog"),
        ("permission rules in the UI", "check_rules_ui"),
        ("plugins panel", "check_plugins_ui"),
        ("messaging panel + webhook", "check_messaging_ui"),
        ("file explorer + diff review", "check_files_ui"),
        ("visual render in a real browser", "check_ui_visual"),
        ("dashboard JavaScript", "check_ui_js"),
        ("streamed markdown answer", "check_answer_stream"),
    ]
    for label, module in checks:
        print()
        print("=" * 60)
        print(label)
        print("=" * 60)
        # The vision check needs an image; reuse an uploaded one if present, else skip that part.
        argv = [str(PORT)]
        if module == "check_dashboard_features":
            candidate = REPO / "bench-out" / "uidemo2" / ".myriaxon" / "uploads"
            images = sorted(candidate.glob("*.png")) if candidate.is_dir() else []
            if images:
                argv.append(str(images[0]))
        ok = run_module(module, argv)
        results.append((label, ok))

    print()
    print("=" * 60)
    print("summary")
    print("=" * 60)
    for label, ok in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {label}")
    failed = [label for label, ok in results if not ok]
    print()
    print("ALL PASSED" if not failed else f"FAILED: {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
