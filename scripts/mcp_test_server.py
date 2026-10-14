"""A minimal MCP server used as a test fixture.

    python scripts/mcp_test_server.py

Speaks stdio MCP and exposes three tools. Two details are deliberate:

* one tool is named `read_file`, identical to a built-in Myriaxon tool. A server's tools must arrive
  namespaced as `mcp__<server>__<tool>`, so this proves a server cannot shadow a built-in.
* one tool takes typed integer arguments, so the round trip through the JSON schema is exercised
  rather than only string passthrough.

Supports both SDK generations: mcp 1.x calls the class `FastMCP`, mcp 2.x renamed it `MCPServer` and
moved it to `mcp.server.mcpserver`. Importing the wrong name is what made this fixture exit silently
with code 1 the first time it ran.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from mcp.server.mcpserver import MCPServer as _Server  # mcp 2.x
except ImportError:
    try:
        from mcp.server.fastmcp import FastMCP as _Server  # mcp 1.x
    except ImportError:
        print("the 'mcp' package (with server support) is required", file=sys.stderr)
        raise SystemExit(2)

server = _Server("myriaxon-test-server")


@server.tool()
def echo(text: str) -> str:
    """Echo the text back."""
    return f"echo: {text}"


@server.tool()
def read_file(path: str) -> str:
    """Deliberately named like a built-in tool, to prove namespacing."""
    return f"the server's own read_file saw {path}"


@server.tool()
def add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b


if __name__ == "__main__":
    server.run("stdio")
