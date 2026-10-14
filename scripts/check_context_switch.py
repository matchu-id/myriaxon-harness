"""Does switching vendor mid-conversation keep the context?

    python scripts/check_context_switch.py

The claim to verify: tell the agent a fact on one vendor, switch vendor, ask for the fact back. A
switch that silently drops the transcript produces a fluent-sounding "I don't know", which looks like
a model limitation rather than a bug -- so the check is behavioural, not structural.
"""
import json
import sys
import time
import urllib.request

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
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
    return json.loads(urllib.request.urlopen(request, timeout=120).read().decode())


def wait_idle(session_id, timeout=420):
    deadline = time.time() + timeout
    while time.time() < deadline:
        state = [s for s in get("/api/sessions")["sessions"] if s["id"] == session_id]
        if state and not state[0]["busy"]:
            return state[0]
        time.sleep(2)
    return None


def ask(session_id, message):
    post("/api/chat", {"session": session_id, "message": message})
    state = wait_idle(session_id)
    messages = get(f"/api/messages?session={session_id}")["messages"]
    assistant = [m for m in messages if m["role"] == "assistant"]
    return (assistant[-1]["text"] if assistant else ""), state


def main() -> int:
    config = get("/api/config")
    providers = config.get("providers", [])
    print(f"start provider={config['provider']}  available={providers}\n")
    check("provider list is exposed to the UI", len(providers) >= 3, providers)

    session_id = post("/api/sessions", {"mode": "auto_edit"})["session"]["id"]

    print("=== establish a fact on the starting vendor ===")
    answer, state = ask(
        session_id,
        "Remember this exact code word for later: ZEPHYR-42. Just reply 'noted'.",
    )
    check("fact acknowledged", "noted" in answer.lower() or "zephyr" in answer.lower(), answer[:120])
    check("no error on the first turn", not (state or {}).get("error"), (state or {}).get("error"))

    history_after_first = post("/api/chat", {"session": session_id, "message": "/cost"})
    check("/cost works after a turn", bool(history_after_first.get("output")))

    print("\n=== switch vendor, then ask for the fact back ===")
    # netra or deepseek, whichever is not the current provider
    target = next((p for p in providers if p in ("netra", "deepseek") and p != config["provider"]), None)
    if target is None:
        print("no alternate vendor available to switch to")
        return 2

    switch = post("/api/chat", {"session": session_id, "message": f"/provider {target}"})
    print(f"    switch output: {(switch.get('output') or '').strip()[:160]}")
    check("switch reported context carried", "carried over" in (switch.get("output") or ""), switch.get("output"))

    endpoint = get(f"/api/endpoint?session={session_id}")
    check(f"endpoint now {target}", endpoint["provider"] == target, endpoint)

    answer, state = ask(session_id, "What was the code word I asked you to remember? Reply with just the code word.")
    print(f"    answer: {answer[:160]!r}")
    check("no error after the switch", not (state or {}).get("error"), (state or {}).get("error"))
    # A vendor that cannot reach its backend returns an error rather than an answer, which is a
    # different failure from losing the thread -- report it as such.
    if (state or {}).get("error"):
        print("    (alternate vendor unreachable; context retention could not be confirmed end to end)")
    else:
        check("the code word survived the vendor switch", "ZEPHYR" in answer.upper(), answer[:160])

    print("\n=== model switch within the same session ===")
    model_state = post("/api/model", {"session": session_id, "model": endpoint["model"]})
    check("model switch reports kept history", "kept_history" in model_state, model_state)

    print(f"\n{len(passed)} passed, {len(failed)} failed")
    if failed:
        print("failed:", failed)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
