"""Verify the plugins panel and reload over HTTP.

    python scripts/check_plugins_ui.py [port]

The plugin engine is covered offline by check_plugins.py. This covers the dashboard surface: the
config payload the panel renders, and the reload endpoint that makes a newly dropped-in plugin take
effect without restarting the server.
"""
import json
import shutil
import sys
import urllib.request
from pathlib import Path

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8791
BASE = f"http://127.0.0.1:{PORT}"
REPO = Path(__file__).resolve().parent.parent

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


PLUGIN = '''
PLUGIN = {"name": "ui_probe", "version": "9.9", "description": "dashboard fixture"}


def register(api):
    api.add_tool("ui_probe_tool", "A tool from a plugin.",
                 {"type": "object", "properties": {}}, lambda tool_input, ctx: "ok")
    api.on("pre_tool_use", lambda *args: None)
'''


def main() -> int:
    config = get("/api/config")
    print(f"provider={config['provider']}\n")

    print("=== the config exposes plugin state ===")
    plugins = config.get("plugins") or {}
    check("plugins section present", bool(plugins), list(config))
    check("plugin directories are listed", len(plugins.get("dirs") or []) >= 1, plugins.get("dirs"))
    check("hook events advertised", len(plugins.get("events") or []) >= 1, plugins.get("events"))
    for directory in plugins.get("dirs") or []:
        print(f"    looking in: {directory}")

    # The project plugin directory the server watches: workspace/.myriaxon/plugins
    workspace = Path(config["workspace"])
    project_plugins = workspace / ".myriaxon" / "plugins"
    recovered = project_plugins.exists()

    print("\n=== a dropped-in plugin is picked up by Reload ===")
    project_plugins.mkdir(parents=True, exist_ok=True)
    fixture = project_plugins / "ui_probe.py"
    fixture.write_text(PLUGIN, encoding="utf-8")
    try:
        out = post("/api/plugins/reload", {})
        names = [p["name"] for p in out.get("plugins") or []]
        check("reload reports the new plugin", "ui_probe" in names, names)
        entry = next((p for p in out.get("plugins") or [] if p["name"] == "ui_probe"), None)
        check("the plugin is marked ok", bool(entry and entry.get("ok")), entry)
        check("its version is reported", bool(entry and entry.get("version") == "9.9"), entry)
        check("its tool is listed for the agent", "ui_probe_tool" in (out.get("tools") or []), out.get("tools"))
        check("its hook is counted", (out.get("hooks") or {}).get("pre_tool_use", 0) >= 1, out.get("hooks"))
        check("no failures reported", not (out.get("failed") or []), out.get("failed"))
    finally:
        # Leave the workspace as it was found: a fixture plugin must not persist into other suites.
        fixture.unlink(missing_ok=True)
        try:
            if not any(project_plugins.iterdir()):
                project_plugins.rmdir()
        except OSError:
            pass

    print("\n=== reload after removal clears it ===")
    after = post("/api/plugins/reload", {})
    check("the fixture plugin is gone", "ui_probe" not in [p["name"] for p in after.get("plugins") or []],
          [p["name"] for p in after.get("plugins") or []])
    check("its tool is gone too", "ui_probe_tool" not in (after.get("tools") or []), after.get("tools"))

    if not recovered:
        check("workspace left as found", not project_plugins.exists(), str(project_plugins))

    print(f"\n{len(passed)} passed, {len(failed)} failed")
    if failed:
        print("failed:", failed)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
