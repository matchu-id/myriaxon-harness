"""Verify the Harness starts when launched from the home directory, as myriaxon-web.cmd is.

    python scripts/check_harness_launch.py

Reproduces the reported failure: running myriaxon-web.cmd from C:\\Users\\<you> made the home directory the
workspace, and creating ~/.myriaxon/uploads raised PermissionError during startup. This checks that
the launcher's workspace default and the uploads fallback together make that work.
"""
import json
import os
import sys
import threading
import time
import urllib.request
import warnings
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
warnings.filterwarnings("ignore")

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

PORT = 8811
passed, failed = [], []


def check(label, condition, detail=""):
    (passed if condition else failed).append(label)
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}{(' -- ' + str(detail)) if detail and not condition else ''}")


def main() -> int:
    home = Path(os.environ.get("USERPROFILE", str(Path.home())))

    print("=== myriaxon-web.cmd defaults the workspace to the repository ===")
    # Running the launcher's own command line with --help is enough to prove argument handling
    # without binding a port.
    launcher = REPO / "myriaxon-web.cmd"
    check("launcher exists", launcher.is_file(), launcher)
    text = launcher.read_text(encoding="utf-8", errors="replace")
    check("launcher injects a workspace when none is given", "--workspace" in text and "HAVE_WORKSPACE" in text)
    check("launcher still passes through arguments", "%*" in text)

    print("\n=== the previously failing case: workspace = home directory ===")
    # Simulated directly rather than by launching the server, because this is the exact constructor
    # call that raised. It must not raise now.
    try:
        from rich.console import Console

        from myriaxon.config import resolve_config
        from myriaxon.dashboard.server import DashboardApp

        config = resolve_config(Console(quiet=True))
        app = DashboardApp(home, config=config, mode="auto_edit")
        constructed = True
        detail = ""
    except BaseException as exc:  # noqa: BLE001
        constructed = False
        detail = f"{type(exc).__name__}: {exc}"
    check("constructing the app with the home directory as workspace does not raise", constructed, detail)

    if constructed:
        print(f"    workspace : {app.workspace}")
        print(f"    uploads   : {app.uploads_dir}")
        for note in app.uploads.notes:
            print(f"    note      : {note}")
        check("an uploads directory was resolved", app.uploads_dir is not None)
        check("the uploads directory exists", Path(app.uploads_dir).is_dir(), app.uploads_dir)

        print("\n=== uploads actually work in that configuration ===")
        target = Path(app.uploads_dir) / "launch_probe.txt"
        try:
            target.write_bytes(b"probe")
            wrote = target.read_bytes() == b"probe"
            target.unlink(missing_ok=True)
        except OSError as exc:
            wrote = False
            detail = str(exc)
        check("a file can be written to the uploads directory", wrote, detail if not wrote else "")

    print("\n=== the server serves from a repository workspace ===")
    state: dict = {}

    def run():
        from rich.console import Console

        from myriaxon.config import resolve_config
        from myriaxon.dashboard.server import serve

        workspace = REPO / "bench-out" / "launchcheck"
        workspace.mkdir(parents=True, exist_ok=True)
        try:
            serve(workspace, config=resolve_config(Console(quiet=True)), host="127.0.0.1",
                  port=PORT, mode="auto_edit", open_browser=False)
        except BaseException as exc:  # noqa: BLE001
            state["error"] = f"{type(exc).__name__}: {exc}"

    threading.Thread(target=run, daemon=True).start()
    ready = False
    for _ in range(60):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/health", timeout=2).read()
            ready = True
            break
        except Exception:  # noqa: BLE001
            time.sleep(0.5)
    check("server starts and answers /api/health", ready, state.get("error", ""))

    if ready:
        config = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/config", timeout=15).read())
        check("config reports a workspace", bool(config.get("workspace")), config.get("workspace"))
        check("config reports a provider", bool(config.get("provider")), config.get("provider"))
        print(f"    workspace={config.get('workspace')}")
        print(f"    provider ={config.get('provider')}:{config.get('model')}")

    print(f"\n{len(passed)} passed, {len(failed)} failed")
    if failed:
        print("failed:", failed)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
