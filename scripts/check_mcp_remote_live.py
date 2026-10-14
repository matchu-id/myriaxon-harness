"""Verify remote MCP end to end against a real HTTP server.

    python scripts/check_mcp_remote_live.py

Stdio MCP cannot be launched in this sandbox: `asyncio` creates the child's pipes with CreateFile and
the sandbox denies it (WinError 5, "shared memory / named pipe" confinement). Remote HTTP MCP needs no
pipes, and it is the transport hosted services use -- Figma's and draw.io's servers are HTTP endpoints,
not local commands.

So this starts a genuine MCP server over streamable HTTP on a loopback port, then connects to it
through McpBridge exactly as a configured `url_server` would be, lists its tools and calls one. That
covers the whole remote path: config -> transport negotiation -> initialize -> tools/list ->
tools/call.
"""
import asyncio
import socket
import sys
import threading
import time
import traceback
import warnings
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
warnings.filterwarnings("ignore")

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

passed, failed = [], []


def check(label, condition, detail=""):
    (passed if condition else failed).append(label)
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}{(' -- ' + str(detail)) if detail and not condition else ''}")


PORT = 8934


def free_port(preferred: int) -> int:
    for candidate in (preferred, preferred + 1, preferred + 2):
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", candidate))
                return candidate
            except OSError:
                continue
    raise RuntimeError("no free port")


def build_server():
    """A real MCP server with the same three fixture tools as the stdio one."""
    try:
        from mcp.server.mcpserver import MCPServer
    except ImportError:
        from mcp.server.fastmcp import FastMCP as MCPServer

    server = MCPServer("myriaxon-remote-fixture")

    @server.tool()
    def echo(text: str) -> str:
        """Echo the text back."""
        return f"echo: {text}"

    @server.tool()
    def read_file(path: str) -> str:
        """Named like a built-in, to prove namespacing over the remote path too."""
        return f"remote server read_file saw {path}"

    @server.tool()
    def add(a: int, b: int) -> int:
        """Add two integers."""
        return a + b

    return server


def main() -> int:
    from rich.console import Console

    from myriaxon.mcp_client import McpBridge, remote_transport_support

    support = remote_transport_support()
    print(f"transports: {support}\n")
    if not (support.get("streamable_http") or support.get("sse")):
        print("no remote transport available in this install")
        return 2

    port = free_port(PORT)
    server = build_server()
    server_error: dict = {}

    def run_server():
        try:
            # Blocking; runs on its own thread for the life of the test.
            server.run("streamable-http", host="127.0.0.1", port=port)
        except BaseException:  # noqa: BLE001
            server_error["traceback"] = traceback.format_exc()

    thread = threading.Thread(target=run_server, daemon=True, name="mcp-fixture-http")
    thread.start()

    print(f"=== starting a real MCP server on http://127.0.0.1:{port} ===")
    ready = False
    for _ in range(60):
        with socket.socket() as probe:
            try:
                probe.connect(("127.0.0.1", port))
                ready = True
                break
            except OSError:
                time.sleep(0.5)
    check("the fixture server is listening", ready, server_error.get("traceback", "")[-300:])
    if not ready:
        return 1

    url = f"http://127.0.0.1:{port}/mcp"
    console = Console(quiet=True)
    bridge = McpBridge(console)
    conf = {"name": "remotefix", "url": url, "timeout": 20.0}

    print("\n=== connecting through McpBridge as a url_server ===")
    try:
        tool_defs = bridge.start([], [conf])
    except BaseException:  # noqa: BLE001
        traceback.print_exc()
        tool_defs = []
    names = sorted(t.get("name") for t in tool_defs)
    print(f"    tools: {names}")

    check("a remote server connected", bool(tool_defs), "no tools returned")
    check("it is reported as connected", "remotefix" in bridge.connected_server_names(),
          bridge.connected_server_names())
    check("tools are namespaced", all(n.startswith("mcp__remotefix__") for n in names), names)
    check("echo arrived", any(n.endswith("__echo") for n in names), names)
    check("add arrived", any(n.endswith("__add") for n in names), names)
    check("a remote read_file did not become the built-in name", "read_file" not in names, names)

    print("\n=== calling a remote tool ===")
    echo_name = next((n for n in names if n.endswith("__echo")), None)
    if echo_name:
        result = bridge.call_tool(echo_name, {"text": "hello remote"})
        print(f"    {echo_name} -> {str(result)[:110]!r}")
        check("the remote tool returned a result", "hello remote" in str(result), result)
    else:
        check("the remote tool returned a result", False, "no echo tool")

    add_name = next((n for n in names if n.endswith("__add")), None)
    if add_name:
        result = bridge.call_tool(add_name, {"a": 20, "b": 22})
        print(f"    {add_name} -> {str(result)[:110]!r}")
        check("typed remote arguments worked", "42" in str(result), result)

    print("\n=== auth headers are sent when configured ===")
    # The fixture does not require auth, so this asserts the configuration path rather than rejection:
    # a token must not break the connection (it is sent as a bearer header).
    authed = McpBridge(console)
    conf_authed = {"name": "authedfix", "url": url, "timeout": 20.0, "token": "test-token-value"}
    authed_defs = authed.start([], [conf_authed])
    check("a token does not prevent connecting", len(authed_defs) == len(tool_defs),
          f"{len(authed_defs)} vs {len(tool_defs)}")

    print("\n=== a bad URL fails cleanly ===")
    bad = McpBridge(console)
    bad_defs = bad.start([], [{"name": "dud", "url": f"http://127.0.0.1:{port}/nope", "timeout": 8.0}])
    check("an unreachable endpoint yields no tools instead of raising", bad_defs == [], bad_defs)

    print(f"\n{len(passed)} passed, {len(failed)} failed")
    if failed:
        print("failed:", failed)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
