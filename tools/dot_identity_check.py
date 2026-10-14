"""Ask a dot whether it is that dot, against the live server. The defect, end to end.

The report was: `/dots run analyst "are you analyst?"` answered "Not by default -- I'm a coding
agent". The briefing was being sent, but as a user message, while the system prompt still asserted the
generic Myriaxon identity with `essential=True`. This runs the real thing and reads the real answer.
"""

from __future__ import annotations

import json
import re
import sys
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8765"
failures = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global failures
    if not ok:
        failures += 1
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}{('  -- ' + detail) if detail and not ok else ''}")


def call(path: str, body: dict | None = None, timeout: float = 300.0):
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
    name = sys.argv[1] if len(sys.argv) > 1 else "analyst"

    deadline = time.time() + 45
    while time.time() < deadline:
        try:
            call("/api/health", timeout=3)
            break
        except Exception:
            time.sleep(0.5)
    else:
        print("the dashboard is not up")
        return 1

    print(f"1. {name} must be enabled")
    status, listing = call("/api/dots")
    dot = next((d for d in (listing.get("dots") or []) if d["name"] == name), None)
    if dot is None:
        print(f"  no dot named {name}")
        return 1
    if not dot.get("enabled"):
        call("/api/dots", {"action": "toggle", "name": name, "enabled": True})
        print("     (enabled it)")

    print(f"\n2. ask {name} who it is")
    status, payload = call("/api/dots", {
        "action": "run", "dot": name,
        "message": "In one short sentence: are you the analyst dot, or are you Myriaxon the coding agent?",
    })
    if not payload.get("accepted"):
        print(f"  run refused: {payload.get('reason')}")
        return 1
    session_id = payload["session"]["id"]
    print(f"     session {session_id}")

    print("\n3. wait for the answer")
    answer = ""
    for _ in range(120):
        _, messages = call(f"/api/messages?session={session_id}")
        rows = messages.get("messages") or []
        if rows and rows[-1].get("role") == "assistant":
            answer = rows[-1].get("text") or ""
            break
        time.sleep(1)
    check("the dot answered", bool(answer), "timed out")
    if not answer:
        return 1
    print(f"\n     {name} said: {answer.strip()[:400]}\n")

    lowered = answer.lower()
    # The defect: it identified as the generic agent. Assert it does not, and does identify as itself.
    denies_being_dot = re.search(r"\b(i am|i'm)\s+(not\s+)?(myriaxon|a coding agent)\b", lowered)
    check("does not identify as the coding agent",
          not (denies_being_dot and "not" not in denies_being_dot.group(0)),
          answer[:200])
    check("identifies as the dot", name.lower() in lowered or "dot" in lowered, answer[:200])

    print("\n4. the transcript is labelled with the dot's name")
    _, messages = call(f"/api/messages?session={session_id}")
    check("the session records which dot owns it",
          (messages.get("state") or {}).get("dot") == name,
          str((messages.get("state") or {}).get("dot")))
    check("the title names the dot", name in ((messages.get("state") or {}).get("title") or ""))

    print("\n5. the turn text is the request, not the briefing")
    rows = messages.get("messages") or []
    first_user = next((m.get("text") or "" for m in rows if m.get("role") == "user"), "")
    check("the owner's message is framed as one", "Message from your owner" in first_user,
          first_user[:160])
    check("the briefing is not in the message",
          "persistent Myriaxon agent" not in first_user, first_user[:160])

    print()
    print("dot identity verified" if not failures else f"{failures} check(s) FAILED")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
