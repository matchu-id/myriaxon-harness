from dataclasses import dataclass
from pathlib import Path

from .constants import PROJECT_AGENTS_SUBDIR, USER_AGENTS_DIR
from .frontmatter import parse_frontmatter_doc


@dataclass
class SubagentType:
    name: str
    description: str
    instructions: str
    tool_allowlist: frozenset[str] | None
    source: Path | None


BUILTIN_SUBAGENT_TYPES: dict[str, SubagentType] = {
    "general-purpose": SubagentType(
        name="general-purpose",
        description="Default sub-agent with the full tool set (minus task/ask_user_question).",
        instructions="",
        tool_allowlist=None,
        source=None,
    ),
    "explore": SubagentType(
        name="explore",
        description="Read-only sub-agent for locating code: file search, content search, reading files.",
        instructions=(
            "You are a read-only exploration sub-agent. Use only read_file/glob_search/grep_search "
            "to answer the question -- do not attempt to write, edit, or run commands."
        ),
        tool_allowlist=frozenset({"read_file", "glob_search", "grep_search"}),
        source=None,
    ),
}


def _parse_agent_md(text: str, source: Path) -> SubagentType | None:
    parsed = parse_frontmatter_doc(text, source)
    if parsed is None:
        return None
    frontmatter, body = parsed
    if "name" not in frontmatter:
        return None

    tools_field = frontmatter.get("tools", "").strip()
    allowlist = frozenset(t.strip() for t in tools_field.split(",") if t.strip()) if tools_field else None

    return SubagentType(
        name=frontmatter["name"],
        description=frontmatter.get("description", ""),
        instructions=body,
        tool_allowlist=allowlist,
        source=source,
    )


def _scan_agents_dir(agents_dir: Path) -> dict[str, SubagentType]:
    found: dict[str, SubagentType] = {}
    if not agents_dir.is_dir():
        return found

    for entry in sorted(agents_dir.glob("*.md")):
        try:
            text = entry.read_text(encoding="utf-8")
        except OSError:
            continue
        subagent_type = _parse_agent_md(text, entry)
        if subagent_type is not None:
            found[subagent_type.name] = subagent_type

    return found


def discover_subagent_types(project_root: Path | None = None) -> dict[str, SubagentType]:
    types = dict(BUILTIN_SUBAGENT_TYPES)
    types.update(_scan_agents_dir(USER_AGENTS_DIR))

    root = project_root if project_root is not None else Path.cwd()
    types.update(_scan_agents_dir(root / PROJECT_AGENTS_SUBDIR))

    return types


def format_subagent_type_listing(types: dict[str, SubagentType]) -> str:
    if not types:
        return "(no subagent types discovered)"
    return "\n".join(f"- {t.name}: {t.description}" for t in types.values())
