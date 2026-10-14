"""Print the MCP helper signatures so the dashboard panel calls them correctly."""
import inspect
import sys
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
warnings.filterwarnings("ignore")

from myriaxon import mcp_client

for name in (
    "load_mcp_config",
    "save_mcp_config",
    "add_stdio_server",
    "add_url_server",
    "remove_server",
    "search_registry",
    "resolve_registry_stdio_spec",
    "resolve_registry_url_spec",
):
    function = getattr(mcp_client, name, None)
    print(f"  {name}{inspect.signature(function) if function else ' -- MISSING'}")

print()
print("  config keys:", list(mcp_client.load_mcp_config().keys()))
