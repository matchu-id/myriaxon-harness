from dataclasses import dataclass
from pathlib import Path

from . import templating
from .constants import PROJECT_COMMANDS_SUBDIR, RESERVED_COMMAND_NAMES, USER_COMMANDS_DIR
from .frontmatter import parse_frontmatter_doc


@dataclass
class Command:
    name: str
    description: str
    body: str
    source: Path


def _parse_command_md(text: str, source: Path, console=None) -> Command | None:
    parsed = parse_frontmatter_doc(text, source)
    if parsed is None:
        return None
    frontmatter, body = parsed
    if "name" not in frontmatter:
        return None

    name = frontmatter["name"]
    if name in RESERVED_COMMAND_NAMES:
        if console is not None:
            console.print(
                f"[warning]Custom command {name!r} in {source} collides with a built-in "
                "command name and will be ignored.[/]"
            )
        return None

    return Command(name=name, description=frontmatter.get("description", ""), body=body, source=source)


def _scan_commands_dir(commands_dir: Path, console=None) -> dict[str, Command]:
    found: dict[str, Command] = {}
    if not commands_dir.is_dir():
        return found

    for entry in sorted(commands_dir.glob("*.md")):
        try:
            text = entry.read_text(encoding="utf-8")
        except OSError:
            continue
        command = _parse_command_md(text, entry, console)
        if command is not None:
            found[command.name] = command

    return found


def discover_commands(project_root: Path | None = None, console=None) -> dict[str, Command]:
    commands = _scan_commands_dir(USER_COMMANDS_DIR, console)

    root = project_root if project_root is not None else Path.cwd()
    commands.update(_scan_commands_dir(root / PROJECT_COMMANDS_SUBDIR, console))

    return commands


def format_command_listing(commands: dict[str, Command]) -> str:
    if not commands:
        return "(no custom commands discovered)"
    return "\n".join(f"/{command.name}: {command.description}" for command in commands.values())


def resolve_input(text: str, commands: dict[str, Command]) -> str:
    """If `text` invokes a discovered custom command (`/name ...rest`), render its body with the
    rest of the line substituted in as args. Otherwise return `text` unchanged."""
    if not text.startswith("/"):
        return text

    first_token, _, rest = text[1:].partition(" ")
    command = commands.get(first_token)
    if command is None:
        return text

    return templating.substitute_args(command.body, rest.strip())
