#!/usr/bin/env python3
"""Drive the dashboard in a REAL browser and check it, because reading markup proves nothing.

WHY THIS EXISTS

The pane wall was reported as working on the strength of two things: the API returned the right JSON, and the
page contained the expected ids. Both were true, and the wall was **completely dead** -- `renderWall` called
`wall.linksOn()`, which did not exist, and the exception escaped a `forEach` so the panes were never appended.
The room opened, looked empty, and said nothing. Every static check passed.

`tools/ui_check.py` is good and stays -- it caught a different real defect (a Clear button wired to an id that
did not exist). But a static check cannot answer "does dragging a pane move it", and that is the question.

So this starts real Chrome, drives the real page over the DevTools Protocol, and asserts on the DOM after real
clicks. No mocking, no jsdom, and nothing to install: Chrome ships on Windows, and node 22+ has WebSocket
built in.

USAGE

    python tools/browser/wall_check.py              # all three drivers
    python tools/browser/wall_check.py --only hard  # one of them
    python tools/browser/wall_check.py --url http://127.0.0.1:9000

It SKIPS rather than fails when Chrome or node is absent, and says which. A check that cannot run is not a
check that failed, and reporting it as a failure teaches people to ignore the suite.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent

CHROME_CANDIDATES = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
)

DRIVERS = {
    "drive": ("wall-drive.mjs", "the wall as a user reaches it"),
    "hard": ("wall-hard.mjs", "the claims a happy path cannot prove: real moves, the join, failures, a live turn"),
    "edges": ("wall-edges.mjs", "async failures, a missing agent, nine panes, bounded reporting"),
}


def find_chrome() -> str | None:
    for candidate in CHROME_CANDIDATES:
        if candidate and Path(candidate).is_file():
            return candidate
    for name in ("google-chrome", "chrome", "chromium", "msedge"):
        found = shutil.which(name)
        if found:
            return found
    return None


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_for(url: str, seconds: float = 25.0) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3) as response:
                if response.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(0.6)
    return False


def start_dashboard(port: int, workspace: Path) -> subprocess.Popen | None:
    """Serve the dashboard in a child process, so the checks do not need one already running."""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO)
    env["MYRIAXON_TRANSPARENCY"] = "8"
    env["MYRIAXON_NO_SUPERVISOR"] = "1"   # a browser test must not start real agent runs on its own
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        process = subprocess.Popen(
            [sys.executable, "-m", "myriaxon", "web", "--no-browser", "--port", str(port),
             "--workspace", str(workspace)],
            cwd=str(REPO), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            encoding="utf-8", errors="replace",
        )
    except OSError as exc:
        print(f"  could not start the dashboard: {exc}")
        return None
    return process


def main() -> int:
    parser = argparse.ArgumentParser(description="Drive the dashboard in a real browser.")
    parser.add_argument("--only", default="", help="comma-separated: " + ", ".join(DRIVERS))
    parser.add_argument("--url", default="", help="an already-running dashboard (skips starting one)")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--keep-open", action="store_true", help="leave the browser running afterwards")
    args = parser.parse_args()

    chrome = find_chrome()
    node = shutil.which("node")
    if not chrome or not node:
        missing = ", ".join(n for n, v in (("Chrome", chrome), ("node", node)) if not v)
        print(f"  SKIP: {missing} not available, so the browser checks cannot run.")
        print("        These are real checks and they are not failing -- they did not run.")
        return 0

    selected = [name for name in DRIVERS if not args.only or name in args.only.split(",")]
    if not selected:
        print(f"  nothing selected; choose from {', '.join(DRIVERS)}")
        return 2

    dashboard = None
    base = args.url
    if not base:
        port = args.port or free_port()
        scratch = Path(tempfile.mkdtemp(prefix="mx-wallcheck-"))
        print(f"  starting the dashboard on port {port} (workspace {scratch})")
        dashboard = start_dashboard(port, scratch)
        base = f"http://127.0.0.1:{port}"
        if dashboard is None or not wait_for(base):
            print("  the dashboard did not come up; cannot check the wall")
            if dashboard:
                dashboard.terminate()
            return 1

    cdp_port = free_port()
    profile = Path(tempfile.mkdtemp(prefix="mx-chrome-"))
    print(f"  launching {Path(chrome).name} with DevTools on {cdp_port}")
    browser = subprocess.Popen(
        [chrome, "--headless=new", f"--remote-debugging-port={cdp_port}", "--remote-allow-origins=*",
         f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check",
         "--disable-gpu", "--window-size=1600,1000", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    if not wait_for(f"http://127.0.0.1:{cdp_port}/json/version"):
        print("  Chrome did not expose DevTools; cannot check the wall")
        browser.terminate()
        if dashboard:
            dashboard.terminate()
        return 1

    env = dict(os.environ)
    env["MYX_BASE"] = base
    # The drivers take the port through the environment rather than an argument, so the same file can be run by
    # hand against a browser somebody already has open.
    failures: list[str] = []
    try:
        for name in selected:
            script, description = DRIVERS[name]
            print()
            print(f"--- {name}: {description} " + "-" * max(0, 50 - len(description)))
            # Each driver attaches to the FIRST page target, so they must not run concurrently against one
            # browser. Sequential by necessity, not by preference.
            completed = subprocess.run(
                [node, str(HERE / script)], cwd=str(REPO), env={**env, "MYX_CDP_PORT": str(cdp_port)},
                text=True, encoding="utf-8", errors="replace", timeout=900,
            )
            if completed.returncode != 0:
                failures.append(name)
    except subprocess.TimeoutExpired:
        failures.append("timeout")
    finally:
        if not args.keep_open:
            browser.terminate()
        if dashboard:
            dashboard.terminate()
            try:
                dashboard.wait(timeout=10)
            except subprocess.TimeoutExpired:
                dashboard.kill()
        shutil.rmtree(profile, ignore_errors=True)

    print()
    print("=" * 78)
    if failures:
        print(f"  {len(selected) - len(failures)}/{len(selected)} driver(s) passed; FAILED: {', '.join(failures)}")
    else:
        print(f"  all {len(selected)} driver(s) passed")
    print("=" * 78)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
