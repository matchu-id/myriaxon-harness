"""Inventory of what Myriaxon actually provides, for gap analysis against other harnesses.

    python scripts/audit_capabilities.py

Reports the real surface: tools the agent has, slash commands, modes, providers, MCP support, plugin
support, and the dashboard's endpoints. Everything is read from the code rather than asserted, so the
gap list cannot drift from reality.
"""
import re
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

from myriaxon.dotenv_support import load_dotenv_into_environ  # noqa: E402

load_dotenv_into_environ()


def heading(title: str) -> None:
    print()
    print(f"--- {title} " + "-" * max(0, 60 - len(title)))


def main() -> int:
    from myriaxon import tool_registry
    from myriaxon.constants import (
        DEFAULT_PROVIDER,
        EFFORT_LEVELS,
        MODES,
        PROVIDER_ANTHROPIC,
        PROVIDER_DEEPSEEK,
        PROVIDER_GEMINI,
        PROVIDER_NETRA,
        PROVIDER_OPENAI_COMPAT,
        PROVIDER_OPENROUTER,
    )
    from myriaxon.model_profiles import PROFILES

    heading("PROVIDERS")
    for provider in (PROVIDER_GEMINI, PROVIDER_NETRA, PROVIDER_DEEPSEEK, PROVIDER_OPENROUTER,
                     PROVIDER_ANTHROPIC, PROVIDER_OPENAI_COMPAT):
        print(f"  {provider}")

    heading("TOOLS")
    tools = sorted(tool_registry.TOOL_SCHEMAS.keys()) if hasattr(tool_registry, "TOOL_SCHEMAS") else []
    if not tools:
        for attribute in ("TOOL_SPECS", "TOOLS", "TOOL_DEFS"):
            mapping = getattr(tool_registry, attribute, None)
            if isinstance(mapping, dict):
                tools = sorted(mapping.keys())
                break
    if not tools:
        # Fall back to the builder the CLI uses.
        built = tool_registry.build_tool_defs_gemini()
        tools = sorted(t.get("name") or (t.get("function") or {}).get("name", "") for t in built)
    print(f"  {len(tools)} tools")
    for index in range(0, len(tools), 3):
        print("   ", "  ".join(name.ljust(24) for name in tools[index:index + 3]))

    heading("MODES")
    for mode in MODES:
        print(f"  {mode}")

    heading("EFFORT LEVELS")
    print(f"  {', '.join(EFFORT_LEVELS)}")

    heading("MODEL PROFILES (context adaptation)")
    for name, profile in PROFILES.items():
        print(f"  {name:<10} ctx={profile.context_window:>9,}  verbosity={profile.verbosity:<6} "
              f"tool_results<={profile.tool_result_chars:>6,}ch  delegation={profile.allow_delegation}")

    heading("SLASH COMMANDS")
    cli_source = (REPO / "myriaxon" / "cli.py").read_text(encoding="utf-8")
    commands = sorted(set(re.findall(r'lowered == "(/[a-z-]+)"', cli_source)))
    print(f"  {len(commands)} commands")
    for index in range(0, len(commands), 4):
        print("   ", "  ".join(name.ljust(18) for name in commands[index:index + 4]))

    heading("MCP SUPPORT")
    mcp_source = (REPO / "myriaxon" / "mcp_client.py").read_text(encoding="utf-8")
    dashboard_source = (REPO / "myriaxon" / "dashboard" / "server.py").read_text(encoding="utf-8")
    print(f"  client implemented  yes ({len(mcp_source.splitlines()):,} lines)")
    print(f"  stdio servers       {'yes' if 'stdio' in mcp_source.lower() else 'no'}")
    print(f"  http/url servers    {'yes' if 'add_url_server' in mcp_source else 'no'}")
    print(f"  registry search     {'yes' if 'search_registry' in mcp_source else 'no'}")
    print(f"  add/remove in CLI   {'yes' if 'add_stdio_server' in mcp_source else 'no'}")
    print(f"  manageable from UI  {'yes' if '/api/mcp' in dashboard_source else 'NO -- CLI only'}")

    heading("PLUGIN SUPPORT")
    for candidate in ("plugins.py", "extensions.py", "plugin_loader.py"):
        print(f"  {candidate:<20} {'present' if (REPO / 'myriaxon' / candidate).is_file() else 'MISSING'}")
    # Skills, hooks and subagents together cover most of what a plugin API would provide; the
    # remaining gap is one discovery point for third-party code.
    hooks_source = (REPO / "myriaxon" / "hooks.py").read_text(encoding="utf-8")
    hook_events = sorted(set(re.findall(r'["\']((?:Pre|Post|User|Stop)[A-Za-z]+)["\']', hooks_source)))
    print(f"  hook events       {len(hook_events)}: {', '.join(hook_events) if hook_events else '(none detected)'}")

    heading("SKILLS / SUBAGENTS / HOOKS")
    for name in ("skills.py", "subagents.py", "hooks.py"):
        path = REPO / "myriaxon" / name
        print(f"  {name:<16} {'present' if path.is_file() else 'MISSING'}")

    heading("DASHBOARD ENDPOINTS")
    routes = sorted(set(re.findall(r'route == "(/api/[a-z_-]+)"', dashboard_source)))
    print(f"  {len(routes)} routes")
    for index in range(0, len(routes), 3):
        print("   ", "  ".join(name.ljust(24) for name in routes[index:index + 3]))

    heading("DASHBOARD UI CAPABILITIES")
    ui = (REPO / "myriaxon" / "dashboard" / "ui.py").read_text(encoding="utf-8")
    for label, needle in (
        ("multi-vendor dropdown", "provider-sel"),
        ("multi-model dropdown", 'id="model"'),
        ("add custom endpoint", "vendormanage"),
        ("endpoint probe/test", "vtest"),
        ("MCP management", "mcpmodal"),
        ("plugin management", "plugin"),
        # The file explorer moved into the code modal's Files pane; the old `filetree` id no longer
        # exists anywhere in the page, which made this row report MISSING for a shipped feature.
        ("file explorer", "codepane-files"),
        ("image attachments", "api/upload"),
        ("light/dark theme", "themebtn"),
        ("settings panel", "setform"),
        ("project picker", "projmodal"),
        ("session delete", 'class="del"'),
    ):
        present = needle in ui
        print(f"  {label:<24} {'yes' if present else 'MISSING'}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
