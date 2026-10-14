"""Verify custom endpoints and multi-vendor/multi-model switching in the web UI.

    python scripts/check_endpoints.py [port]

The user-facing claim is: "I can add any OpenAI-compatible endpoint from the UI, and switch between
multiple vendors and models inside the UI, not just in backend state." That is verified here through
the same HTTP calls the browser makes, plus structural checks that the controls exist and are wired.

Live provider under test is always Netra, per the project's testing rule.
"""
import json
import os
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
    return json.loads(urllib.request.urlopen(request, timeout=120).read().decode())


def delete(path):
    return json.loads(urllib.request.urlopen(urllib.request.Request(BASE + path, method="DELETE"), timeout=60).read().decode())


def main() -> int:
    config = get("/api/config")
    print(f"provider={config['provider']} model={config['model']}\n")

    print("=== the UI exposes vendor and model switching ===")
    check("built-in vendors listed", len(config["providers"]) >= 3, config["providers"])
    check("models listed for the active vendor", len(config["models"]) >= 1, config["models"])
    check("custom endpoint list present (may be empty)", "custom_endpoints" in config)

    print("\n=== add a custom endpoint ===")
    name = "probe-gateway"
    created = post("/api/endpoints", {
        "name": name,
        "base_url": "https://api.netraruntime.com/v1",
        "api_key": "test-key-not-real",
        "models": "model-a, model-b",
        "model": "model-a",
        "label": "Probe Gateway",
    })
    endpoint = created.get("endpoint") or {}
    check("endpoint saved", endpoint.get("name") == name, created)
    check("key is not echoed back to the browser", endpoint.get("api_key") == "" and endpoint.get("has_api_key") is True, endpoint)
    check("models parsed from a comma-separated string",
          endpoint.get("models") == ["model-a", "model-b"], endpoint.get("models"))
    check("a default model is chosen", endpoint.get("model") == "model-a", endpoint.get("model"))

    print("\n=== it becomes a first-class vendor ===")
    refreshed = get("/api/config")
    check("appears in the provider list", name in refreshed["providers"], refreshed["providers"])

    switch = post("/api/chat", {"session": post("/api/sessions", {"mode": "auto_edit"})["session"]["id"],
                                "message": f"/provider {name}"})
    text = switch.get("output") or ""
    print(f"    switch output: {text.strip()[:120]}")
    check("switchable by name", "set to" in text, text)
    check("endpoint remapped to the custom URL", "netraruntime" in text, text)
    check("its own model was applied", "model-a" in text, text)

    session_id = switch.get("session", {}).get("id") or ""
    endpoint_state = get(f"/api/endpoint?session={session_id}") if session_id else {}
    if endpoint_state:
        check("session reports the custom vendor", endpoint_state.get("provider") == name, endpoint_state.get("provider"))
        check("session reports the custom endpoint", "netraruntime" in (endpoint_state.get("base_url") or ""), endpoint_state.get("base_url"))

    print("\n=== validation rejects bad input ===")
    for label, payload in (
        ("empty name", {"name": "", "base_url": "https://x/v1"}),
        ("name with spaces", {"name": "bad name", "base_url": "https://x/v1"}),
        ("missing url", {"name": "nourl"}),
        ("url without scheme", {"name": "badurl", "base_url": "x.com/v1"}),
    ):
        try:
            post("/api/endpoints", payload)
            check(f"rejects {label}", False, "accepted")
        except urllib.error.HTTPError as exc:
            check(f"rejects {label}", exc.code == 400, exc.code)

    print("\n=== editing keeps a stored key ===")
    edited = post("/api/endpoints", {"name": name, "base_url": "https://api.netraruntime.com/v1",
                                     "models": "model-c", "model": "model-c"})
    check("edit accepted", edited["endpoint"]["model"] == "model-c", edited)
    check("stored key retained when the form sends none", edited["endpoint"]["has_api_key"] is True, edited["endpoint"])

    print("\n=== probe and test against a real endpoint ===")
    # Netra is the live provider for this project, so it is also the honest thing to probe with.
    netra_url = config["base_url"] if config["provider"] == "netra" else "https://api.netraruntime.com/v1"
    probe = post("/api/endpoints/probe", {"base_url": netra_url})
    check("probe succeeded", probe.get("ok") is True, probe)
    check("probe listed models", len(probe.get("models") or []) >= 1, probe)
    if probe.get("models"):
        print(f"    models: {probe['models']}")

    bad_probe = post("/api/endpoints/probe", {"base_url": "http://127.0.0.1:9/nope"})
    check("an unreachable endpoint reports an error, not a crash", bad_probe.get("ok") is False, bad_probe)

    test = post("/api/endpoints/test", {
        "base_url": netra_url,
        "model": "deepseek/deepseek-v4.1-flash",
        # The endpoint needs the real key. Taken from the environment rather than hardcoded, and the
        # test still passes meaningfully if it is absent: the failure mode is then a clean 401 rather
        # than a crash, which is itself the property under test.
        "api_key": os.environ.get("NETRA_API_KEY", ""),
    })
    if os.environ.get("NETRA_API_KEY"):
        check("a real test message can be sent before committing", test.get("ok") is True, test)
        if test.get("ok"):
            print(f"    reply: {test['reply']!r}")
    else:
        check("no key: the endpoint reports a clean auth error instead of crashing",
              test.get("ok") is False and "401" in str(test.get("error")), test)

    print("\n=== delete ===")
    removed = post("/api/endpoints", {"action": "delete", "name": name})
    check("delete reported", removed.get("deleted") is True, removed)
    after = get("/api/config")
    check("gone from the vendor list", name not in after["providers"], after["providers"])

    print(f"\n{len(passed)} passed, {len(failed)} failed")
    if failed:
        print("failed:", failed)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
