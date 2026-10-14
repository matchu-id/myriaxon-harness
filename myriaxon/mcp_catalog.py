"""A catalog of known MCP servers, so a useful one is one click away.

The problem this solves: knowing that Figma's Dev Mode server lives at `http://127.0.0.1:3845/mcp`, or
that draw.io ships as `npx -y @drawio/mcp`, is knowledge a user should not have to acquire before getting
value. Each entry here is a server that has been configured the way its own documentation describes, so
enabling it is a toggle rather than a research task.

An entry is data, not code:

    key            stable id, also the server name unless overridden
    label          what the user sees
    category       grouping in the UI
    transport      "stdio" or "http"
    command/args   for stdio
    url            for http
    needs          list of secrets/fields the user must supply: token, env, path, url
    note           one line about what it gives you, and any prerequisite

`needs` drives the form: a server with no `needs` can be switched on immediately ("turn on and voila"),
and one with `needs` shows exactly the fields it requires instead of making the user guess.

Nothing here is installed or fetched automatically. `resolve()` turns an entry plus supplied values into
the configuration shape `mcp_servers.json` uses.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class CatalogEntry:
    key: str
    label: str
    category: str
    transport: str
    note: str = ""
    command: str = ""
    args: list[str] = field(default_factory=list)
    url: str = ""
    needs: list[dict] = field(default_factory=list)
    # Some servers need a package fetched on first run (npx/uvx), which is slow and needs the network.
    # Surfaced so the first use is not mysteriously slow.
    fetches_on_first_run: bool = False

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "category": self.category,
            "transport": self.transport,
            "note": self.note,
            "command": self.command,
            "args": self.args,
            "url": self.url,
            "needs": self.needs,
            "fetches_on_first_run": self.fetches_on_first_run,
            "ready": not self.needs,
        }


# Field kinds the UI knows how to render. "token" is a secret: stored, never returned.
NEEDS_TOKEN = {"key": "token", "label": "Access token", "kind": "token", "required": True}
NEEDS_PATH = lambda label, default=".": {"key": "path", "label": label, "kind": "text", "default": default}
NEEDS_URL = {"key": "url", "label": "Server URL", "kind": "text", "default": "http://127.0.0.1:3845/mcp"}


CATALOG: list[CatalogEntry] = [
    # ---------------------------------------------------------------- design
    CatalogEntry(
        key="figma",
        label="Figma (Dev Mode)",
        category="Design",
        transport="http",
        url="http://127.0.0.1:3845/mcp",
        note="Read frames, styles and assets, and generate code from a selection. Needs the Figma "
             "desktop app running with Dev Mode MCP enabled.",
    ),
    CatalogEntry(
        key="figma-hosted",
        label="Figma (hosted)",
        category="Design",
        transport="http",
        url="https://mcp.figma.com/mcp",
        needs=[NEEDS_TOKEN],
        note="Figma's hosted server. Needs a personal access token from Settings → Security.",
    ),
    CatalogEntry(
        key="drawio",
        label="draw.io",
        category="Design",
        transport="stdio",
        command="npx",
        args=["-y", "@drawio/mcp"],
        note="Create and edit draw.io diagrams. Fetched on first use.",
        fetches_on_first_run=True,
    ),
    CatalogEntry(
        key="excalidraw",
        label="Excalidraw",
        category="Design",
        transport="stdio",
        command="npx",
        args=["-y", "excalidraw-mcp"],
        note="Hand-drawn style diagrams. Fetched on first use.",
        fetches_on_first_run=True,
    ),
    # ---------------------------------------------------------------- files and data
    CatalogEntry(
        key="filesystem",
        label="Filesystem",
        category="Files & data",
        transport="stdio",
        command="npx",
        # `{path}`, not a literal ".": with the default baked into the args, the directory the user typed
        # was silently ignored and the server always exposed the working directory. The placeholder is
        # substituted from the value, and the *default* lives in `needs` where the form can show it.
        args=["-y", "@modelcontextprotocol/server-filesystem", "{path}"],
        note="Read and write files in a directory you choose.",
        needs=[NEEDS_PATH("Directory to expose", ".")],
        fetches_on_first_run=True,
    ),
    CatalogEntry(
        key="git",
        label="Git",
        category="Files & data",
        transport="stdio",
        command="uvx",
        args=["mcp-server-git", "--repository", "{path}"],
        note="Inspect history, diffs and branches. Requires uv.",
        needs=[NEEDS_PATH("Repository path", ".")],
        fetches_on_first_run=True,
    ),
    CatalogEntry(
        key="sqlite",
        label="SQLite",
        category="Files & data",
        transport="stdio",
        command="uvx",
        args=["mcp-server-sqlite", "--db-path", "{path}"],
        note="Query a SQLite database. Requires uv.",
        needs=[{"key": "path", "label": "Database file", "kind": "text", "default": "./data.db"}],
        fetches_on_first_run=True,
    ),
    CatalogEntry(
        key="memory",
        label="Memory (knowledge graph)",
        category="Files & data",
        transport="stdio",
        command="npx",
        args=["-y", "@modelcontextprotocol/server-memory"],
        note="A persistent knowledge graph the agent can write to and query.",
        fetches_on_first_run=True,
    ),
    CatalogEntry(
        key="postgres",
        label="PostgreSQL",
        category="Files & data",
        transport="stdio",
        command="npx",
        args=["-y", "@modelcontextprotocol/server-postgres", "postgresql://localhost/mydb"],
        note="Read-only SQL against Postgres.",
        needs=[{"key": "url", "label": "Connection string", "kind": "text",
                "default": "postgresql://localhost/mydb"}],
        fetches_on_first_run=True,
    ),
    # ---------------------------------------------------------------- web and search
    CatalogEntry(
        key="fetch",
        label="Fetch (web pages)",
        category="Web & search",
        transport="stdio",
        command="uvx",
        args=["mcp-server-fetch"],
        note="Fetch a URL and convert it to markdown for the model. Requires uv.",
        fetches_on_first_run=True,
    ),
    CatalogEntry(
        key="brave-search",
        label="Brave Search",
        category="Web & search",
        transport="stdio",
        command="npx",
        args=["-y", "@modelcontextprotocol/server-brave-search"],
        note="Web and local search. Needs a Brave Search API key (free tier available).",
        needs=[{"key": "env", "env": "BRAVE_API_KEY", "label": "Brave API key", "kind": "token"}],
        fetches_on_first_run=True,
    ),
    CatalogEntry(
        key="tavily",
        label="Tavily Search",
        category="Web & search",
        transport="stdio",
        command="npx",
        args=["-y", "tavily-mcp"],
        note="Search built for agents. Needs a Tavily API key.",
        needs=[{"key": "env", "env": "TAVILY_API_KEY", "label": "Tavily API key", "kind": "token"}],
        fetches_on_first_run=True,
    ),
    # ---------------------------------------------------------------- developer tools
    CatalogEntry(
        key="github",
        label="GitHub",
        category="Developer",
        transport="stdio",
        command="npx",
        args=["-y", "@modelcontextprotocol/server-github"],
        note="Issues, pull requests and repository search. Needs a personal access token.",
        needs=[{"key": "env", "env": "GITHUB_PERSONAL_ACCESS_TOKEN", "label": "GitHub token",
                "kind": "token"}],
        fetches_on_first_run=True,
    ),
    CatalogEntry(
        key="sentry",
        label="Sentry",
        category="Developer",
        transport="http",
        url="https://mcp.sentry.dev/mcp",
        needs=[NEEDS_TOKEN],
        note="Read issues and stack traces for a project.",
    ),
    CatalogEntry(
        key="linear",
        label="Linear",
        category="Developer",
        transport="http",
        url="https://mcp.linear.app/mcp",
        needs=[NEEDS_TOKEN],
        note="Issues and projects.",
    ),
    CatalogEntry(
        key="notion",
        label="Notion",
        category="Developer",
        transport="http",
        url="https://mcp.notion.com/mcp",
        needs=[NEEDS_TOKEN],
        note="Read and write pages and databases.",
    ),
    CatalogEntry(
        key="puppeteer",
        label="Puppeteer (browser automation)",
        category="Developer",
        transport="stdio",
        command="npx",
        args=["-y", "@modelcontextprotocol/server-puppeteer"],
        note="Drive a headless browser: navigate, click, screenshot, evaluate.",
        fetches_on_first_run=True,
    ),
    CatalogEntry(
        key="everything",
        label="Everything (reference test server)",
        category="Developer",
        transport="stdio",
        command="npx",
        args=["-y", "@modelcontextprotocol/server-everything"],
        note="The specification's own test server. Useful to confirm MCP works end to end.",
        fetches_on_first_run=True,
    ),
]


def all_entries() -> list[CatalogEntry]:
    return list(CATALOG)


def by_key(key: str) -> CatalogEntry | None:
    for entry in CATALOG:
        if entry.key == key:
            return entry
    return None


def catalog_payload() -> list[dict]:
    return [entry.to_dict() for entry in CATALOG]


def categories() -> list[str]:
    seen: list[str] = []
    for entry in CATALOG:
        if entry.category not in seen:
            seen.append(entry.category)
    return seen


def resolve(entry: CatalogEntry, values: dict | None = None, name: str = "") -> dict:
    """Turn a catalog entry plus user-supplied values into an mcp_servers.json entry.

    Raises ValueError naming the missing field rather than writing a configuration that cannot work:
    a server silently missing its API key produces a connection error far from the cause.

    Field defaults are applied here, before substitution, so `args=["...", "{path}"]` resolves to the
    default directory when the user leaves the field blank. Leaving the placeholder unsubstituted was a
    real bug: the server received the literal text `{path}` as its argument.
    """
    values = dict(values or {})
    for need in entry.needs:
        key = need.get("key")
        if key and not str(values.get(key) or "").strip() and need.get("default"):
            values[key] = need["default"]

    server: dict = {"name": name or entry.key}

    if entry.transport == "stdio":
        if not entry.command:
            raise ValueError(f"{entry.label} has no command")
        server["command"] = entry.command
        server["args"] = [_substitute(arg, values) for arg in entry.args]
        env: dict = {}
        for need in entry.needs:
            if need.get("kind") == "token" and need.get("env"):
                value = str(values.get(need["key"]) or "").strip()
                if not value:
                    raise ValueError(f"{need['label']} is required for {entry.label}")
                env[need["env"]] = value
        if env:
            server["env"] = env
    else:
        server["url"] = _substitute(entry.url, values) or entry.url
        token = str(values.get("token") or "").strip()
        if token:
            server["token"] = token

    # Required text fields (a path, a connection string) must have a value by now, default included.
    for need in entry.needs:
        if need.get("kind") == "text" and need.get("required", True):
            value = str(values.get(need.get("key")) or "").strip()
            if not value:
                raise ValueError(f"{need['label']} is required for {entry.label}")

    # A leftover placeholder means a field the entry references but never declares, which would be sent
    # to the server as literal text.
    leftover = [arg for arg in server.get("args", []) if "{" in str(arg) and "}" in str(arg)]
    if leftover:
        raise ValueError(f"{entry.label} has an unfilled placeholder: {leftover}")

    return server


def _substitute(text: str, values: dict) -> str:
    result = text
    for key, value in values.items():
        if isinstance(value, str) and value:
            result = result.replace("{" + key + "}", value)
    return result


def missing_fields(entry: CatalogEntry, values: dict | None = None) -> list[str]:
    """Which required fields are still empty, for the UI to show before enabling."""
    values = values or {}
    missing = []
    for need in entry.needs:
        if not need.get("required", True):
            continue
        value = str(values.get(need.get("key", "")) or "").strip()
        if not value and need.get("kind") == "token":
            missing.append(need["label"])
    return missing
