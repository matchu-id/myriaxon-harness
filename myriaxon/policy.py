"""Validation for permission-rule keys.

Rule keys are what a user types into a policy file or the dashboard's rule editor. They are checked
here, in the core, so that a rule which *looks* fine but can never match is rejected at the point the
user writes it rather than silently doing nothing at request time.

Accepted forms:

    "*"                  every tool
    "run_bash"           one tool, whatever its arguments
    "run_bash:git *"     one tool, when its primary argument matches the glob

Rejected, with a reason:
    ""                   empty
    "run_bash "          whitespace-only pattern
    ":"                  no tool name
    "run bash:git"       a space in the tool name (tool names never contain spaces)
    "*:git *"            a wildcard tool name -- ambiguous, and "*" already covers every tool
"""
from __future__ import annotations

import re

# Tool names in this codebase are lowercase identifiers, optionally namespaced with "__" for MCP
# tools (mcp__server__tool).
_TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")


def validate_rule_key(key: str) -> str | None:
    """Return a human-readable reason the key is invalid, or None when it is fine."""
    if key is None:
        return "empty rule"
    key = str(key).strip()
    if not key:
        return "empty rule"

    if ":" in key:
        tool, _, pattern = key.partition(":")
        tool = tool.strip()
        if not tool:
            return "no tool name before the ':'"
        # Checked before the name pattern, so "*" gets its specific explanation rather than the
        # generic "invalid tool name" message (which is what happened first time round).
        if tool == "*":
            return "a wildcard tool with a pattern is ambiguous; use '*' alone to cover every tool"
        if not _TOOL_NAME_RE.match(tool):
            return (
                f"invalid tool name {tool!r} -- tool names have no spaces; "
                "use 'tool:pattern' for an argument rule"
            )
        if not pattern.strip():
            return "empty pattern after the ':'"
        return None

    if not _TOOL_NAME_RE.match(key):
        return f"invalid tool name {key!r} -- names have no spaces"
    return None


def describe_rule(key: str, decision: str) -> str:
    """One-line explanation, so the UI can show what a rule will actually do."""
    if key == "*":
        return f"every tool is {decision}ed"
    if ":" in key:
        tool, _, pattern = key.partition(":")
        return f"{tool} is {decision}ed when its argument matches {pattern!r}"
    return f"{key} is {decision}ed"
