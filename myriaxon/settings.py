import json
from dataclasses import dataclass, field
from pathlib import Path

from .atomic_io import atomic_write_text
from .constants import MODE_AUTO, MODE_MANUAL, PROJECT_SETTINGS_SUBPATH, SYSTEM_SETTINGS_FILE, USER_SETTINGS_FILE

# Built-in profiles, always available even with no settings.json -- override by defining a
# profile with the same name at any layer.
BUILTIN_PROFILES: dict[str, dict] = {
    "fast": {"mode": MODE_AUTO, "yolo": True},
    "careful": {"mode": MODE_MANUAL, "yolo": False},
}


@dataclass
class Settings:
    # Flattened (later-layer-wins) view -- used only for /policy display. Enforcement uses
    # permission_layers instead (see resolve_permission), since flattening loses which layer a
    # rule came from and that matters: a lower-priority layer's specific rule must never be able
    # to silently override a higher-priority layer's wildcard rule.
    permissions: dict[str, str] = field(default_factory=dict)
    # Ordered lowest -> highest priority ([user, project, system], empty layers omitted).
    permission_layers: list[dict[str, str]] = field(default_factory=list)
    enforced_provider: str | None = None
    enforced_mode: str | None = None
    # Named, bundleable mode+yolo combinations (Codex-style approval profiles), applied together
    # via /profile <name> instead of setting /mode and /yolo separately each time.
    profiles: dict[str, dict] = field(default_factory=dict)
    # Personal UI preference -- read from the User layer only in practice (see load_settings),
    # but merged the same way as everything else for simplicity.
    theme: str | None = None
    # Same personal-preference discipline as theme -- vi-style input-line editing in the REPL
    # prompt, persisted via /vim so it doesn't need re-toggling every launch.
    vim_mode: bool = False
    # If True (typically set at the System/org layer), Docker sandboxing for run_bash is
    # mandatory and /sandbox off is refused.
    enforced_sandbox: bool = False
    # Additional directories (outside the project root) that write_file/edit_file may touch --
    # additive across layers (union, not deny-wins-style override), since these only ever grant
    # extra permission, never restrict.
    allowed_write_paths: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)

    def resolve_permission(self, tool_name: str, tool_input: dict | None = None) -> str | None:
        """The decision for one tool call: "allow", "deny", "ask", or None for no opinion.

        Layered first, then most-specific-wins *within* a layer. The outer rule is unchanged: a
        higher-priority layer with any opinion is final, so System can lock everything down with
        `"*": "deny"` and a lower layer cannot quietly re-open one tool. Inside a layer the order is:

          1. ``"tool_name:pattern"``  e.g. ``"run_bash:git *"`` -- matched against the tool's argument
          2. ``"tool_name"``          e.g. ``"run_bash"``
          3. ``"*"``

        That is what makes "allow git, ask for rm" expressible, which a bare boolean was not. The
        pattern matches the tool's primary argument -- `command` for bash, `path` for file tools,
        otherwise the JSON of the whole input -- so a rule can address whichever field it cares about.
        """
        for layer in reversed(self.permission_layers):
            decision = _resolve_in_layer(layer, tool_name, tool_input)
            if decision is not None:
                return decision
        return None


def _rule_argument(tool_name: str, tool_input: dict | None) -> str:
    """The string a ``tool:pattern`` rule is matched against for this call."""
    if not tool_input:
        return ""
    # The field a human would name in a rule. `run_bash` is the case that matters most: "allow git"
    # has to mean the command, not the JSON wrapper around it.
    for key in ("command", "path", "file_path", "pattern", "query"):
        value = tool_input.get(key)
        if isinstance(value, str):
            return value
    try:
        import json as _json

        return _json.dumps(tool_input, default=str, sort_keys=True)
    except (TypeError, ValueError):
        return str(tool_input)


def _resolve_in_layer(layer: dict, tool_name: str, tool_input: dict | None) -> str | None:
    """One layer's opinion, most specific first."""
    if not isinstance(layer, dict):
        return None

    # 1. argument-specific rules, e.g. "run_bash:git *"
    prefix = f"{tool_name}:"
    candidates = [
        (key, value)
        for key, value in layer.items()
        if isinstance(key, str) and key.startswith(prefix)
    ]
    if candidates:
        argument = _rule_argument(tool_name, tool_input)
        # Longest pattern wins: "run_bash:git push *" is more specific than "run_bash:git *", and
        # without this ordering the result would depend on dictionary insertion order.
        for key, value in sorted(candidates, key=lambda kv: len(kv[0]), reverse=True):
            pattern = key[len(prefix):]
            if _pattern_matches(pattern, argument):
                return value

    # 2. tool-wide rule
    if tool_name in layer:
        return layer[tool_name]

    # 3. layer-wide rule
    if "*" in layer:
        return layer["*"]
    return None


def _pattern_matches(pattern: str, argument: str) -> bool:
    """Glob match, with the two conveniences a command rule needs.

    ``*`` is fnmatch's, so ``git *`` matches "git status" and ``*.py`` matches a path. A pattern with
    no wildcard also matches a leading-word form, so ``"git"`` means "the git command" rather than
    "a command whose entire text is exactly git" -- which is what someone writing that rule intends.
    """
    import fnmatch

    if pattern in ("", "*"):
        return True
    if fnmatch.fnmatchcase(argument, pattern):
        return True
    if not any(character in pattern for character in "*?["):
        # Whole-word prefix: "git" matches "git status" but not "gitleaks".
        return argument == pattern or argument.startswith(pattern + " ")
    return False


def _read_settings_file(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        # utf-8-sig: BOM-safe, same discipline as hooks.json/frontmatter parsing elsewhere --
        # Windows tools that write JSON via Set-Content -Encoding utf8 prepend a BOM that plain
        # utf-8 decoding chokes on.
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError):
        return {}


def load_settings(project_root: Path | None = None) -> Settings:
    """Layered governance settings: User -> Project -> System, later layers win. System is
    checked last on purpose -- it represents organization/AI-governance policy (an admin-managed
    settings.json under ProgramData) and must be able to override anything a user or project
    sets, mirroring gemini-cli's own System-settings-as-final-authority design. Never raises -- a
    missing or malformed settings file at any layer is silently skipped, not fatal."""
    project_root = project_root or Path.cwd()
    permissions: dict[str, str] = {}
    permission_layers: list[dict[str, str]] = []
    enforced_provider: str | None = None
    enforced_mode: str | None = None
    profiles: dict[str, dict] = {}
    theme: str | None = None
    vim_mode = False
    enforced_sandbox = False
    allowed_write_paths: list[str] = []
    sources: list[str] = []

    # Each file contributes at most one layer, and a file reached by two of the three paths
    # contributes only once. Measured collision: on a machine whose home directory is not writable,
    # `<project>/.myriaxon/settings.json` was also the user settings path, so the same rules were
    # loaded as both the user layer and the project layer -- harmless for the decision, but it made
    # the reported layer list wrong and would have misled anyone debugging a policy.
    seen_paths: set[Path] = set()

    for path in (USER_SETTINGS_FILE, project_root / PROJECT_SETTINGS_SUBPATH, SYSTEM_SETTINGS_FILE):
        try:
            identity = Path(path).resolve()
        except OSError:
            identity = Path(path)
        if identity in seen_paths:
            continue
        seen_paths.add(identity)

        data = _read_settings_file(path)
        if not data:
            continue
        sources.append(str(path))
        perms = data.get("permissions")
        if isinstance(perms, dict) and perms:
            layer = {str(k): str(v) for k, v in perms.items()}
            permission_layers.append(layer)
            permissions.update(layer)  # flattened view, display only -- see resolve_permission
        if data.get("enforced_provider"):
            enforced_provider = str(data["enforced_provider"])
        if data.get("enforced_mode"):
            enforced_mode = str(data["enforced_mode"])
        file_profiles = data.get("profiles")
        if isinstance(file_profiles, dict):
            profiles.update(file_profiles)
        if data.get("theme"):
            theme = str(data["theme"])
        if data.get("vim_mode") is True:
            vim_mode = True
        if data.get("sandbox") is True:
            enforced_sandbox = True
        extra_paths = data.get("allowed_write_paths")
        if isinstance(extra_paths, list):
            allowed_write_paths.extend(str(p) for p in extra_paths)

    merged_profiles = dict(BUILTIN_PROFILES)
    merged_profiles.update(profiles)

    return Settings(
        permissions=permissions,
        permission_layers=permission_layers,
        enforced_provider=enforced_provider,
        enforced_mode=enforced_mode,
        profiles=merged_profiles,
        theme=theme,
        vim_mode=vim_mode,
        enforced_sandbox=enforced_sandbox,
        allowed_write_paths=allowed_write_paths,
        sources=sources,
    )


def set_user_theme(theme: str) -> None:
    """Persists the theme preference to the User settings layer only -- a personal choice, not
    org policy. Preserves any other keys already in that file."""
    data = _read_settings_file(USER_SETTINGS_FILE)
    data["theme"] = theme
    atomic_write_text(USER_SETTINGS_FILE, json.dumps(data, indent=2))


def set_user_vim_mode(enabled: bool) -> None:
    """Same persistence discipline as set_user_theme -- a personal choice, User layer only."""
    data = _read_settings_file(USER_SETTINGS_FILE)
    data["vim_mode"] = enabled
    atomic_write_text(USER_SETTINGS_FILE, json.dumps(data, indent=2))


def format_settings_summary(settings: Settings) -> str:
    if not settings.sources:
        return (
            "No settings.json found at any layer (user/project/system) -- no policy restrictions "
            "active. See docs for the permissions/enforced_provider/enforced_mode schema."
        )
    lines = [f"Active layers: {', '.join(settings.sources)}"]
    if settings.enforced_provider:
        lines.append(f"Enforced provider: {settings.enforced_provider}")
    if settings.enforced_mode:
        lines.append(f"Enforced mode: {settings.enforced_mode}")
    if settings.enforced_sandbox:
        lines.append("Sandbox: mandatory (Docker) -- /sandbox off is refused")
    if settings.permissions:
        lines.append("Tool permissions:")
        lines += [f"  {tool}: {action}" for tool, action in sorted(settings.permissions.items())]
    else:
        lines.append("Tool permissions: none set (falls back to mode/yolo as usual)")
    if settings.profiles:
        lines.append("Profiles (/profile <name>):")
        lines += [f"  {name}: {cfg}" for name, cfg in sorted(settings.profiles.items())]
    return "\n".join(lines)
