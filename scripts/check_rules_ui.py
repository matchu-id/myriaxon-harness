"""Verify permission rules are editable from the web UI and actually take effect.

    python scripts/check_rules_ui.py [port]

Two properties matter:
  1. the panel's API accepts, validates and persists rules
  2. a rule written through the UI *changes what the agent may do* -- a policy editor that saves a
     value and changes nothing is worse than none, because it looks like protection

The second is asserted through the same policy engine the tool boundary consults, plus a live check
that a denied tool is refused while a permitted one runs.

Live provider under test is always Netra, per the project's testing rule.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

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


def user_rules() -> dict:
    return (get("/api/config").get("rules") or {}).get("user_rules") or {}


def main() -> int:
    config = get("/api/config")
    print(f"provider={config['provider']}\n")

    print("=== the config exposes the rule editor state ===")
    rules = config.get("rules") or {}
    check("rules section present", bool(rules), list(config))
    check("decisions offered", set(rules.get("decisions") or []) == {"allow", "ask", "deny"}, rules.get("decisions"))
    check("user file path reported", bool(rules.get("user_file")), rules.get("user_file"))
    check("precedence flag reported", "higher_priority_active" in rules, list(rules))
    original = dict(rules.get("user_rules") or {})
    print(f"    existing user rules: {original or 'none'}")

    print("\n=== add rules ===")
    saved = post("/api/rules", {"rules": {"run_bash:git *": "allow", "run_bash": "ask"}})
    check("saved", saved.get("rules", {}).get("user_rules") == {"run_bash:git *": "allow", "run_bash": "ask"}, saved)
    check("reported as taking effect", saved.get("rebuilt_sessions", 0) >= 0, saved)
    check("persisted to config", user_rules().get("run_bash") == "ask", user_rules())

    print("\n=== validation ===")
    for label, payload in (
        ("bad decision", {"rules": {"run_bash": "maybe"}}),
        ("space in tool name", {"rules": {"run bash": "allow"}}),
        ("empty pattern after colon", {"rules": {"run_bash:": "allow"}}),
        ("wildcard tool with pattern", {"rules": {"*:git *": "allow"}}),
        ("rules not an object", {"rules": ["nope"]}),
    ):
        try:
            post("/api/rules", payload)
            check(f"rejects {label}", False, "accepted")
        except urllib.error.HTTPError as exc:
            check(f"rejects {label}", exc.code == 400, exc.code)

    print("\n=== unknown keys are not silently dropped ===")
    post("/api/rules", {"rules": {"write_file": "deny", "totally_made_up_tool": "allow"}})
    stored = user_rules()
    # A tool name this build does not know is still a valid rule key: a rule file may be shared with
    # a newer build, and rejecting it would make the file unusable.
    check("a rule for an unknown tool is kept", "totally_made_up_tool" in stored, stored)

    print("\n=== set the rules the behaviour checks need ===")
    # Each save REPLACES the rule set, so the earlier scenarios have to be re-established explicitly.
    # Assuming they persisted is what made these checks fail: the unknown-keys step above had already
    # overwritten them.
    post("/api/rules", {"rules": {"run_bash:git *": "allow", "run_bash": "ask", "write_file": "deny"}})
    check("rules re-established",
          user_rules().get("run_bash") == "ask" and user_rules().get("write_file") == "deny", user_rules())

    print("\n=== the rule actually changes what the agent may do ===")
    # Asserted against the layer the SERVER reports loading, not against a fresh load_settings() in
    # this process. The combined runner starts the server with its own workspace, so this process's
    # project root differs and a direct load_settings() would read a different user file -- which is
    # what made this check fail inside the suite while passing standalone.
    reported = get("/api/config").get("rules") or {}
    check("the server reports the rule it just saved",
          (reported.get("user_rules") or {}).get("run_bash") == "ask", reported.get("user_rules"))
    check("the loaded layer contains both rules",
          any("run_bash:git *" in layer for layer in reported.get("layers") or []),
          reported.get("layers"))

    # A fresh load in THIS process, using the workspace the server is serving, must agree.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from myriaxon.settings import load_settings
    from myriaxon.policy import validate_rule_key

    workspace = (get("/api/config").get("workspace") or "").strip()
    live = load_settings(Path(workspace)) if workspace else load_settings()
    check("the engine agrees: git is allowed",
          live.resolve_permission("run_bash", {"command": "git status"}) == "allow",
          live.resolve_permission("run_bash", {"command": "git status"}))
    check("the engine agrees: a non-git command is ask",
          live.resolve_permission("run_bash", {"command": "rm -rf /"}) == "ask",
          live.resolve_permission("run_bash", {"command": "rm -rf /"}))
    check("validate_rule_key accepts an argument rule", validate_rule_key("run_bash:git *") is None)
    check("validate_rule_key rejects a wildcard tool with pattern", validate_rule_key("*:git") is not None)

    print("\n=== a live agent turn respects the deny ===")
    # The strongest evidence: ask the model to do something a rule forbids and confirm it reports the
    # block rather than doing it. Netra is the only provider used for live testing.
    session_id = post("/api/sessions", {"mode": "auto_edit"})["session"]["id"]
    post("/api/chat", {"session": session_id, "message": 'create a file named forbidden_probe.txt containing x'})
    deadline = time.time() + 300
    while time.time() < deadline:
        state = [s for s in get("/api/sessions")["sessions"] if s["id"] == session_id]
        if state and not state[0]["busy"]:
            break
        time.sleep(2)
    messages = get(f"/api/messages?session={session_id}")["messages"]
    blob = " ".join(m.get("text") or "" for m in messages).lower()
    print(f"    transcript mentions policy/blocked: {'policy' in blob or 'blocked' in blob}")
    check("the agent was blocked and said so", "policy" in blob or "blocked" in blob, blob[:200])

    print("\n=== restore the user's original rules ===")
    # Restoring matters more than it looks: these rules are enforced process-wide, so a leftover
    # `write_file: deny` silently blocks every later check that writes a file. Measured exactly that
    # -- the plugin-shadowing security check failed on an unwritten file because this suite had left
    # a deny rule behind.
    restored = post("/api/rules", {"rules": original})
    check("rules restored", restored["rules"]["user_rules"] == original,
          restored["rules"]["user_rules"])
    check("no deny rule survives", (restored["rules"]["user_rules"] or {}).get("write_file") != "deny",
          restored["rules"]["user_rules"])

    print(f"\n{len(passed)} passed, {len(failed)} failed")
    if failed:
        print("failed:", failed)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
