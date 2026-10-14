"""Verify the preconfigured MCP catalog: one click turns a server on.

    python scripts/check_mcp_catalog.py [port]

The promise being tested is "turn it on and it works": a catalog entry that needs nothing must install
from a key alone, and one needing a credential must say exactly which field is missing rather than
writing a configuration that fails later with a connection error.
"""
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8791
BASE = f"http://127.0.0.1:{PORT}"
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

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
    try:
        return json.loads(urllib.request.urlopen(request, timeout=120).read().decode())
    except urllib.error.HTTPError as exc:
        return {"_http_error": exc.code, "_body": exc.read().decode("utf-8", errors="replace")[:200]}


def mcp():
    return get("/api/config").get("mcp") or {}


def installed_names() -> set:
    state = mcp()
    return ({s["name"] for s in state.get("stdio_servers") or []}
            | {s["name"] for s in state.get("url_servers") or []})


def clean_up(names):
    for name in names:
        post("/api/mcp", {"action": "remove", "name": name})


def main() -> int:
    from myriaxon.mcp_catalog import all_entries, by_key, catalog_payload, resolve

    print("=== the catalog is offered to the UI ===")
    state = mcp()
    catalog = state.get("catalog") or []
    check("the catalog is present", bool(catalog), list(state))
    check("it has a useful number of entries", len(catalog) >= 12, len(catalog))
    check("entries are grouped", len(state.get("categories") or []) >= 3, state.get("categories"))
    keys = {c["key"] for c in catalog}
    for expected in ("figma", "drawio", "filesystem", "github", "memory", "fetch"):
        check(f"the catalog knows {expected}", expected in keys)
    ready = [c["key"] for c in catalog if c["ready"]]
    check("several need no input at all", len(ready) >= 5, ready)
    print(f"    ready with no input: {ready}")

    print("\n=== a server that needs nothing installs from a key alone ===")
    before = installed_names()
    clean_up([c["key"] for c in catalog if c["key"] in before])
    result = post("/api/mcp", {"action": "catalog_enable", "key": "figma"})
    check("enabling figma succeeded", "_http_error" not in result, result)
    names = installed_names()
    check("it is now configured", "figma" in names, names)
    urls = {s["name"]: s.get("url") for s in (mcp().get("url_servers") or [])}
    check("with the right URL", urls.get("figma") == "http://127.0.0.1:3845/mcp", urls)
    check("it is reported as installed in the catalog",
          any(c["key"] == "figma" and c["installed"] for c in mcp().get("catalog") or []))
    check("tools reload for the sessions", result.get("restart_required") is True, result)

    print("\n=== a local server that needs nothing ===")
    result = post("/api/mcp", {"action": "catalog_enable", "key": "memory"})
    check("enabling memory succeeded", "_http_error" not in result, result)
    stdio = {s["name"]: s for s in (mcp().get("stdio_servers") or [])}
    check("the command is configured", stdio.get("memory", {}).get("command") == "npx", stdio.get("memory"))
    check("its args are configured",
          (stdio.get("memory", {}).get("args") or [])[:1] == ["-y"], stdio.get("memory"))

    print("\n=== a server needing a credential is refused until it has one ===")
    result = post("/api/mcp", {"action": "catalog_enable", "key": "github"})
    check("enabling github without a token is refused", "_http_error" in result, result)
    body = str(result.get("_body") or "")
    check("the error names the field", "GitHub token" in body, body)
    check("nothing was configured", "github" not in installed_names(), installed_names())

    print("\n=== supplying the credential works ===")
    result = post("/api/mcp", {"action": "catalog_enable", "key": "github",
                               "values": {"env": "ghp_test_token_value"}})
    check("enabling github with a token succeeded", "_http_error" not in result, result)
    stdio = {s["name"]: s for s in (mcp().get("stdio_servers") or [])}
    env = (stdio.get("github") or {}).get("env") or {}
    check("the token landed in the right variable",
          env.get("GITHUB_PERSONAL_ACCESS_TOKEN") == "ghp_test_token_value", env)

    print("\n=== a path field is substituted, not ignored ===")
    result = post("/api/mcp", {"action": "catalog_enable", "key": "filesystem",
                               "values": {"path": "./src"}})
    check("enabling filesystem succeeded", "_http_error" not in result, result)
    stdio = {s["name"]: s for s in (mcp().get("stdio_servers") or [])}
    args = (stdio.get("filesystem") or {}).get("args") or []
    check("the chosen directory is in the command", "./src" in args, args)

    print("\n=== turning one off removes it ===")
    result = post("/api/mcp", {"action": "catalog_disable", "key": "memory"})
    check("disabling succeeded", "_http_error" not in result, result)
    check("it is gone from the configuration", "memory" not in installed_names(), installed_names())
    check("the catalog reports it as not installed",
          not any(c["key"] == "memory" and c["installed"] for c in mcp().get("catalog") or []))
    check("the removal was recorded", result.get("removed") is True, result)

    print("\n=== an unknown key is a clean 404, not a crash ===")
    result = post("/api/mcp", {"action": "catalog_enable", "key": "not-a-real-server"})
    check("an unknown key is refused", result.get("_http_error") in (400, 404), result)

    print("\n=== the catalog data itself is well formed ===")
    # all_entries() gives the dataclass objects; catalog_payload() gives the dicts the API serves. Using
    # the payload here and then reading `.transport` raised AttributeError, which is a bug in this check
    # rather than in the catalog.
    problems = []
    for entry in all_entries():
        if entry.transport not in ("stdio", "http"):
            problems.append(f"{entry.key}: bad transport {entry.transport}")
        if entry.transport == "stdio" and not entry.command:
            problems.append(f"{entry.key}: stdio without a command")
        if entry.transport == "http" and not entry.url.startswith("http"):
            problems.append(f"{entry.key}: http without a url")
        for need in entry.needs:
            if "key" not in need or "label" not in need:
                problems.append(f"{entry.key}: malformed need {need}")
    check("every entry is complete", not problems, problems)

    # Resolving every entry must produce a usable server; anything a user can supply is supplied, so a
    # failure here is a defect in the entry rather than a missing value.
    resolve_problems = []
    for entry in all_entries():
        values = {"token": "x", "env": "x", "path": "./x", "url": "postgresql://x/y"}
        try:
            server = resolve(entry, values)
            if not server.get("url") and not server.get("command"):
                resolve_problems.append(f"{entry.key}: resolved to nothing usable")
            leftover = [a for a in (server.get("args") or []) if "{" in str(a)]
            if leftover:
                resolve_problems.append(f"{entry.key}: unfilled placeholder {leftover}")
        except ValueError as exc:
            resolve_problems.append(f"{entry.key}: {exc}")
    check("every entry resolves to something usable", not resolve_problems, resolve_problems)

    # A blank optional field must fall back to its documented default, not leak the placeholder.
    defaulted = resolve(by_key("filesystem"), {})
    check("a blank field uses its default", "./" not in str(defaulted["args"]) and "{" not in str(defaulted["args"]),
          defaulted["args"])

    print("\n=== restore ===")
    clean_up([c["key"] for c in catalog if c["key"] in installed_names()])
    check("the catalog is back to nothing installed", not installed_names(), installed_names())

    print(f"\n{len(passed)} passed, {len(failed)} failed")
    if failed:
        print("failed:", failed)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
