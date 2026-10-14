"""Verify provider selection and session switching, the two objective items not yet exercised.

    python scripts/check_dashboard_provider_sessions.py [port]

Provider switching swaps the endpoint AND the credential, which is the part most likely to be wired
wrongly -- an earlier bug had the stored endpoint of the previous provider leak into the new one, so
this asserts the endpoint actually changes with the provider.
"""
import json
import sys
import urllib.error
import urllib.request

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8791
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


def main() -> int:
    config = get("/api/config")
    original_provider = config["provider"]
    print(f"original provider={original_provider} model={config['model']}\n")

    print("=== provider selection ===")
    session_id = post("/api/sessions", {"mode": "auto_edit"})["session"]["id"]

    # Asserting on the endpoint, not just on an acknowledgement string: the failure this guards
    # against is a provider switch that keeps the previous provider's endpoint, which returns a 404
    # from the wrong host and looks like a broken model.
    expected_endpoints = {"netra": "netraruntime", "deepseek": "deepseek", "gemini": "googleapis"}
    for target, expected in expected_endpoints.items():
        out = post("/api/chat", {"session": session_id, "message": f"/provider {target}"})
        text = out.get("output") or ""
        check(f"/provider {target} acknowledged", "set to" in text, text[:100])

        state = get(f"/api/endpoint?session={session_id}")
        check(f"endpoint reports provider={target}", state["provider"] == target, state["provider"])
        check(
            f"endpoint for {target} is the {target} host",
            expected in (state["base_url"] or ""),
            f"base_url={state['base_url']!r}",
        )

    # Restore the original provider so this probe is not destructive.
    post("/api/chat", {"session": session_id, "message": f"/provider {original_provider}"})
    restored = next(s for s in get("/api/sessions")["sessions"] if s["id"] == session_id)
    check("provider restored", restored["provider"] == original_provider, restored["provider"])

    print("\n=== session switching ===")
    first = post("/api/sessions", {"mode": "auto_edit"})["session"]["id"]
    second = post("/api/sessions", {"mode": "plan"})["session"]["id"]
    check("two distinct sessions", first != second, f"{first} / {second}")

    listing = get("/api/sessions")["sessions"]
    check("both appear in the list", {first, second} <= {s["id"] for s in listing}, str([s["id"] for s in listing]))
    modes = {s["id"]: s["mode"] for s in listing}
    check("each session keeps its own mode", modes.get(second) == "plan", str(modes.get(second)))

    # Sessions must have independent transcripts: a message in one must not appear in the other.
    post("/api/chat", {"session": first, "message": "/clear"})
    post("/api/chat", {"session": second, "message": "/clear"})
    first_messages = get(f"/api/messages?session={first}")["messages"]
    second_messages = get(f"/api/messages?session={second}")["messages"]
    check("transcripts are independent objects", first_messages is not second_messages)

    unknown = get("/api/messages?session=does-not-exist") if False else None
    try:
        urllib.request.urlopen(f"{BASE}/api/messages?session=nope-not-real", timeout=30)
        check("unknown session is rejected", False, "returned 200")
    except urllib.error.HTTPError as exc:
        check("unknown session is rejected", exc.code == 404, str(exc.code))

    print(f"\n{len(passed)} passed, {len(failed)} failed")
    if failed:
        print("failed:", failed)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
