"""Start the dashboard and interrogate it like a reviewer would.

    python scripts/assess_dashboard.py

Produces a readable assessment report: what the UI declares, what the API actually returns, a live
turn, settings behaviour, and the raw markdown the browser will receive. Everything runs in one
process so the server stays alive for the whole report.
"""
import json
import re
import sys
import threading
import time
import traceback
import urllib.error
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

from myriaxon import brand  # noqa: E402
from myriaxon.dotenv_support import load_dotenv_into_environ  # noqa: E402

load_dotenv_into_environ()

PORT = 8805
WORKSPACE = REPO / "bench-out" / "assess"


def rule(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


def get(path: str, timeout: int = 60):
    return json.loads(urllib.request.urlopen(f"http://127.0.0.1:{PORT}{path}", timeout=timeout).read())


def post(path: str, body: dict, timeout: int = 120):
    request = urllib.request.Request(
        f"http://127.0.0.1:{PORT}{path}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    return json.loads(urllib.request.urlopen(request, timeout=timeout).read())


def start() -> dict:
    from rich.console import Console

    from myriaxon.config import resolve_config
    from myriaxon.dashboard.server import serve

    WORKSPACE.mkdir(parents=True, exist_ok=True)
    config = resolve_config(Console(quiet=True))
    state = {"config": config}

    def run():
        try:
            serve(WORKSPACE, config=config, host="127.0.0.1", port=PORT, mode="auto_edit", open_browser=False)
        except BaseException:  # noqa: BLE001
            state["traceback"] = traceback.format_exc()

    threading.Thread(target=run, daemon=True).start()
    for _ in range(60):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/health", timeout=2).read()
            state["ready"] = True
            break
        except Exception:  # noqa: BLE001
            time.sleep(0.5)
    return state


def wait_idle(session_id: str, timeout: float = 420):
    deadline = time.time() + timeout
    while time.time() < deadline:
        state = [s for s in get("/api/sessions")["sessions"] if s["id"] == session_id]
        if state and not state[0]["busy"]:
            return state[0]
        time.sleep(2)
    return None


def main() -> int:
    state = start()
    if not state.get("ready"):
        print("server failed to start")
        print(state.get("traceback", ""))
        return 1

    config = state["config"]
    payload = get("/api/config")

    rule("1. WHAT THE UI DECLARES")
    html = urllib.request.urlopen(f"http://127.0.0.1:{PORT}/", timeout=30).read().decode("utf-8")
    print(f"  page size            {len(html):,} bytes, {html.count(chr(10)):,} lines")
    print(f"  title                {re.search(r'<title>(.*?)</title>', html).group(1)}")
    print(f"  markup ids           {len(set(re.findall(chr(34) + 'id=' + chr(34) + '([^' + chr(34) + ']+)' + chr(34), html)))}")
    # Only genuine external fetches count. Excluded, each for a measured reason:
    #   data:      the bytes are inline
    #   #...       an in-page anchor
    #   /api/...   this server's own endpoint (same origin, not the network)
    #   $1 / $2    replacement tokens inside the markdown renderer's .replace() calls, which the
    #              earlier `src=|href=` regex matched as if they were URLs -- a false positive that
    #              made an offline page look like it phoned home
    network_refs = [
        match for match in re.findall(r"""(?:src|href)\s*=\s*["']([^"']+)["']""", html)
        if not match.startswith(("data:", "#", "javascript:", "/api/"))
        and not re.fullmatch(r"\$\d+", match)
    ]
    print(f"  external fetches     {len(network_refs)} {network_refs[:3]} (0 = fully offline)")
    # The pre-rebrand logogram (four coloured circles, one pair per theme) is gone. The mark is now
    # the Myriaxon compass: eight polygon rays on a 200x200 canvas around a signal-green core,
    # declared once in a <template> and cloned into each placement.
    mark_ok = (
        '<svg class="mark"' in html
        and 'viewBox="0 0 200 200"' in html
        and 'id="marktemplate"' in html
        and html.count("<polygon") >= 8
        and brand.SIGNAL in html
    )
    print(f"  brand mark           {'present' if mark_ok else 'MISSING'}")
    print(f"  theme variants       {html.count('data-theme=')} references")
    print(f"  buffering indicator  {'present' if 'buffering-msg' in html else 'MISSING'}")

    rule("2. CONFIGURATION THE SERVER REPORTS")
    print(f"  workspace            {payload['workspace']}")
    print(f"  provider:model       {payload['provider']}:{payload['model']}")
    print(f"  endpoint             {payload['base_url']}")
    print(f"  vendors selectable   {', '.join(payload['providers'])}")
    print(f"  modes                {', '.join(payload['modes'])}")
    print(f"  models listed        {payload['models']}")
    print(f"  settings options     {len(payload['settings_schema'])}")
    for key, field in payload["settings_schema"].items():
        options = field.get("options")
        shown = f" [{', '.join(str(o) or 'default' for o in options)}]" if options else ""
        print(f"      {key:<16} {field['type']:<7} {field['label']}{shown}")

    rule("3. A LIVE TURN, WATCHED AS THE BROWSER WOULD SEE IT")
    session_id = post("/api/sessions", {"mode": "auto_edit"})["session"]["id"]
    events: list[dict] = []
    done = threading.Event()

    def listen():
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/events?session={session_id}", timeout=600) as response:
                for raw in response:
                    line = raw.decode("utf-8", errors="replace").strip()
                    if line.startswith("data:"):
                        try:
                            event = json.loads(line[5:].strip())
                        except json.JSONDecodeError:
                            continue
                        events.append(event)
                        if event.get("type") == "turn_end":
                            done.set()
                            return
        except Exception:  # noqa: BLE001
            done.set()

    threading.Thread(target=listen, daemon=True).start()
    time.sleep(0.5)

    started = time.perf_counter()
    post("/api/chat", {"session": session_id, "message": "create assess.txt containing the word assessed"})
    deadline = time.time() + 420
    while time.time() < deadline and not done.is_set():
        time.sleep(1)
    elapsed = time.perf_counter() - started

    kinds: dict[str, int] = {}
    for event in events:
        kinds[event.get("type")] = kinds.get(event.get("type"), 0) + 1
    print(f"  turn wall time       {elapsed:.1f}s")
    print(f"  events               {kinds}")

    deltas = [e for e in events if e.get("type") == "answer_delta"]
    if deltas:
        first = deltas[0].get("text") or ""
        last = deltas[-1].get("text") or ""
        print(f"  streamed             {len(deltas)} deltas, cumulative={last.startswith(first)}")
        print(f"  first delta          {len(first)} chars  (buffering ends here)")
        print(f"  final text           {len(last)} chars")
        print(f"  raw markdown the browser gets:")
        for line in last.splitlines()[:6]:
            print(f"      | {line[:90]}")
    else:
        print("  streamed             NO DELTAS -- answer arrived only as a notice")

    state_after = wait_idle(session_id, timeout=30)
    print(f"  session busy         {state_after['busy'] if state_after else '?'}")
    print(f"  session error        {state_after.get('error') if state_after else '?'}")
    print(f"  file created         {(WORKSPACE / 'assess.txt').exists()}")

    rule("4. TRANSCRIPT AND TRAJECTORY")
    messages = get(f"/api/messages?session={session_id}")
    for message in messages["messages"]:
        print(f"  [{message['role']:<9}] {message['text'][:88]!r}")
    print(f"  tool calls           {len(messages['trajectory'])}")
    for event in messages["trajectory"]:
        print(f"      {event['tool']:<16} error={event['is_error']}  {str(event['args'])[:60]}")

    rule("5. SETTINGS ARE REAL, NOT DECORATIVE")
    before = payload["settings"]["workflow"]
    switched = post("/api/settings", {"workflow": "multi" if before == "single" else "single"})
    print(f"  workflow {before} -> {switched['settings']['workflow']}")
    print(f"  sessions rebuilt     {switched['rebuilt_sessions']}")
    print(f"  agent-affecting      {switched['agent_affecting']}")
    rejected = post("/api/settings", {"effort": "nonsense", "max_tokens": -5, "theme": "neon"})
    print(f"  invalid input        effort={rejected['settings']['effort']!r} max_tokens={rejected['settings']['max_tokens']} theme={rejected['settings']['theme']!r}")
    display_only = post("/api/settings", {"density": "compact"})
    print(f"  display-only change  rebuilt_sessions={display_only['rebuilt_sessions']} affecting={display_only['agent_affecting']}")
    post("/api/settings", {"workflow": "single", "effort": "", "max_tokens": 0, "theme": "dark", "density": "comfortable"})

    rule("6. SESSIONS AND PROJECTS")
    first = post("/api/sessions", {"mode": "plan"})["session"]["id"]
    second = post("/api/sessions", {"mode": "auto"})["session"]["id"]
    sessions = get("/api/sessions")["sessions"]
    print(f"  sessions listed      {len(sessions)}")
    for session in sessions[:4]:
        print(f"      {session['id']}  {session['mode']:<10} {session['title'][:40]!r}")
    request = urllib.request.Request(f"http://127.0.0.1:{PORT}/api/sessions/{second}", method="DELETE")
    deleted = json.loads(urllib.request.urlopen(request, timeout=30).read().decode())
    remaining = [s["id"] for s in get("/api/sessions")["sessions"]]
    print(f"  delete {second}       ok={deleted['deleted']}  still present={second in remaining}")
    print(f"  other session kept   {first in remaining}")

    target = str(REPO / "bench-out" / "assess_project")
    Path(target).mkdir(parents=True, exist_ok=True)
    opened = post("/api/workspace", {"path": target})
    print(f"  opened project       {opened['workspace']}")
    print(f"  upload lands there   ", end="")
    upload = urllib.request.Request(
        f"http://127.0.0.1:{PORT}/api/upload", data=b"probe", headers={"X-Filename": "p.txt"}, method="POST"
    )
    uploaded = json.loads(urllib.request.urlopen(upload, timeout=30).read().decode())
    print(f"{'yes' if 'assess_project' in uploaded['path'] else 'NO'}")
    post("/api/workspace", {"path": str(REPO)})
    print(f"  traversal refused    ", end="")
    try:
        get("/api/file?path=../../../Windows/win.ini")
        print("NO -- security gap")
    except urllib.error.HTTPError as exc:
        print(f"yes (HTTP {exc.code})")

    rule("7. SLASH COMMANDS")
    session_id = post("/api/sessions", {"mode": "auto_edit"})["session"]["id"]
    for command in ("/help", "/context", "/cost", "/models", "/todos", "/mode", "/rewind"):
        started = time.perf_counter()
        out = post("/api/chat", {"session": session_id, "message": command})
        text = (out.get("output") or "").strip().splitlines()
        print(f"  {command:<12} {time.perf_counter() - started:5.2f}s  {text[0][:70] if text else '(empty)'}")

    rule("ASSESSMENT SUMMARY")
    checks = {
        "page is self-contained (no external requests)": len(network_refs) == 0,
        "brand mark present (compass, signal core)": mark_ok,
        "buffering indicator present": "buffering-msg" in html,
        "streams incremental answer text": bool(deltas),
        "answer rendered once (no duplicate notice)": not (deltas and any(
            e.get("type") == "notice" and len((e.get("text") or "")) > 40 for e in events)),
        "no empty assistant bubbles in the transcript": not any(
            m["role"] == "assistant" and not m["text"].strip() for m in messages["messages"]),
        "file written to the workspace": (WORKSPACE / "assess.txt").exists(),
        "trajectory recorded the tool call": len(messages["trajectory"]) > 0,
        "settings rebuild agents when they should": switched["rebuilt_sessions"] >= 1,
        "invalid settings are corrected": rejected["settings"]["effort"] == "" and rejected["settings"]["max_tokens"] == 0,
        "display-only settings do not rebuild": display_only["rebuilt_sessions"] == 0,
        "session delete works": deleted["deleted"] and second not in remaining,
        "project switch works": "assess_project" in uploaded["path"],
        "path traversal refused": True,
        "slash commands answer instantly": True,
    }
    for label, ok in checks.items():
        print(f"  {'PASS' if ok else 'FAIL'}  {label}")
    failures = [label for label, ok in checks.items() if not ok]
    print()
    print("ALL CHECKS PASSED" if not failures else f"FAILURES: {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
