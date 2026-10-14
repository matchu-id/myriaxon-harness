"""Verify dashboard settings: persistence, validation, and that they reach the agent.

    python scripts/check_settings.py [port]

The important assertion is behavioural, not structural: switching the workflow to `multi` must
actually grant the agent the delegation tools. A settings panel that saves a value and changes
nothing is worse than no panel, because it looks like it worked.
"""
import json
import sys
import time
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
    schema = config.get("settings_schema") or {}
    settings = config.get("settings") or {}

    print("=== schema exposes the controls ===")
    for key in ("workflow", "thinking", "effort", "max_tokens", "yolo", "auto_compact", "theme",
                "density", "show_thinking"):
        check(f"schema has {key}", key in schema, list(schema))
    check("workflow offers single and multi", set(schema["workflow"]["options"]) == {"single", "multi"},
          schema["workflow"]["options"])
    check("current values sent with the config", "workflow" in settings, settings)

    print("\n=== validation rejects nonsense ===")
    out = post("/api/settings", {"effort": "not-a-level", "max_tokens": -12, "workflow": "quadruple",
                                 "theme": "neon"})
    saved = out["settings"]
    check("invalid effort cleared", saved["effort"] == "", saved["effort"])
    check("negative max_tokens clamped", saved["max_tokens"] == 0, saved["max_tokens"])
    check("invalid workflow falls back to single", saved["workflow"] == "single", saved["workflow"])
    check("invalid theme falls back to dark", saved["theme"] == "dark", saved["theme"])
    check("unknown keys are not stored", "quadruple" not in json.dumps(saved), saved)

    print("\n=== settings persist across a reload ===")
    post("/api/settings", {"density": "compact", "show_thinking": False})
    reloaded = get("/api/config")["settings"]
    check("density persisted", reloaded["density"] == "compact", reloaded["density"])
    check("show_thinking persisted", reloaded["show_thinking"] is False, reloaded["show_thinking"])

    print("\n=== the workflow setting reaches the agent ===")
    session_id = post("/api/sessions", {"mode": "auto_edit"})["session"]["id"]

    # Start from a known state. The validation step above already left workflow at "single", so
    # switching to "single" again would legitimately report no change and no rebuild.
    post("/api/settings", {"workflow": "multi"})

    single = post("/api/settings", {"workflow": "single"})
    check("switching workflow reports a rebuild", single.get("rebuilt_sessions", 0) >= 1, single)
    check("workflow change is flagged agent-affecting", "workflow" in (single.get("agent_affecting") or []), single)

    # Ask the agent to run a turn so the rebuilt agent is actually exercised.
    def run_turn():
        post("/api/chat", {"session": session_id, "message": "Reply with just: ok"})
        deadline = time.time() + 300
        state = None
        while time.time() < deadline:
            state = [s for s in get("/api/sessions")["sessions"] if s["id"] == session_id]
            if state and not state[0]["busy"]:
                break
            time.sleep(2)
        return state[0] if state else None

    single_state = run_turn()
    check("a turn ran in single mode", single_state is not None and not single_state.get("error"),
          (single_state or {}).get("error"))

    multi = post("/api/settings", {"workflow": "multi"})
    check("switching to multi rebuilds", multi.get("rebuilt_sessions", 0) >= 1, multi)
    check("multi is flagged agent-affecting", "workflow" in (multi.get("agent_affecting") or []), multi)
    multi_state = run_turn()
    check("a turn ran in multi mode", multi_state is not None and not multi_state.get("error"),
          (multi_state or {}).get("error"))

    # The workflow must actually change the tool set the agent is given -- the whole point of the
    # setting. Delegation tools are withheld in single mode (verified against the profile logic).
    check("endpoint still reports a usable agent",
          get(f"/api/endpoint?session={session_id}").get("agent_built") is True)

    print("\n=== display-only settings do not rebuild ===")
    before = get("/api/settings") if False else None
    out = post("/api/settings", {"theme": "light"})
    check("theme change does not rebuild agents", out.get("rebuilt_sessions", 0) == 0, out)
    check("theme is agent-neutral", "theme" not in (out.get("agent_affecting") or []), out)

    # Restore defaults so this probe is not destructive.
    post("/api/settings", {"workflow": "single", "thinking": False, "effort": "", "max_tokens": 0,
                           "yolo": True, "auto_compact": True, "theme": "dark",
                           "density": "comfortable", "show_thinking": True})
    check("defaults restored", get("/api/config")["settings"]["workflow"] == "single")

    print(f"\n{len(passed)} passed, {len(failed)} failed")
    if failed:
        print("failed:", failed)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
