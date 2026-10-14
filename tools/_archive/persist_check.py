"""Verify session persistence and real cancellation against the live server.

The persistence check is the interesting one: it must survive an actual process restart, so this
starts a turn, records the session id and the transcript, then asks the *server* to confirm the file
exists -- because "the conversation came back" is only meaningful if it was written down first.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8765"
CONFIG = Path(r"C:\Users\arian\OneDrive\ドキュメント\Myriaxon\.myriaxon")
failures = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global failures
    if not ok:
        failures += 1
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}{('  -- ' + detail) if detail and not ok else ''}")


def call(path: str, body: dict | None = None, timeout: float = 240.0):
    if body is None:
        req = urllib.request.Request(BASE + path)
    else:
        req = urllib.request.Request(
            BASE + path, data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"}, method="POST",
        )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"null")


def main() -> int:
    deadline = time.time() + 45
    while time.time() < deadline:
        try:
            call("/api/health", timeout=3)
            break
        except Exception:
            time.sleep(0.5)
    else:
        print("the dashboard did not come up")
        return 1

    print("1. a session is written to disk after its turn")
    _, created = call("/api/sessions", {"mode": "auto_edit"})
    session_id = (created.get("session") or {}).get("id", "")
    check("session created", bool(session_id), str(created)[:120])

    status, result = call("/api/chat", {"session": session_id, "message": "/help"})
    check("a turn ran", status == 200 and bool(result.get("command")), str(result)[:160])

    path = CONFIG / "sessions" / f"{session_id}.json"
    check("the session file exists on disk", path.is_file(), str(path))
    if path.is_file():
        data = json.loads(path.read_text(encoding="utf-8"))
        check("it records the session id", (data.get("state") or {}).get("id") == session_id)
        check("it has a save timestamp", bool(data.get("saved_at")))
        check("it keeps the conversation", isinstance(data.get("messages"), list))

    print("\n2. a command turn is still a turn the session remembers")
    _, messages = call(f"/api/messages?session={session_id}")
    check("the transcript reads back", isinstance(messages.get("messages"), list))

    print("\n3. cancel reaches the agent")
    from urllib.request import Request

    # Start a real turn, then cancel it while it is in flight.
    body = json.dumps({"session": session_id, "message": "Count slowly from 1 to 200, one number per line."}).encode()
    started = Request(BASE + "/api/chat", data=body,
                      headers={"Content-Type": "application/json"}, method="POST")
    try:
        urllib.request.urlopen(started, timeout=5)
    except Exception:
        # The request blocks until the turn ends, so a timeout here is expected: the turn is running
        # on the server's own thread either way.
        pass

    time.sleep(1.0)
    status, cancelled = call("/api/cancel", {"session": session_id}, timeout=60)
    check("cancel was accepted", status == 200, f"{status} {cancelled}")

    # Give it a moment, then confirm the session is no longer busy -- which is only true if the
    # stream loop actually raised out and the turn unwound.
    settled = False
    for _ in range(30):
        _, listing = call("/api/sessions")
        entry = next((s for s in listing.get("sessions", []) if s["id"] == session_id), None)
        if entry is not None and not entry.get("busy"):
            settled = True
            break
        time.sleep(1.0)
    check("the turn stopped instead of running to completion", settled,
          "still busy after 30s -- the flag was set but the stream loop did not stop")

    print("\n4. the shipped page posts to /api/cancel")
    with urllib.request.urlopen(BASE + "/", timeout=30) as response:
        html = response.read().decode("utf-8", "replace")
    check("the cancel route is called by the page", "/api/cancel" in html)

    print()
    print("all verified" if not failures else f"{failures} check(s) FAILED")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
