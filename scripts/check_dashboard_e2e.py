"""End-to-end check of the running Myriaxon dashboard over HTTP.

    python scripts/check_dashboard_e2e.py [port]

Exercises the same calls the browser makes: config, session creation, a chat turn, the SSE event
stream, model/mode switching, upload, and file browsing. Asserts on observable effects (the file
exists on disk, the stream delivered tool_call events) rather than on the HTTP status alone.
"""
import json
import sys
import threading
import time
import urllib.error
import urllib.request

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8791
BASE = f"http://127.0.0.1:{PORT}"

passed, failed = [], []


def check(label, condition, detail=""):
    (passed if condition else failed).append(label)
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}{(' -- ' + detail) if detail and not condition else ''}")


def get(path):
    return json.loads(urllib.request.urlopen(BASE + path, timeout=30).read().decode())


def post(path, body):
    request = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}, method="POST"
    )
    return json.loads(urllib.request.urlopen(request, timeout=30).read().decode())


def main() -> int:
    print("=== config ===")
    config = get("/api/config")
    check("config reports a provider", bool(config.get("provider")), str(config.get("provider")))
    check("config lists all four modes", set(config.get("modes", [])) == {"manual", "auto_edit", "plan", "auto"},
          str(config.get("modes")))
    check("config lists at least one model", len(config.get("models", [])) >= 1, str(config.get("models")))
    print(f"    provider={config.get('provider')} model={config.get('model')} workspace={config.get('workspace')}")

    print("\n=== sessions ===")
    created = post("/api/sessions", {"mode": "auto_edit"})
    session_id = created["session"]["id"]
    check("session created", bool(session_id), session_id)
    check("session lists", any(s["id"] == session_id for s in get("/api/sessions")["sessions"]))

    print("\n=== sse stream (subscribed before the turn) ===")
    events = []
    stop = threading.Event()

    def listen():
        try:
            with urllib.request.urlopen(f"{BASE}/api/events?session={session_id}", timeout=180) as response:
                for raw in response:
                    if stop.is_set():
                        return
                    line = raw.decode("utf-8", errors="replace").strip()
                    if line.startswith("data:"):
                        try:
                            events.append(json.loads(line[5:].strip()))
                        except json.JSONDecodeError:
                            pass
                    if any(e.get("type") == "turn_end" for e in events):
                        return
        except Exception:  # noqa: BLE001 -- the assertion below reports the outcome
            return

    listener = threading.Thread(target=listen, daemon=True)
    listener.start()
    time.sleep(0.5)

    print("\n=== chat turn ===")
    accepted = post(
        "/api/chat",
        {"session": session_id, "message": "create e2e_probe.txt containing the word verified", "mode": "auto_edit"},
    )
    check("turn accepted", accepted.get("accepted") is True, str(accepted))

    deadline = time.time() + 240
    while time.time() < deadline:
        state = [s for s in get("/api/sessions")["sessions"] if s["id"] == session_id]
        if state and not state[0]["busy"]:
            break
        time.sleep(2)

    listener.join(timeout=5)
    stop.set()

    kinds = {}
    for event in events:
        kinds[event.get("type")] = kinds.get(event.get("type"), 0) + 1
    print(f"    events: {kinds}")
    check("stream delivered turn_start", "turn_start" in kinds)
    check("stream delivered tool_call", "tool_call" in kinds)
    check("stream delivered turn_end", "turn_end" in kinds)
    # The assistant's text arrives either as streamed deltas (every provider that streams) or as one
    # notice. Requiring `notice` specifically failed on Netra, which streams properly -- the
    # assertion was describing the old behaviour rather than the contract.
    check("stream delivered the assistant's text", "answer_delta" in kinds or "notice" in kinds, kinds)
    check("no error event", "error" not in kinds,
          next((e.get("message") for e in events if e.get("type") == "error"), ""))

    print("\n=== transcript ===")
    payload = get(f"/api/messages?session={session_id}")
    roles = [m["role"] for m in payload["messages"]]
    check("transcript has a user message", "user" in roles, str(roles))
    check("transcript has an assistant message", "assistant" in roles, str(roles))
    check("first message is the user's", roles[:1] == ["user"], str(roles))
    check("trajectory recorded a write", any(t["tool"] == "write_file" for t in payload["trajectory"]),
          str([t["tool"] for t in payload["trajectory"]]))

    print("\n=== files (workspace confinement) ===")
    files = get("/api/files?path=")
    names = [f["name"] for f in files.get("files", [])]
    check("probe file is listed in the workspace", "e2e_probe.txt" in names, str(names))
    try:
        get("/api/file?path=../../../etc/hosts")
        check("path traversal is refused", False, "read outside the workspace succeeded")
    except urllib.error.HTTPError as exc:
        check("path traversal is refused", exc.code in (403, 404, 500), str(exc.code))

    print("\n=== upload ===")
    request = urllib.request.Request(
        BASE + "/api/upload",
        data=b"hello from the e2e check",
        headers={"X-Filename": "e2e_upload.txt"},
        method="POST",
    )
    upload = json.loads(urllib.request.urlopen(request, timeout=30).read().decode())
    check("upload accepted", bool(upload.get("path")), str(upload))
    check("upload not marked as an image", upload.get("is_image") is False)

    print("\n=== mode + model switching ===")
    check("mode switch to plan", post("/api/mode", {"session": session_id, "mode": "plan"}).get("mode") == "plan")
    check("invalid mode rejected", _rejects(lambda: post("/api/mode", {"session": session_id, "mode": "nope"})))
    model = config["model"]
    check("model switch", post("/api/model", {"session": session_id, "model": model}).get("model") == model)

    print("\n=== health ===")
    health = get("/api/health")
    check("health ok", health.get("ok") is True)

    print(f"\n{len(passed)} passed, {len(failed)} failed")
    if failed:
        print("failed:", failed)
        return 1
    return 0


def _rejects(call) -> bool:
    try:
        call()
        return False
    except urllib.error.HTTPError:
        return True
    except Exception:  # noqa: BLE001
        return True


if __name__ == "__main__":
    raise SystemExit(main())
