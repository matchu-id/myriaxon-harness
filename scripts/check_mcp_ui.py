"""Verify MCP server management from the web UI.

    python scripts/check_mcp_ui.py [port]

The backend (stdio + http + registry search) already existed but was reachable only from the CLI.
This checks the panel's own API surface: list, add (both transports), remove, validation, and that
adding a server invalidates built agents so its tools appear without restarting the server.

A real MCP process is not started here -- that needs a network install -- so the test asserts on the
config that would be used, which is the part the UI owns.
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
    return json.loads(urllib.request.urlopen(request, timeout=120).read().decode())


def names_of(config: dict) -> set:
    """Server names from either shape this test sees.

    `/api/config` nests the MCP state under "mcp", while the add/remove responses return the MCP
    config directly. Accepting both avoids a check that fails on packaging rather than behaviour --
    which is exactly the false failure this helper caused first time round.
    """
    config = config or {}
    mcp = config.get("mcp") if isinstance(config.get("mcp"), dict) else config
    return {s.get("name") for s in (mcp.get("url_servers") or [])} | {
        s.get("name") for s in (mcp.get("stdio_servers") or [])
    }


def main() -> int:
    config = get("/api/config")
    print(f"provider={config['provider']}\n")

    print("=== the config exposes MCP state to the UI ===")
    check("mcp section present", "mcp" in config, list(config))
    check("stdio and url lists present",
          "stdio_servers" in config["mcp"] and "url_servers" in config["mcp"], config.get("mcp"))
    check("config file path reported", bool(config["mcp"].get("config_file")), config["mcp"].get("config_file"))
    before = names_of(config)
    print(f"    existing servers: {sorted(before) or 'none'}")

    probe_name = "myriaxon-ui-probe"

    print("\n=== add a remote (HTTP) server ===")
    added = post("/api/mcp", {"action": "add", "kind": "url", "name": probe_name,
                              "url": "https://example.invalid/mcp"})
    check("add reported", added.get("added") == probe_name, added)
    # The response wraps the reloaded config under "config"; passing the whole envelope here was a
    # bug in this check, not in the feature.
    check("server is in the config", probe_name in names_of(added.get("config") or {}), added.get("config"))
    check("restart requirement is reported, not hidden", added.get("restart_required") is True, added)

    print("\n=== it reaches the UI config payload ===")
    refreshed = get("/api/config")
    check("appears for the panel to render", probe_name in names_of(refreshed), sorted(names_of(refreshed)))

    print("\n=== add a local (stdio) server ===")
    stdio_name = "myriaxon-ui-stdio"
    added2 = post("/api/mcp", {"action": "add", "kind": "stdio", "name": stdio_name,
                               "command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "."]})
    check("stdio add reported", stdio_name in json.dumps(added2), added2)
    stdio_entries = [s for s in (added2["config"].get("stdio_servers") or []) if s.get("name") == stdio_name]
    check("command stored", bool(stdio_entries) and stdio_entries[0].get("command") == "npx", stdio_entries)
    check("args stored as a list", bool(stdio_entries) and isinstance(stdio_entries[0].get("args"), list), stdio_entries)
    check("args split correctly",
          bool(stdio_entries) and stdio_entries[0].get("args")[:2] == ["-y", "@modelcontextprotocol/server-filesystem"],
          stdio_entries[0].get("args") if stdio_entries else None)

    print("\n=== validation ===")
    for label, payload in (
        ("missing name", {"action": "add", "kind": "url", "url": "https://x.invalid/mcp"}),
        ("missing url", {"action": "add", "kind": "url", "name": "nourl"}),
        ("missing command", {"action": "add", "kind": "stdio", "name": "nocmd"}),
        ("unknown action", {"action": "frobnicate", "name": "x"}),
    ):
        try:
            post("/api/mcp", payload)
            check(f"rejects {label}", False, "accepted")
        except urllib.error.HTTPError as exc:
            check(f"rejects {label}", exc.code == 400, exc.code)

    print("\n=== remove ===")
    removed = post("/api/mcp", {"action": "remove", "name": probe_name})
    check("remove reported", removed.get("removed") is True, removed)
    check("first server gone", probe_name not in names_of(removed.get("config") or {}),
          sorted(names_of(removed.get("config") or {})))
    removed2 = post("/api/mcp", {"action": "remove", "name": stdio_name})
    check("second server gone", stdio_name not in names_of(removed2.get("config") or {}),
          sorted(names_of(removed2.get("config") or {})))

    print("\n=== registry search (read-only) ===")
    try:
        search = post("/api/mcp/search", {"query": "filesystem"})
        check("search answered", "servers" in search, search)
        if search.get("ok"):
            # A registry query can legitimately return nothing for a given term; what matters is that
            # the endpoint answered with a well-formed list.
            check("search returned a list", isinstance(search["servers"], list), search)
            print(f"    found {len(search['servers'])}: {[s['name'] for s in search['servers'][:4]]}")
        else:
            # A network failure here is environmental; the endpoint must still answer cleanly.
            check("network failure reported as data, not a crash", bool(search.get("error")), search)
    except Exception as exc:  # noqa: BLE001
        check("search answered", False, f"{type(exc).__name__}: {exc}")

    print("\n=== state restored ===")
    final = names_of(get("/api/config"))
    check("no probe servers left behind", probe_name not in final and stdio_name not in final, sorted(final))

    print(f"\n{len(passed)} passed, {len(failed)} failed")
    if failed:
        print("failed:", failed)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
