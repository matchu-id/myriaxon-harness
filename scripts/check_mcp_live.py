"""Connect to a real MCP server and call one of its tools, end to end.

    python scripts/check_mcp_live.py

Uses scripts/mcp_test_server.py as the server, started over stdio the same way a configured stdio
server is. This is the check that was impossible before the `mcp` SDK was installed: everything else
about MCP could be verified from configuration, but "does a tool from a real server actually reach the
model and work" could not.

Also asserts namespacing: the fixture server exposes a tool literally named `read_file`, and it must
arrive as `mcp__<server>__read_file` -- never as `read_file`, which would shadow the built-in.
"""
import sys
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


SERVER_NAME = "myriaxontest"


def _stdio_blocked() -> str:
    """Why a stdio MCP server cannot be launched here, or "" if it can.

    Stdio MCP uses `asyncio.create_subprocess_exec` with piped stdio, which creates the pipes with
    `_winapi.CreateFile`. A confined environment denies that even though the *blocking* subprocess API
    works -- measured both ways:

        subprocess.run(..., capture_output=True)          -> rc 0
        asyncio.create_subprocess_exec(..., PIPE)         -> PermissionError [WinError 5]

    So the probe has to use the asyncio path; probing with subprocess.run reported "not blocked" and
    the real failure surfaced later as a connection error, which read as a bridge defect rather than an
    environment boundary. Remote MCP needs no pipes and is covered by check_mcp_remote_live.py.
    """
    import asyncio
    import sys as _sys

    async def probe() -> str:
        try:
            process = await asyncio.create_subprocess_exec(
                _sys.executable, "-c", "print(1)",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            await process.communicate()
            return ""
        except OSError as exc:
            return f"asyncio cannot create a piped child process here ({type(exc).__name__}: {exc})"
        except BaseException as exc:  # noqa: BLE001
            return f"child process probe failed ({type(exc).__name__}: {exc})"

    return asyncio.run(probe())


def main() -> int:
    from rich.console import Console

    from myriaxon.mcp_client import McpBridge, _MCP_AVAILABLE, remote_transport_support

    if not _MCP_AVAILABLE:
        print("the 'mcp' package is not installed")
        return 2

    print(f"transports available: {remote_transport_support()}\n")

    blocked = _stdio_blocked()
    if blocked:
        print("=== SKIPPED: stdio MCP cannot run in this environment ===")
        print(f"    {blocked}")
        print("    remote MCP is verified separately by check_mcp_remote_live.py")
        # Still assert the parts that do not need a child process.
        console = Console(quiet=True)
        bridge = McpBridge(console)
        out = bridge.start([{"name": "x", "command": "definitely-not-a-real-command", "args": []}], [])
        check("a server that cannot start yields no tools instead of raising", out == [], out)
        check("no phantom server is recorded as connected", bridge.connected_server_names() == set(),
              bridge.connected_server_names())
        print(f"\n{len(passed)} passed, {len(failed)} failed (stdio body skipped)")
        return 1 if failed else 0

    console = Console(quiet=True)
    bridge = McpBridge(console)
    fixture = REPO / "scripts" / "mcp_test_server.py"

    server_conf = {
        "name": SERVER_NAME,
        "command": sys.executable,
        "args": [str(fixture)],
    }

    print("=== starting a real MCP server over stdio ===")
    tool_defs = bridge.start([server_conf], [])
    names = sorted(t.get("name") for t in tool_defs)
    print(f"    tools discovered: {names}")

    check("the server connected", bool(tool_defs), "no tools returned")
    check("the server is reported as connected", SERVER_NAME in bridge.connected_server_names(),
          bridge.connected_server_names())

    check("its tools are namespaced", all(n.startswith(f"mcp__{SERVER_NAME}__") for n in names), names)
    check("the echo tool is present", any(n.endswith("__echo") for n in names), names)
    check("the add tool is present", any(n.endswith("__add") for n in names), names)
    check("a server tool named read_file did NOT become the plain built-in name",
          "read_file" not in names, names)
    check("...but is reachable under its namespaced name",
          any(n.endswith("__read_file") for n in names), names)
    check("tool schemas came through", all(isinstance(t.get("input_schema"), dict) for t in tool_defs),
          [type(t.get("input_schema")).__name__ for t in tool_defs])

    print("\n=== calling a tool on the real server ===")
    echo_name = next((n for n in names if n.endswith("__echo")), None)
    if echo_name:
        result = bridge.call_tool(echo_name, {"text": "hello from Myriaxon"})
        print(f"    {echo_name} -> {str(result)[:120]!r}")
        check("the tool ran and returned a result", "hello from Myriaxon" in str(result), result)
    else:
        check("the tool ran and returned a result", False, "no echo tool")

    add_name = next((n for n in names if n.endswith("__add")), None)
    if add_name:
        result = bridge.call_tool(add_name, {"a": 17, "b": 25})
        print(f"    {add_name} -> {str(result)[:120]!r}")
        check("a typed tool returned the right value", "42" in str(result), result)

    print("\n=== an unknown tool is reported, not crashed ===")
    result = bridge.call_tool(f"mcp__{SERVER_NAME}__does_not_exist", {})
    # The bridge returns (text, is_error), matching the rest of the tool registry. Asserting on a bare
    # string here was wrong: the tuple is the contract.
    text, is_error = result if isinstance(result, tuple) else (result, False)
    print(f"    -> {str(text)[:100]!r} is_error={is_error}")
    check("an unknown tool returns an error", bool(text), result)
    check("...flagged as an error", bool(is_error) or "unknown" in str(text).lower(), result)

    print("\n=== a broken server does not stop the bridge ===")
    second = McpBridge(console)
    bad = [{"name": "broken", "command": sys.executable, "args": ["-c", "raise SystemExit(1)"]}]
    out = second.start(bad, [])
    check("a server that fails to start yields no tools", out == [], out)

    print(f"\n{len(passed)} passed, {len(failed)} failed")
    if failed:
        print("failed:", failed)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
