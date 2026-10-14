"""Plugin API: load in-process extensions that add tools and observe hooks.

Skills, hooks and subagents already cover much of what a plugin system provides, but none of them is
a single, documented entry point for third-party Python. This is that entry point.

A plugin is a ``.py`` file (or a package directory) in one of the plugin directories:

    <config dir>/plugins/            user-level, applies everywhere
    <project>/.myriaxon/plugins/      project-level, ships with the repository

A module becomes a plugin by defining any of these, all optional:

    PLUGIN = {"name": "acme", "version": "1.0", "description": "..."}
    def register(api): ...           # add tools and hooks; see PluginAPI below

The ``register`` function receives a :class:`PluginAPI` with:

    api.add_tool(name, description, input_schema, handler, mutating=False)
        handler(tool_input: dict, ctx) -> str | tuple[str, bool]
        `mutating=True` routes the call through the same confirmation/policy gate as built-in
        mutating tools, so a plugin cannot silently bypass a `deny` rule.
    api.on(event, handler)
        event is one of HOOK_EVENTS; handler(*args) is called in-process.

Design decisions worth knowing:

* **Failure is contained.** A plugin that raises on import is recorded and skipped; it never stops the
  agent from starting. Failures are surfaced in the dashboard's Plugins panel rather than only in a log.
* **Plugins run in-process**, unlike shell hooks which spawn a subprocess. That makes them powerful and
  therefore trusted: they are code the user deliberately placed in their own config directory. Nothing
  downloads or auto-installs a plugin.
* **Registration is idempotent per name.** A plugin cannot silently replace a built-in tool; the
  attempt is refused and recorded.
"""
from __future__ import annotations

import importlib.util
import threading
import traceback
import types
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import os

# Events an in-process plugin may observe. These mirror the shell-hook events so a plugin can replace
# a bash hook with something that does not pay a process spawn per tool call.
HOOK_EVENTS = (
    "pre_tool_use",     # (tool_name, tool_input, ctx) -- return a string to BLOCK the call
    "post_tool_use",    # (tool_name, tool_input, result, is_error, ctx)
    "user_prompt",      # (prompt, ctx) -- return a string to replace the prompt
    "agent_start",      # (ctx)
    "agent_stop",       # (ctx)
)


@dataclass
class PluginTool:
    name: str
    description: str
    input_schema: dict
    handler: Callable
    mutating: bool = False
    plugin: str = ""


@dataclass
class LoadedPlugin:
    name: str
    path: str
    version: str = ""
    description: str = ""
    tools: list[str] = field(default_factory=list)
    hooks: list[str] = field(default_factory=list)
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "path": self.path,
            "version": self.version,
            "description": self.description,
            "tools": self.tools,
            "hooks": self.hooks,
            "error": self.error,
            "ok": not self.error,
        }


def plugin_dirs(config_dir: Path, project_root: Path | None = None) -> list[Path]:
    """Where plugins are looked for, in load order (user first, then project).

    Duplicates are removed. Measured on this machine: the user config directory falls back into the
    repository, so `config_dir` and `<project>/.myriaxon` were the same directory and the Plugins panel
    listed the identical path twice -- which reads as though two locations were searched when only one
    was. The same file was also loaded as two layers, briefly.
    """
    candidates = [config_dir / "plugins"]
    if project_root is not None:
        candidates.append(Path(project_root) / ".myriaxon" / "plugins")

    unique: list[Path] = []
    for candidate in candidates:
        duplicate = False
        for already in unique:
            try:
                if os.path.samefile(candidate, already):
                    duplicate = True
                    break
            except OSError:
                # samefile needs both to exist; fall back to a normalised comparison.
                if os.path.normcase(str(candidate)) == os.path.normcase(str(already)):
                    duplicate = True
                    break
        if not duplicate:
            unique.append(candidate)
    return unique


class PluginAPI:
    """The surface handed to a plugin's `register` function.

    Deliberately narrow: adding a tool and observing an event. A plugin gets no handle on the agent,
    the session, or the filesystem beyond what its own handler receives, so a plugin cannot take over
    the turn loop.
    """

    def __init__(self, registry: "PluginRegistry", plugin_name: str) -> None:
        self._registry = registry
        self._plugin = plugin_name

    def add_tool(
        self,
        name: str,
        description: str,
        input_schema: dict,
        handler: Callable,
        mutating: bool = False,
    ) -> None:
        self._registry._add_tool(self._plugin, name, description, input_schema, handler, mutating)

    def on(self, event: str, handler: Callable) -> None:
        self._registry._add_hook(self._plugin, event, handler)

    @property
    def name(self) -> str:
        return self._plugin


class PluginRegistry:
    """Discovers, loads and exposes plugins. One instance per process."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._plugins: dict[str, LoadedPlugin] = {}
        self._tools: dict[str, PluginTool] = {}
        self._hooks: dict[str, list[tuple[str, Callable]]] = {event: [] for event in HOOK_EVENTS}
        self._loaded = False
        # Names a plugin may never take. Set by tool_registry once the built-in tool list is known.
        # Measured failure without this: a plugin that registered `write_file` INTERCEPTED the real
        # one -- same name, same arguments, silently routed to the plugin's handler. Filtering the
        # tool *definitions* was not enough, because dispatch looks the name up in this registry.
        self._reserved: set[str] = set()

    def set_reserved_names(self, names: set[str]) -> None:
        """Declare names that plugins may not claim. Idempotent; called by the tool registry."""
        with self._lock:
            self._reserved = {str(n) for n in names}

    def reserved_names(self) -> set[str]:
        with self._lock:
            return set(self._reserved)

    # -- loading -------------------------------------------------------------------
    def load(self, config_dir: Path, project_root: Path | None = None, force: bool = False) -> list[LoadedPlugin]:
        """Load plugins from every plugin directory. Safe to call repeatedly.

        Reserved (built-in) tool names are established BEFORE any plugin's register() runs, so a
        plugin cannot claim one even momentarily. Setting them lazily on first tool-def build would
        leave a window in which a plugin held the name.
        """
        with self._lock:
            if self._loaded and not force:
                return list(self._plugins.values())
            self._plugins.clear()
            self._tools.clear()
            self._hooks = {event: [] for event in HOOK_EVENTS}
            if not self._reserved:
                # Imported here rather than at module scope: tool_registry imports this module.
                try:
                    from .tool_registry import _builtin_tool_names

                    self._reserved = _builtin_tool_names()
                except Exception:  # noqa: BLE001
                    self._reserved = set()

            seen_names: set[str] = set()
            for directory in plugin_dirs(config_dir, project_root):
                for module_path in self._discover(directory):
                    plugin = self._load_one(module_path)
                    if plugin.name in seen_names and not plugin.error:
                        # Project plugins load after user plugins, so a project can shadow a user
                        # plugin of the same name -- noted rather than merged, because two plugins
                        # registering the same tool name would be ambiguous.
                        plugin.error = f"duplicate plugin name (already loaded from another directory)"
                    seen_names.add(plugin.name)
                    self._plugins[plugin.name] = plugin
            self._loaded = True
            return list(self._plugins.values())

    @staticmethod
    def _discover(directory: Path) -> list[Path]:
        """Plugin entry points: ``*.py`` files and package directories with ``__init__.py``."""
        if not directory.is_dir():
            return []
        found: list[Path] = []
        for child in sorted(directory.iterdir()):
            if child.name.startswith("_"):
                continue
            if child.is_file() and child.suffix == ".py":
                found.append(child)
            elif child.is_dir() and (child / "__init__.py").is_file():
                found.append(child / "__init__.py")
        return found

    def _load_one(self, path: Path) -> LoadedPlugin:
        name = path.parent.name if path.name == "__init__.py" else path.stem
        record = LoadedPlugin(name=name, path=str(path))
        module_name = f"myriaxon_plugin_{name}"

        try:
            if path.name == "__init__.py":
                spec = importlib.util.spec_from_file_location(
                    module_name, path, submodule_search_locations=[str(path.parent)]
                )
            else:
                spec = importlib.util.spec_from_file_location(module_name, path)
            if spec is None or spec.loader is None:
                record.error = "could not build an import spec"
                return record

            module = importlib.util.module_from_spec(spec)
            # Registered before execution so a plugin importing a sibling module works, and so a
            # partially-initialised module is not importable twice.
            import sys

            sys.modules[module_name] = module
            spec.loader.exec_module(module)
        except BaseException as exc:  # noqa: BLE001 -- a broken plugin must not break the agent
            record.error = f"{type(exc).__name__}: {exc}"
            record.hooks.append("import failed")
            return record

        meta = getattr(module, "PLUGIN", None)
        if isinstance(meta, dict):
            record.version = str(meta.get("version") or "")
            record.description = str(meta.get("description") or "")
            declared = str(meta.get("name") or "")
            if declared:
                record.name = declared

        register = getattr(module, "register", None)
        if not callable(register):
            # A plugin with no register() is still listed, with a note: it may exist purely to be
            # imported by another plugin, but the user should be able to see that it contributed
            # nothing.
            record.hooks.append("no register() -- contributed nothing")
            return record

        api = PluginAPI(self, record.name)
        try:
            register(api)
        except BaseException as exc:  # noqa: BLE001
            record.error = f"register() raised {type(exc).__name__}: {exc}"
            return record

        record.tools = sorted(t.name for t in self._tools.values() if t.plugin == record.name)
        record.hooks = sorted({f"{event}:{handler.__name__}" for event in HOOK_EVENTS
                               for owner, handler in self._hooks[event] if owner == record.name})
        return record

    # -- registration (called through PluginAPI) -------------------------------------
    def _add_tool(
        self, plugin: str, name: str, description: str, input_schema: dict, handler: Callable, mutating: bool
    ) -> None:
        if not name or not callable(handler):
            raise ValueError("add_tool requires a name and a callable handler")
        if not isinstance(input_schema, dict):
            raise ValueError("input_schema must be a JSON-schema object")
        if name in self._reserved:
            # Refused outright rather than skipped later: a built-in tool must keep working, and the
            # plugin should learn immediately that its name was rejected.
            raise ValueError(
                f"{name!r} is a built-in tool and cannot be replaced by a plugin. "
                "Choose a different name."
            )
        self._tools[name] = PluginTool(
            name=name, description=description or "", input_schema=input_schema,
            handler=handler, mutating=bool(mutating), plugin=plugin,
        )

    def _add_hook(self, plugin: str, event: str, handler: Callable) -> None:
        if event not in HOOK_EVENTS:
            raise ValueError(f"unknown hook event {event!r}; expected one of {', '.join(HOOK_EVENTS)}")
        if not callable(handler):
            raise ValueError("hook handler must be callable")
        self._hooks[event].append((plugin, handler))

    # -- use -----------------------------------------------------------------------
    def tool_defs(self) -> list[dict]:
        """Anthropic-shaped definitions, matching the rest of the registry.

        Reserved names are excluded here as well as refused at registration, so the tool list never
        advertises a name that dispatch would not route to the plugin.
        """
        return [
            {"name": tool.name, "description": tool.description, "input_schema": tool.input_schema}
            for tool in sorted(self._tools.values(), key=lambda t: t.name)
            if tool.name not in self._reserved
        ]

    def tool_names(self) -> list[str]:
        return sorted(name for name in self._tools if name not in self._reserved)

    def get_tool(self, name: str) -> PluginTool | None:
        """A plugin tool, or None. A reserved (built-in) name is never returned, even if a plugin
        managed to register it before the reserved set was known."""
        if name in self._reserved:
            return None
        return self._tools.get(name)

    def is_mutating(self, name: str) -> bool:
        tool = self._tools.get(name)
        return bool(tool and tool.mutating)

    def call(self, name: str, tool_input: dict, ctx: Any) -> tuple[str, bool]:
        tool = self._tools.get(name)
        if tool is None:
            return f"No plugin tool named {name!r}", True
        try:
            result = tool.handler(tool_input or {}, ctx)
        except BaseException as exc:  # noqa: BLE001
            return f"Plugin tool {name} raised {type(exc).__name__}: {exc}", True
        if isinstance(result, tuple) and len(result) == 2:
            return str(result[0]), bool(result[1])
        return str(result), False

    def run_hooks(self, event: str, *args: Any) -> list[Any]:
        """Call every handler for an event. Returns their results, skipping ones that raise."""
        results = []
        for plugin, handler in list(self._hooks.get(event, [])):
            try:
                results.append(handler(*args))
            except BaseException:  # noqa: BLE001
                # A misbehaving plugin hook must not abort a turn; the failure is reported on stderr
                # with the plugin name so it is traceable without taking the session down.
                import sys

                print(
                    f"[plugin] hook {event} from {plugin!r} raised:\n{traceback.format_exc()}",
                    file=sys.stderr,
                )
        return results

    def first_blocking_result(self, event: str, *args: Any) -> str | None:
        """For pre-tool-use: the first handler that returns a non-empty string blocks the call."""
        for result in self.run_hooks(event, *args):
            if isinstance(result, str) and result.strip():
                return result
        return None

    def describe(self) -> list[LoadedPlugin]:
        """The loaded plugins themselves, so callers can read fields without unwrapping dicts."""
        return list(self._plugins.values())

    @property
    def loaded(self) -> bool:
        return self._loaded

    def summary(self) -> dict:
        """A JSON-serializable view, for the API and the dashboard panel."""
        plugins = [plugin.to_dict() for plugin in self._plugins.values()]
        return {
            "plugins": plugins,
            "count": len(plugins),
            "failed": [p["name"] for p in plugins if not p["ok"]],
            "tools": self.tool_names(),
            "hooks": {event: len(handlers) for event, handlers in self._hooks.items() if handlers},
            "events": list(HOOK_EVENTS),
        }


# Process-wide registry. Plugins are process-level: they register tools and hooks that the tool
# registry and agent loops consult, so one shared instance is the honest model.
_registry = PluginRegistry()
_registry_lock = threading.Lock()


def get_registry() -> PluginRegistry:
    return _registry


def load_plugins(config_dir: Path, project_root: Path | None = None, force: bool = False) -> PluginRegistry:
    with _registry_lock:
        _registry.load(config_dir, project_root, force=force)
    return _registry
