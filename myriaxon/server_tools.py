from .constants import WEB_FETCH_TOOL_TYPE, WEB_SEARCH_TOOL_TYPE

WEB_SEARCH_TOOL = {"type": WEB_SEARCH_TOOL_TYPE, "name": "web_search"}
WEB_FETCH_TOOL = {"type": WEB_FETCH_TOOL_TYPE, "name": "web_fetch"}

_STATUS_LABELS = {
    "web_search": "searching the web",
    "web_fetch": "fetching a web page",
}


def status_line_for_block(content_block) -> str | None:
    """Themed status text for a streaming content_block_start event, or None to render nothing.

    Server-executed tools (web_search/web_fetch) never arrive as tool_use blocks — the API
    runs them and returns results inline — so this is purely a "something is happening" cue
    during the pause, not part of tool dispatch.
    """
    block_type = getattr(content_block, "type", None)
    if block_type == "server_tool_use":
        name = getattr(content_block, "name", None)
        label = _STATUS_LABELS.get(name, name or "server tool")
        return f"[tool.pending]{label}...[/]"
    if block_type == "mcp_tool_use":
        return "[tool.pending]calling remote MCP tool...[/]"
    return None
