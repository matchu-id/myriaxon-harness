"""Verify dashboard attachments reach the model, and that slash commands work over HTTP.

    python scripts/check_dashboard_features.py [port] [image-path]

Two things the browser relies on that the earlier end-to-end check did not cover:
  * an uploaded image actually being seen by the model (a text-only fallback would still "succeed")
  * slash commands executing immediately without a model turn
"""
import json
import sys
import time
import urllib.error
import urllib.request

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8791
IMAGE = sys.argv[2] if len(sys.argv) > 2 else ""
BASE = f"http://127.0.0.1:{PORT}"

passed, failed = [], []


def check(label, condition, detail=""):
    (passed if condition else failed).append(label)
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}{(' -- ' + str(detail)) if detail and not condition else ''}")


def get(path):
    return json.loads(urllib.request.urlopen(BASE + path, timeout=60).read().decode())


def post(path, body):
    request = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}, method="POST"
    )
    return json.loads(urllib.request.urlopen(request, timeout=60).read().decode())


def wait_idle(session_id, timeout=420):
    deadline = time.time() + timeout
    while time.time() < deadline:
        state = [s for s in get("/api/sessions")["sessions"] if s["id"] == session_id]
        if state and not state[0]["busy"]:
            return state[0]
        time.sleep(2)
    return None


def main() -> int:
    config = get("/api/config")
    print(f"provider={config['provider']} model={config['model']}\n")

    session_id = post("/api/sessions", {"mode": "auto_edit"})["session"]["id"]

    # ---- slash commands must not start a model turn -------------------------------------------
    print("=== slash commands ===")
    started = time.time()
    result = post("/api/chat", {"session": session_id, "message": "/help"})
    elapsed = time.time() - started
    check("command accepted", result.get("accepted") is True, result)
    check("command flagged as a command", result.get("command") is True, result)
    check("command listed the available commands", "/context" in (result.get("output") or ""), result.get("output"))
    check(f"command returned immediately ({elapsed:.2f}s, no model turn)", elapsed < 20, f"{elapsed:.1f}s")
    check("session stayed idle", wait_idle(session_id, timeout=15) is not None)

    for command, needle in (
        ("/context", "Context"),
        ("/cost", "") ,
        ("/models", "model"),
        ("/todos", "todo"),
        ("/mode", "mode is"),
        ("/thinking", "reasoning display"),
        ("/yolo", "approval"),
        ("/bogus-command", "unknown command"),
    ):
        out = post("/api/chat", {"session": session_id, "message": command})
        text = (out.get("output") or "").lower()
        check(f"{command} responds", bool(text), out.get("output"))
        if needle:
            check(f"{command} mentions {needle!r}", needle.lower() in text, out.get("output", "")[:120])

    check("cli-only command explains itself", "needs the CLI" in (post("/api/chat", {"session": session_id, "message": "/rewind"}).get("output") or ""))

    # ---- attachment reaches the model ---------------------------------------------------------
    if IMAGE:
        print("\n=== image attachment ===")
        with open(IMAGE, "rb") as handle:
            data = handle.read()
        request = urllib.request.Request(
            BASE + "/api/upload",
            data=data,
            headers={"X-Filename": "vision_probe.png"},
            method="POST",
        )
        upload = json.loads(urllib.request.urlopen(request, timeout=120).read().decode())
        check("upload stored", bool(upload.get("path")), upload)
        check("recognised as an image", upload.get("is_image") is True, upload)

        post(
            "/api/chat",
            {
                "session": session_id,
                "message": "In one sentence, what is the title of this image?",
                "attachments": [upload["path"]],
            },
        )
        state = wait_idle(session_id)
        check("turn finished", state is not None and not state.get("error"), state.get("error") if state else "timeout")

        messages = get(f"/api/messages?session={session_id}")["messages"]
        assistant = [m for m in messages if m["role"] == "assistant"]
        answer = assistant[-1]["text"] if assistant else ""
        print(f"    answer: {answer[:200]!r}")
        check("model answered", bool(answer.strip()))
        lowered = answer.lower()
        check(
            "model did not claim it cannot see the image",
            not any(p in lowered for p in ("don't see", "do not see", "no image", "can't see", "cannot see", "unable to view")),
            answer[:150],
        )
        check("answer mentions governance (the image's subject)", "governance" in lowered, answer[:150])

    print(f"\n{len(passed)} passed, {len(failed)} failed")
    if failed:
        print("failed:", failed)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
