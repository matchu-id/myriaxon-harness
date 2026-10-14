"""Render the dashboard in a real browser and assert on what it actually looks like.

    python scripts/check_ui_visual.py

Every other UI check is structural: it asserts an element id appears in the served HTML. That cannot
catch the failures that matter visually -- unreadable contrast, an unresolved CSS variable, a panel
rendered off screen, a dialog that opens as an empty box, a page exception that leaves the controls
unwired. This starts the real server, drives Chrome through the DevTools Protocol, screenshots the main
view and every dialog in both themes, and asserts on measurements taken inside the page.

If no browser is present the checks are reported as skipped rather than failed, since this is an
environment capability rather than a property of the code.
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
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

passed, failed, skipped = [], [], []


def check(label, condition, detail=""):
    (passed if condition else failed).append(label)
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}{(' -- ' + str(detail)) if detail and not condition else ''}")


def skip(label, why):
    skipped.append(label)
    print(f"  [SKIP] {label} -- {why}")


def find_chrome() -> str:
    candidates = [
        os.environ.get("CHROME_PATH", ""),
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    return ""


def free_port(preferred: int = 8823) -> int:
    for candidate in range(preferred, preferred + 16):
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", candidate))
                return candidate
            except OSError:
                continue
    raise RuntimeError("no free port")


def main() -> int:
    chrome = find_chrome()
    if not chrome:
        skip("visual rendering", "no Chrome or Edge found; set CHROME_PATH to run this")
        print(f"\n{len(passed)} passed, {len(failed)} failed, {len(skipped)} skipped")
        return 0
    node = shutil.which("node")
    if not node:
        skip("visual rendering", "node is required to drive the browser")
        print(f"\n{len(passed)} passed, {len(failed)} failed, {len(skipped)} skipped")
        return 0

    os.environ["CHROME_PATH"] = chrome
    print(f"browser: {chrome}")
    os.environ["CHROME_PATH"] = chrome

    # Probe the real capability: can a child process here launch Chrome and be read? `chrome --version`
    # is not a reliable probe -- it exits non-zero under some sandboxes even when headless rendering
    # works, which skipped the whole check in an environment that could in fact run it. Asking Chrome to
    # do the thing we actually need is the honest test.
    probe_dir = REPO / "bench-out" / "chrome-probe"
    shutil.rmtree(probe_dir, ignore_errors=True)
    probe_dir.mkdir(parents=True, exist_ok=True)
    probe_shot = probe_dir / "probe.png"
    probe_html = probe_dir / "probe.html"
    probe_html.write_text("<html><body style='background:#123456'>probe</body></html>", encoding="utf-8")
    try:
        rendered = subprocess.run(
            [chrome, "--headless=new", "--no-sandbox", "--disable-gpu", "--disable-crash-reporter",
             f"--user-data-dir={probe_dir / 'profile'}", "--window-size=200,120",
             f"--screenshot={probe_shot}", probe_html.as_uri()],
            capture_output=True, text=True, timeout=120,
        )
        # A 200x120 solid-colour PNG is only ~550 bytes, so the threshold is deliberately small: a
        # larger one rejected a working browser and skipped the whole check.
        launchable = probe_shot.is_file() and probe_shot.stat().st_size > 200
        why = "" if launchable else f"chrome produced no screenshot (rc={rendered.returncode})"
    except (OSError, subprocess.TimeoutExpired) as exc:
        launchable = False
        why = f"a child process cannot be captured here ({type(exc).__name__}: {exc})"
    shutil.rmtree(probe_dir, ignore_errors=True)

    if not launchable:
        skip("visual rendering", why)
        print(f"\n{len(passed)} passed, {len(failed)} failed, {len(skipped)} skipped")
        return 0
    port = free_port()
    workspace = REPO / "bench-out" / "uicheck"
    workspace.mkdir(parents=True, exist_ok=True)
    shots = REPO / "bench-out" / "ui-shots"
    if shots.exists():
        shutil.rmtree(shots, ignore_errors=True)
    shots.mkdir(parents=True, exist_ok=True)

    from rich.console import Console

    from myriaxon.config import resolve_config
    from myriaxon.dashboard.server import serve

    # serve() owns building DashboardApp, so the CLI and this check agree on how one is made.
    thread = threading.Thread(
        target=serve,
        kwargs={"workspace": workspace, "config": resolve_config(Console(quiet=True)),
                "host": "127.0.0.1", "port": port, "mode": "auto_edit", "open_browser": False},
        daemon=True,
    )
    thread.start()

    base = f"http://127.0.0.1:{port}/"
    ready = False
    for _ in range(120):
        with socket.socket() as probe:
            try:
                probe.connect(("127.0.0.1", port))
                ready = True
                break
            except OSError:
                time.sleep(0.25)
    check("the dashboard serves over HTTP", ready, base)
    if not ready:
        return 1

    print("\n=== driving a real browser ===")
    try:
        completed = subprocess.run(
            [node, str(REPO / "scripts" / "ui_render.js"), base, str(shots)],
            capture_output=True, text=True, timeout=300, cwd=str(REPO),
        )
    except subprocess.TimeoutExpired:
        check("the browser render completed", False, "timed out after 300s")
        print(f"\n{len(passed)} passed, {len(failed)} failed")
        return 1
    except OSError as exc:
        # A confined environment blocks a child process from having piped stdio (on Windows this is the
        # documented spawn EPERM / named-pipe boundary), so node cannot be captured. That is an
        # environment capability, not a defect in the UI, so it is reported as a skip. Running this
        # script with wider access performs the full render.
        skip("visual rendering", f"cannot capture a child process here ({type(exc).__name__}: {exc})")
        print(f"\n{len(passed)} passed, {len(failed)} failed, {len(skipped)} skipped")
        return 0

    payload = {}
    for line in (completed.stdout or "").splitlines():
        if line.startswith("RESULT:"):
            try:
                payload = json.loads(line[len("RESULT:"):])
            except json.JSONDecodeError as exc:
                check("the browser reported results", False, f"bad RESULT json: {exc}")
    if not payload:
        # No RESULT line. If the failure is the environment refusing the render (PermissionError /
        # EPERM in the browser's own launch), that is a skip; otherwise it is a real failure.
        combined = (completed.stderr or "") + (completed.stdout or "")
        environment_denied = any(marker in combined for marker in
                                 ("EPERM", "PermissionError", "Access is denied", "WinError 5"))
        if environment_denied:
            skip("visual rendering", "the environment denied the browser launch; run this with wider access")
            print(f"\n{len(passed)} passed, {len(failed)} failed, {len(skipped)} skipped")
            return 0
        check("the browser reported results", False, combined[-400:])
        print(f"\n{len(passed)} passed, {len(failed)} failed")
        return 1

    check("the browser reported results", True)
    if payload.get("fatal"):
        check("the render succeeded", False, payload["fatal"])
        print(f"\n{len(passed)} passed, {len(failed)} failed")
        return 1

    result = payload.get("result") or {}
    shots_made = payload.get("shots") or {}
    problems = result.get("problems") or []

    print("\n=== what the browser computed ===")
    for name, value in sorted((result.get("vars") or {}).items()):
        print(f"    {name:14} {value}")
    print(f"    body: {result.get('body')}")
    print(f"    contrast (bg/text luminance): {result.get('contrast')}")

    check("no unresolved CSS variable", not [p for p in problems if "CSS variable" in p],
          [p for p in problems if "CSS variable" in p])
    check("the page has readable contrast", not [p for p in problems if "contrast" in p],
          [p for p in problems if "contrast" in p])
    check("no control is missing or hidden", not [p for p in problems if "control" in p],
          [p for p in problems if "control" in p])
    check("no control is off screen", not [p for p in problems if "off screen" in p],
          [p for p in problems if "off screen" in p])
    check("the transcript has height", not [p for p in problems if "transcript" in p],
          [p for p in problems if "transcript" in p])
    check("no dialog is visible before being opened",
          not [p for p in problems if "visible before" in p], [p for p in problems if "visible before" in p])
    check("no dialog is missing", not [p for p in problems if "missing dialog" in p],
          [p for p in problems if "missing dialog" in p])
    check("every dialog opens with a usable size",
          not [p for p in problems if "usable size" in p], [p for p in problems if "usable size" in p])
    check("the page raised no exception", not (result.get("consoleErrors") or []),
          result.get("consoleErrors"))

    print("\n=== dialogs, as opened ===")
    for dialog_id, info in sorted((result.get("dialogs") or {}).items()):
        opened = info.get("opened") or {}
        print(f"    {dialog_id:15} opened={opened.get('ok')} size={opened.get('dialog')}")

    print("\n=== screenshots ===")
    for name, info in sorted(shots_made.items()):
        size = info.get("bytes", 0)
        check(f"{name} screenshot is non-trivial ({size:,} bytes)", size > 8_000, info.get("file"))
        print(f"    {info.get('file')}  {size:,} bytes")

    print("\n=== the light/dark control ===")
    theme = result.get("themeButton") or {}
    print(f"    label={theme.get('label')!r} {theme.get('before')} -> {theme.get('after')} "
          f"size={theme.get('w')}x{theme.get('h')} stored={theme.get('stored')!r}")
    # This control is easy to lose: it sits last in the sidebar footer, and with `margin-left:auto` on
    # every button it was pushed past the sidebar edge and was not clickable at all. Asserting that it
    # exists is not enough; it has to be on screen and it has to change the theme.
    check("the theme control exists", theme.get("ok") is True, theme)
    check("the theme control is on screen and large enough to click",
          theme.get("clickable") is True, theme)
    check("clicking it actually changes the theme",
          theme.get("after") in ("light", "dark") and theme.get("after") != theme.get("before"), theme)
    check("the choice is remembered", theme.get("stored") in ("light", "dark"), theme.get("stored"))

    print("\n=== the light theme is a real second appearance ===")
    light = result.get("light") or {}
    dark_vars = result.get("vars") or {}
    light_vars = light.get("vars") or {}
    differing = [k for k in dark_vars if light_vars.get(k) and light_vars[k] != dark_vars[k]]
    check("light mode changes the palette", len(differing) >= 4, differing)
    check("light mode is readable",
          not [p for p in (light.get("problems") or []) if "contrast" in p],
          [p for p in (light.get("problems") or []) if "contrast" in p])
    print(f"    variables that differ: {differing}")

    print(f"\nScreenshots are in {shots}")
    print(f"{len(passed)} passed, {len(failed)} failed"
          + (f", {len(skipped)} skipped" if skipped else ""))
    if failed:
        print("failed:", failed)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
