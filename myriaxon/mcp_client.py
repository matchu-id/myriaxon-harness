import asyncio
import hashlib
import json
import re
import shutil
import threading
from typing import Any

from rich.console import Console
from rich.markup import escape

from .constants import MCP_SERVERS_FILE, MCP_STDIO_CALL_TIMEOUT, MCP_STDIO_CONNECT_TIMEOUT

# A small, deliberately conservative set of well-known MCP servers -- only entries whose
# package name/invocation is stable, official (modelcontextprotocol org) or long-established
# community convention, since a wrong package name here would silently write a broken config
# entry. "prompts"/"env_prompts" are filled in interactively at install time (Prompt.ask in
# cli.py), not hardcoded, since they're per-user (a directory, a personal access token, etc.).
MCP_REGISTRY: dict[str, dict] = {
    "filesystem": {
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem", "{directory}"],
        "prompts": [("directory", "Directory to allow access to")],
        "description": "Read/write access to a specific directory tree, outside the project root.",
        "requires": "npx",
    },
    "github": {
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-github"],
        "env_prompts": [("GITHUB_PERSONAL_ACCESS_TOKEN", "GitHub personal access token", True)],
        "description": "GitHub repo/issue/PR access via the GitHub API.",
        "requires": "npx",
    },
    "memory": {
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-memory"],
        "description": "Persistent knowledge-graph memory across sessions (separate from Myriaxon's own /memory).",
        "requires": "npx",
    },
    "sequential-thinking": {
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-sequential-thinking"],
        "description": "Structured, revisable step-by-step reasoning as an explicit tool call.",
        "requires": "npx",
    },
    "fetch": {
        "command": "uvx",
        "args": ["mcp-server-fetch"],
        "description": "Fetch a URL and convert it to markdown for the model to read.",
        "requires": "uvx",
    },
    "drawio": {
        "command": "npx",
        "args": ["-y", "@drawio/mcp"],
        # Confirmed live on npmjs.org (2026-08-31): "@drawio/mcp" is jgraph's own official
        # package ("Official draw.io MCP server for LLMs"), distinct from an unrelated
        # third-party "io.github.yohasacura/drawio-mcp" (PyPI) that also shows up in the live
        # MCP registry search -- not in the registry itself, hence hardcoded here like the rest.
        "description": "Official draw.io/diagrams.net MCP server -- create and open diagrams from the editor.",
        "requires": "npx",
    },
}


def is_command_available(name: str) -> bool:
    return shutil.which(name) is not None


def save_mcp_config(config: dict) -> None:
    MCP_SERVERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    MCP_SERVERS_FILE.write_text(json.dumps(config, indent=2), encoding="utf-8")


def add_stdio_server(name: str, command: str, args: list[str], env: dict[str, str] | None) -> str | None:
    """Adds/replaces a stdio server entry in mcp_servers.json. Returns an error message on
    failure, None on success. Never connects to the server itself -- McpBridge only connects at
    startup, so this only takes effect on the next launch, same as any other config-file change."""
    config = load_mcp_config()
    existing_names = {s["name"] for s in config["stdio_servers"]}
    entry = {"name": name, "command": command, "args": args}
    if env:
        entry["env"] = env
    if name in existing_names:
        config["stdio_servers"] = [s if s["name"] != name else entry for s in config["stdio_servers"]]
    else:
        config["stdio_servers"].append(entry)
    try:
        save_mcp_config(config)
    except OSError as exc:
        return f"Could not write {MCP_SERVERS_FILE}: {exc}"
    return None


def add_url_server(name: str, url: str, token: str = "", headers: dict | None = None) -> str | None:
    """Same replace-not-duplicate discipline as add_stdio_server, for remote (HTTP/SSE) MCP servers.

    `token` and `headers` exist because virtually every hosted MCP server is authenticated: Figma
    wants a personal access token, Microsoft 365 an OAuth bearer token, GitHub a PAT. Without a place
    to put one, a remote server can be configured and then always fails to connect. An explicit
    `headers` dict takes precedence over `token`, so a server needing a non-bearer scheme (an API key
    in `X-Api-Key`, say) is still expressible.
    """
    config = load_mcp_config()
    entry: dict = {"name": name, "url": url}
    if token:
        entry["token"] = token
    if headers:
        entry["headers"] = {str(k): str(v) for k, v in headers.items()}
    existing_names = {s["name"] for s in config["url_servers"]}
    if name in existing_names:
        # Preserve a previously stored token when the caller does not supply one: the UI never
        # receives the token, so re-saving an edited server would otherwise erase it.
        if not token and not headers:
            previous = next((s for s in config["url_servers"] if s["name"] == name), {})
            for key in ("token", "headers"):
                if key in previous:
                    entry[key] = previous[key]
        config["url_servers"] = [s if s["name"] != name else entry for s in config["url_servers"]]
    else:
        config["url_servers"].append(entry)
    try:
        save_mcp_config(config)
    except OSError as exc:
        return f"Could not write {MCP_SERVERS_FILE}: {exc}"
    return None


def remove_server(name: str) -> bool:
    """Removes a server (stdio or url) by name. Returns True if something was actually removed."""
    config = load_mcp_config()
    before = len(config["stdio_servers"]) + len(config["url_servers"])
    config["stdio_servers"] = [s for s in config["stdio_servers"] if s["name"] != name]
    config["url_servers"] = [s for s in config["url_servers"] if s["name"] != name]
    after = len(config["stdio_servers"]) + len(config["url_servers"])
    if after == before:
        return False
    save_mcp_config(config)
    return True


MCP_REGISTRY_API_BASE = "https://registry.modelcontextprotocol.io/v0"
MCP_REGISTRY_SEARCH_TIMEOUT_SECONDS = 10.0
# The two runtimes Myriaxon's own local MCP_REGISTRY entries already assume -- a package entry
# using anything else (docker, a raw binary, etc.) isn't auto-installable today.
_SUPPORTED_REGISTRY_RUNTIMES = {"npx": "npm", "uvx": "pypi"}


def search_registry(query: str, limit: int = 10) -> list[dict]:
    """Best-effort search against the official, live MCP registry (registry.modelcontextprotocol.io
    -- confirmed live 2026-08-28 via a real GET request) -- covers vastly more servers than the
    small hardcoded MCP_REGISTRY above. Never raises; [] on any failure (network, timeout, schema
    change) since this is a convenience lookup, not something that should ever block /mcp.

    The raw API returns full version history per server (confirmed live: searching "filesystem"
    returned the same server 3x at versions 0.1.2/0.1.3/0.1.5) -- collapsed here to one, the
    latest, entry per server name using the registry's own isLatest flag, since every caller
    wants "what should I install" not a changelog. Over-fetches (limit * 4 raw entries) before
    collapsing so version-history duplication doesn't silently starve the result count below
    what the caller asked for."""
    try:
        import httpx  # lazy -- avoid paying an import cost for users who never touch MCP

        resp = httpx.get(
            f"{MCP_REGISTRY_API_BASE}/servers",
            params={"search": query, "limit": limit * 4},
            timeout=MCP_REGISTRY_SEARCH_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return []

    latest_by_name: dict[str, dict] = {}
    for entry in data.get("servers", []):
        server = entry.get("server")
        if not isinstance(server, dict) or "name" not in server:
            continue
        name = server["name"]
        meta = (entry.get("_meta") or {}).get("io.modelcontextprotocol.registry/official") or {}
        if name not in latest_by_name or meta.get("isLatest"):
            latest_by_name[name] = server
    return list(latest_by_name.values())[:limit]


def find_registry_match(name: str, servers: list[dict]) -> dict | None:
    """Exact match on the full reverse-DNS registry name (e.g. 'io.github.foo/bar') or on just
    the last path segment ('bar') -- lets a user type the short, memorable name they'd expect."""
    name_lower = name.lower()
    for server in servers:
        full_name = server.get("name", "")
        short_name = full_name.rsplit("/", 1)[-1]
        if full_name.lower() == name_lower or short_name.lower() == name_lower:
            return server
    return None


def resolve_registry_stdio_spec(server: dict) -> dict | None:
    """Picks the first package entry Myriaxon actually knows how to run (npm-via-npx or
    pypi-via-uvx) and translates the registry's package schema into a runnable stdio spec.
    Returns None if the server has no such package (docker-only, binary-only, etc.)."""
    for pkg in server.get("packages") or []:
        registry_type = pkg.get("registryType")
        runtime = pkg.get("runtimeHint") or {v: k for k, v in _SUPPORTED_REGISTRY_RUNTIMES.items()}.get(registry_type)
        if runtime not in _SUPPORTED_REGISTRY_RUNTIMES:
            continue
        identifier = pkg.get("identifier")
        if not identifier:
            continue
        version = pkg.get("version")
        target = f"{identifier}@{version}" if version else identifier
        runtime_args = [a["value"] for a in pkg.get("runtimeArguments") or [] if a.get("value")]
        return {
            "command": runtime,
            "args": [*runtime_args, target],
            "env_vars": pkg.get("environmentVariables") or [],
        }
    return None


def resolve_registry_url_spec(server: dict) -> dict | None:
    remotes = server.get("remotes") or []
    if not remotes or not remotes[0].get("url"):
        return None
    return {"url": remotes[0]["url"]}


_INVALID_FUNCTION_NAME_CHARS = re.compile(r"[^A-Za-z0-9_-]")
_MAX_FUNCTION_NAME_LEN = 64


def _normalize_function_name(raw: str) -> str:
    """DeepSeek/OpenAI's function-calling contract restricts tool names to
    ^[A-Za-z0-9_-]{1,64}$. MCP server/tool names have no such restriction -- a long or
    oddly-charactered name would otherwise pass validation fine for Anthropic/Gemini but get
    silently rejected by a real 400 the first time it's sent to an OpenAI-compatible provider.
    Sanitizes disallowed characters and truncates with a deterministic hash suffix (rather than
    bare truncation) so two different raw names that happen to collide after truncation don't
    silently become the same tool name."""
    cleaned = _INVALID_FUNCTION_NAME_CHARS.sub("_", raw)
    if len(cleaned) <= _MAX_FUNCTION_NAME_LEN:
        return cleaned
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]
    keep = _MAX_FUNCTION_NAME_LEN - len(digest) - 1
    return f"{cleaned[:keep]}_{digest}"

# The MCP SDK is imported on first use, not here. This block used to run at module load and cost 1.24 s of
# `myriaxon.cli`'s 1.7 s import -- 107 modules, on every invocation including `--version` and `-p "..."`, for
# users with no MCP server configured. `httpx` above is already lazy for the same reason; the SDK was not.
#
# `_MCP_AVAILABLE` keeps its name and type for anything that reads it, but its meaning is now "resolved and
# present" rather than "present at import time" -- it is False until `_mcp_sdk()` has run once. Code that needs
# the truth should call `mcp_available()`.
_MCP_AVAILABLE = False
_mcp_sdk_cache: tuple | None = None


def _mcp_sdk():
    """Return `(ClientSession, StdioServerParameters, stdio_client)`, or None when the SDK is absent.

    The result is cached, including the failure, so a missing SDK is probed once rather than on every call.
    """
    global _MCP_AVAILABLE, _mcp_sdk_cache
    if _mcp_sdk_cache is None:
        try:
            from mcp import ClientSession
            from mcp.client.stdio import StdioServerParameters, stdio_client

            _mcp_sdk_cache = (ClientSession, StdioServerParameters, stdio_client)
            _MCP_AVAILABLE = True
        except ImportError:
            _mcp_sdk_cache = ()
            _MCP_AVAILABLE = False
    return _mcp_sdk_cache or None


def mcp_available() -> bool:
    """Whether the MCP SDK can be imported. Resolves and caches on first call."""
    return _mcp_sdk() is not None

# Remote transports are imported defensively: the SDK renamed things between 1.x and 2.x, and a
# given install may have one transport but not the other. Both names are accepted so this works
# against either, rather than pinning to one and breaking on the other.
#
#   mcp 1.x:  streamablehttp_client(url, headers=..., timeout=...)
#   mcp 2.x:  streamable_http_client(url, http_client=...)     <- headers go on the client
#
# Streamable HTTP is the current specification; SSE is the older one, still served by several hosted
# servers (Figma's and Microsoft's among them), so both are attempted in that order.
def _load_streamable_http():
    try:
        from mcp.client.streamable_http import streamablehttp_client  # type: ignore

        return streamablehttp_client, "streamablehttp_client"
    except ImportError:
        pass
    try:
        from mcp.client.streamable_http import streamable_http_client  # type: ignore

        return streamable_http_client, "streamable_http_client"
    except ImportError:
        return None, ""


def _load_sse():
    try:
        from mcp.client.sse import sse_client  # type: ignore

        return sse_client, "sse_client"
    except ImportError:
        return None, ""


# Transport support is probed on first use, not at import. Calling these three loaders at module level is
# what kept the whole MCP SDK in the CLI's import path even after `_mcp_sdk()` was made lazy: each loader
# imports from `mcp`, and each ran on every invocation.
_transport_cache: dict | None = None


def _transports() -> dict:
    """Probe which remote MCP transports this install can speak. Cached, including a negative result."""
    global _transport_cache
    if _transport_cache is None:
        streamable_http, streamable_http_name = _load_streamable_http()
        sse, sse_name = _load_sse()
        _transport_cache = {
            "streamable_http": streamable_http,
            "sse": sse,
            "streamable_http_name": streamable_http_name,
            "sse_name": sse_name,
        }
    return _transport_cache


def remote_transport_support() -> dict:
    """What remote MCP transports this install can actually speak.

    Asking this question is what requires probing for the transports, so this is the one call that pays the
    import cost — deliberately, and only when a caller actually asks.
    """
    transports = _transports()
    return {
        "streamable_http": transports["streamable_http"] is not None,
        "sse": transports["sse"] is not None,
        "names": {
            "streamable_http": transports["streamable_http_name"],
            "sse": transports["sse_name"],
        },
    }



def load_mcp_config() -> dict:
    default = {"stdio_servers": [], "url_servers": []}
    if not MCP_SERVERS_FILE.exists():
        return default
    try:
        data = json.loads(MCP_SERVERS_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default
    return {
        "stdio_servers": data.get("stdio_servers", []),
        "url_servers": data.get("url_servers", []),
    }


class _ServerHandle:
    def __init__(self, name, stdio_cm, session_cm, session):
        self.name = name
        self.stdio_cm = stdio_cm
        self.session_cm = session_cm
        self.session = session


def _tool_schema(tool: Any) -> dict:
    """A tool's JSON schema, across SDK versions.

    mcp 2.x renamed the field from `inputSchema` to `input_schema` (the model is now snake_case).
    Reading only the old name raised AttributeError mid-connection -- after the transport had already
    succeeded -- so a rename looked like a failed connection. Both are accepted.
    """
    for attribute in ("input_schema", "inputSchema"):
        schema = getattr(tool, attribute, None)
        if isinstance(schema, dict):
            return schema
    return {"type": "object", "properties": {}}


class McpBridge:
    """Bridges Myriaxon's synchronous agent loop to the async-native `mcp` client library.

    Runs one persistent asyncio event loop in a background thread for the process lifetime —
    MCP stdio servers are long-lived subprocesses, so connections are opened once at startup
    and reused for every tool call rather than reconnected per call.
    """

    def __init__(self, console: Console) -> None:
        self.console = console
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._servers: dict[str, _ServerHandle] = {}
        # Maps a normalized (sanitized/hashed) wire-visible tool name back to the real
        # (server_name, tool_name) pair -- only populated for names that actually needed
        # normalizing, so the common case (short, already-valid names) still round-trips via
        # the plain "mcp__<server>__<tool>".split("__", 2) convention in call_tool().
        self._name_overrides: dict[str, tuple[str, str]] = {}

    def start(
        self, stdio_servers: list[dict] | None = None, url_servers: list[dict] | None = None
    ) -> list[dict]:
        """Connect every configured MCP server and return their tool definitions.

        Both transports are handled here: `url_servers` were previously configurable but never
        connected -- `add_url_server` wrote them to mcp_servers.json and `start()` only iterated
        `stdio_servers`, so a hosted server (Figma, Microsoft 365, GitHub, anything remote) silently
        did nothing. Verified by reading the call path, not by a failing test, which is why it went
        unnoticed.
        """
        stdio_servers = stdio_servers or []
        url_servers = url_servers or []
        if not stdio_servers and not url_servers:
            return []
        if _mcp_sdk() is None:
            self.console.print(
                "[warning]MCP servers are configured in mcp_servers.json but the 'mcp' "
                f"package isn't installed. Install with: {escape('pip install -e .[mcp]')}[/]"
            )
            return []

        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._thread.start()

        tool_defs: list[dict] = []
        targets: list[tuple[str, dict, str]] = (
            [(s["name"], s, "stdio") for s in stdio_servers]
            + [(s["name"], s, "url") for s in url_servers]
        )
        for name, server_conf, kind in targets:
            try:
                coro = self._connect(server_conf) if kind == "stdio" else self._connect_url(server_conf)
                handle, tools = self._run_coro(coro, MCP_STDIO_CONNECT_TIMEOUT)
            except Exception as exc:  # noqa: BLE001
                self.console.print(
                    f"[warning]Failed to start MCP server {escape(repr(name))} ({kind}): "
                    f"{escape(str(exc))}[/]"
                )
                continue

            self._servers[name] = handle
            for tool in tools:
                raw_name = f"mcp__{name}__{tool.name}"
                wire_name = _normalize_function_name(raw_name)
                if wire_name != raw_name:
                    self._name_overrides[wire_name] = (name, tool.name)
                tool_defs.append(
                    {
                        "name": wire_name,
                        "description": tool.description or "",
                        "input_schema": _tool_schema(tool),
                    }
                )

        return tool_defs

    def _run_coro(self, coro, timeout: float):
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=timeout)

    async def _connect(self, server_conf: dict):
        _session_cls, StdioServerParameters, stdio_client = _mcp_sdk()
        params = StdioServerParameters(
            command=server_conf["command"],
            args=server_conf.get("args", []),
            env=server_conf.get("env"),
            cwd=server_conf.get("cwd"),
        )
        stdio_cm = stdio_client(params)
        read_stream, write_stream = await stdio_cm.__aenter__()

        session_cm = _session_cls(read_stream, write_stream)
        session = await session_cm.__aenter__()
        await session.initialize()
        result = await session.list_tools()

        handle = _ServerHandle(server_conf["name"], stdio_cm, session_cm, session)
        return handle, result.tools

    def _auth_headers(self, server_conf: dict) -> dict | None:
        """Headers for a remote server.

        Hosted MCP servers almost all require a bearer token (Figma's personal access token,
        Microsoft's OAuth token, GitHub's PAT). The token is stored in mcp_servers.json next to the
        URL rather than in an environment variable, because the whole point of configuring a server
        from the UI is that it works without also editing the environment.
        """
        headers = dict(server_conf.get("headers") or {})
        token = server_conf.get("token") or server_conf.get("api_key")
        if token and not any(k.lower() == "authorization" for k in headers):
            headers["Authorization"] = f"Bearer {token}"
        return headers or None

    async def _connect_url(self, server_conf: dict):
        """Connect to a remote MCP server over HTTP.

        Tries streamable HTTP first, then SSE. Both are in the specification and servers differ:
        measured that hosted servers still commonly expose only one, so a failure on the first
        transport is not treated as a failure of the server. The error from the last attempt is
        reported, with a note about which transports this install supports, so a missing SDK module
        is distinguishable from a bad URL.
        """
        url = server_conf.get("url")
        if not url:
            raise ValueError(f"MCP server {server_conf.get('name')!r} has no url")
        headers = self._auth_headers(server_conf)
        timeout = float(server_conf.get("timeout") or 30.0)

        attempts: list[tuple[str, str]] = []
        transports: list[tuple[str, Any, str]] = []
        _probed = _transports()
        if _probed["streamable_http"] is not None:
            transports.append(
                ("streamable-http", _probed["streamable_http"], _probed["streamable_http_name"])
            )
        if _probed["sse"] is not None:
            transports.append(("sse", _probed["sse"], _probed["sse_name"]))
        if not transports:
            raise RuntimeError(
                "this install has no remote MCP transport available; "
                "reinstall with: pip install -e .[mcp]"
            )

        # The session class. `_connect` (stdio) unpacks it from `_mcp_sdk()`; this remote path used the same name
        # without defining it, so every hosted MCP server failed with a NameError after its transport opened.
        sdk = _mcp_sdk()
        if sdk is None:
            raise RuntimeError("the MCP SDK is not installed; install it with: pip install myriaxon[mcp]")
        _session_cls = sdk[0]

        for label, factory, function_name in transports:
            try:
                transport_cm = self._open_transport(factory, function_name, url, headers, timeout)
                streams = await transport_cm.__aenter__()
                # streamable HTTP yields (read, write, get_session_id); SSE yields (read, write).
                read_stream, write_stream = streams[0], streams[1]

                session_cm = _session_cls(read_stream, write_stream)
                session = await session_cm.__aenter__()
                await session.initialize()
                result = await session.list_tools()
                handle = _ServerHandle(server_conf["name"], transport_cm, session_cm, session)
                return handle, result.tools
            except Exception as exc:  # noqa: BLE001
                attempts.append((label, f"{type(exc).__name__}: {exc}"))
                continue

        detail = "; ".join(f"{label} -> {error}" for label, error in attempts)
        raise RuntimeError(f"could not connect to {url} ({detail})")

    @staticmethod
    def _open_transport(factory: Any, function_name: str, url: str, headers: dict | None, timeout: float):
        """Call a transport factory with the parameters its SDK version expects.

        The signatures differ meaningfully, so this dispatches on the name rather than guessing:
        mcp 2.x's `streamable_http_client` has no `headers` parameter and takes a prepared
        `httpx2.AsyncClient` instead, while 1.x's `streamablehttp_client` takes headers directly.
        Passing the wrong keyword raised TypeError, which surfaced as "could not connect" and looked
        like the server's fault.
        """
        if function_name == "streamable_http_client":
            from mcp.shared._httpx_utils import create_mcp_http_client

            return factory(
                url,
                http_client=create_mcp_http_client(headers=headers, timeout=timeout),
            )
        kwargs: dict[str, Any] = {"url": url, "timeout": timeout}
        if headers:
            kwargs["headers"] = headers
        return factory(**kwargs)

    def connected_server_names(self) -> set[str]:
        return set(self._servers)

    def call_tool(self, namespaced_name: str, arguments: dict) -> tuple[str, bool]:
        override = self._name_overrides.get(namespaced_name)
        if override is not None:
            server_name, tool_name = override
        else:
            _, server_name, tool_name = namespaced_name.split("__", 2)
        handle = self._servers.get(server_name)
        if handle is None:
            return f"No such MCP server: {server_name}", True

        try:
            result = self._run_coro(handle.session.call_tool(tool_name, arguments), MCP_STDIO_CALL_TIMEOUT)
        except Exception as exc:
            return f"MCP tool call failed: {exc}", True

        text_parts = [block.text for block in result.content if getattr(block, "type", None) == "text"]
        content = "\n".join(text_parts) if text_parts else str(result.content)
        # Redacted AT THE BOUNDARY, before the text can enter the model's context.
        #
        # Found by an audit: this line previously returned the server's output verbatim, and
        # `redaction` was applied only when RECORDING a session or exporting a bundle. So a connector
        # that echoed a token put it into the live transcript -- and therefore into every subsequent
        # request sent upstream, where it is out of reach of any local redaction.
        #
        # Two passes, because neither alone is enough:
        #   * the shape pass catches credentials the harness has never seen (a key pasted into a
        #     ticket the agent just fetched);
        #   * the known-value pass catches everything the harness DOES hold, whatever its shape.
        #     Measured: a generic 68-character gateway token matches no shape pattern at all --
        #     `redaction.redact` leaves it untouched -- so shape matching alone would have missed the
        #     exact credential this harness is configured with.
        content = self._redact(content)
        return content, bool(getattr(result, "isError", False))

    def _redact(self, text: str) -> str:
        """Remove known credentials and credential-shaped strings from tool output. Never raises."""
        if not text:
            return text
        try:
            from . import redaction

            cleaned, _report = redaction.redact(text)
            for secret in self._known_secrets():
                # Only values long enough to be a credential, so a short or empty token cannot turn
                # into a blanket replacement that mangles ordinary output.
                if len(secret) >= 16 and secret in cleaned:
                    cleaned = cleaned.replace(secret, "[redacted-credential]")
            return cleaned
        except Exception:  # noqa: BLE001 - redaction must never break a tool call
            return text

    def _known_secrets(self) -> list[str]:
        """Every credential value this harness holds for its MCP servers.

        Read from the same store the servers were built from, so a token added later is covered
        without a second source of truth to keep in step. Cached because this runs on every tool
        call and the file does not change mid-session in practice; the cache is small and holds
        only what was already on disk.
        """
        cached = getattr(self, "_secret_cache", None)
        if cached is not None:
            return cached
        found: list[str] = []
        try:
            from .constants import MCP_SERVERS_FILE

            if MCP_SERVERS_FILE.exists():
                data = json.loads(MCP_SERVERS_FILE.read_text(encoding="utf-8"))
                for entry in (data.get("stdio_servers") or []) + (data.get("url_servers") or []):
                    if not isinstance(entry, dict):
                        continue
                    token = entry.get("token")
                    if isinstance(token, str) and token.strip():
                        found.append(token.strip())
                    for key, value in (entry.get("env") or {}).items():
                        # Env vars are where a server's own credentials live; the name decides
                        # whether the value is secret, using the same convention `redaction` does.
                        if isinstance(value, str) and value.strip():
                            try:
                                from . import redaction

                                if redaction.is_credential_name(str(key)):
                                    found.append(value.strip())
                            except Exception:  # noqa: BLE001
                                pass
        except Exception:  # noqa: BLE001 - a missing or malformed store means nothing to redact
            pass
        self._secret_cache = found
        return found

    def shutdown(self) -> None:
        if self._loop is None:
            return

        for handle in reversed(list(self._servers.values())):
            try:
                self._run_coro(handle.session_cm.__aexit__(None, None, None), 10)
            except Exception:
                pass
            try:
                self._run_coro(handle.stdio_cm.__aexit__(None, None, None), 10)
            except Exception:
                pass

        self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=10)
