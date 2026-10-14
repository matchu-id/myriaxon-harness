from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.document import Document
from prompt_toolkit.history import FileHistory
from prompt_toolkit.styles import Style

from . import brand
from .commands import Command
from .constants import PROMPT_HISTORY_FILE
from .logging_setup import logger

# Every hardcoded slash command in cli.py's repl() -- kept here as plain data so the
# completer doesn't need to duplicate cli.py's dispatch logic to know what exists.
BUILTIN_COMMANDS: dict[str, str] = {
    "help": "Show all available commands and how to use Myriaxon.",
    "todos": "Show the current task checklist.",
    "?": "Alias for /help.",
    "clear": "Clear the conversation history.",
    "compact": "Summarize the conversation so far to free up context.",
    "commands": "List discovered custom slash commands.",
    "yolo": "Toggle auto-accepting write/edit/bash/notebook confirmations.",
    "thinking": "Toggle showing the model's reasoning trace.",
    "mode": "Set permission mode: manual|auto-edit|plan|auto.",
    "effort": "Set the reasoning/response effort level.",
    "max-tokens": "Set the response token ceiling.",
    "model": "Switch the active model.",
    "reauth": "Re-run the API key/endpoint setup for this session.",
    "provider": "Switch provider (anthropic|openai_compat|openrouter|gemini|deepseek|netra) for this session.",
    "models": "List models available for the active provider.",
    "cost": "Show token usage for this session.",
    "context": "Show a breakdown of what's consuming the context window.",
    "policy": "Show the active layered settings.json policy.",
    "fallback-model": "Set/clear the model to retry on rate-limit errors.",
    "profile": "Apply a named mode+yolo profile (e.g. fast, careful).",
    "theme": "Switch the color theme (default|high-contrast|mono).",
    "export": "Export the conversation transcript to a Markdown file.",
    "memory": "Search past session history (memory search <query>).",
    "sandbox": "Toggle Docker-sandboxed command execution.",
    "undo": "Restore the most recent file checkpoint (no model involved).",
    "rewind": "List/revert to a whole-project + conversation checkpoint (shadow git).",
    "retry": "Re-send the last user message as a fresh turn.",
    "trace": "Toggle inline display of every tool call's raw arguments/result.",
    "diff": "Show a unified diff of every file changed this session.",
    "doctor": "One-shot health check: provider, SDKs, sandbox, settings, checkpoints.",
    "mcp": "Manage MCP servers: /mcp list, list-available, search <query>, install <name>, remove <name>.",
    "plugins": "Show loaded plugins, what they contributed, and any load failures.",
    "loci": "Persistent agents: /loci chat <name> to talk to one continuously, /loci off to return.",
    "mission": "Mission control as text: health, usage, runs, approvals, schedules, trace.",
    "vim": "Toggle vi-style input-line editing (saved for future sessions).",
    "shells": "List background processes started by run_bash.",
    "ground": "Toggle a soft warning when a reply mentions files no tool call this turn touched.",
}


class SlashCommandCompleter(Completer):
    """Only activates when the current input starts with '/' -- commands only ever appear
    at the very start of the line, so this deliberately doesn't try to complete every word
    the way a generic WordCompleter would."""

    def __init__(self, custom_commands: dict[str, Command]) -> None:
        self._descriptions = dict(BUILTIN_COMMANDS)
        self._descriptions.update({name: c.description for name, c in custom_commands.items()})

    def get_completions(self, document: Document, complete_event):
        text = document.text_before_cursor
        if not text.startswith("/"):
            return

        typed = text[1:]
        for name, description in sorted(self._descriptions.items()):
            if name.startswith(typed):
                yield Completion(
                    name,
                    start_position=-len(typed),
                    display=f"/{name}",
                    display_meta=description,
                )


def command_catalogue(custom_commands: dict[str, Command] | None = None) -> list[dict[str, str]]:
    """Every slash command with its description, as plain data.

    Exists so a second front end can offer the same completions without re-typing the list: the
    dashboard had no command suggestions at all, and a duplicated table would have drifted from this
    one within a release. The terminal's completer and the browser's popup now read the same source,
    so a command added here appears in both.
    """
    merged = dict(BUILTIN_COMMANDS)
    for name, command in (custom_commands or {}).items():
        merged[name] = getattr(command, "description", "") or ""
    return [
        {"name": name, "description": description, "slash": f"/{name}"}
        for name, description in sorted(merged.items())
    ]


def _build_history():
    """Prompt history, degrading gracefully when it cannot be persisted.

    Prompt history is a convenience, and `FileHistory.store_string` writes on EVERY accepted line
    from inside prompt_toolkit's key handler -- where an OSError has nothing to catch it and takes
    down the whole event loop. Measured: typing `exit` in a session whose history file was not
    writable produced an unhandled exception in the event loop *instead of exiting*, with the
    traceback pointing at prompt_toolkit rather than at Myriaxon.

    Two defences, because they cover different cases:
      * probe writability up front and use in-memory history when the file is not writable, so the
        failure never reaches the key handler in the first place;
      * a History subclass that swallows OSError, for the case where the file becomes unwritable
        mid-session (permissions change, disk fills, antivirus locks it).
    """
    from prompt_toolkit.history import History

    class _ForgivingFileHistory(FileHistory):
        def store_string(self, string: str) -> None:
            try:
                super().store_string(string)
            except OSError:
                pass  # losing a history entry must never break the session

        def load_history_strings(self):
            try:
                yield from super().load_history_strings()
            except OSError:
                return

    writable = True
    try:
        PROMPT_HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        with PROMPT_HISTORY_FILE.open("a", encoding="utf-8"):
            pass
    except OSError as exc:
        writable = False
        logger.warning(
            "Prompt history is not writable (%s: %s); using in-memory history for this session.",
            exc.__class__.__name__,
            exc,
        )

    if not writable:
        from prompt_toolkit.history import InMemoryHistory

        return InMemoryHistory()
    return _ForgivingFileHistory(str(PROMPT_HISTORY_FILE))


def build_prompt_session(custom_commands: dict[str, Command]) -> PromptSession | None:
    """Best-effort: if construction fails for any reason (no real terminal available, a
    platform quirk, etc.), return None so the caller falls back to a plain input() prompt
    rather than crashing the whole REPL over an optional UX enhancement."""
    try:
        # Built from the brand tokens rather than literals. prompt_toolkit styles the REPL
        # input line and the completion menu independently of Rich, so the palette used to be
        # duplicated here by hand -- and `/theme` could not reach it, leaving the prompt in the
        # old brand after a palette change.
        b = brand
        style = Style.from_dict(
            {
                "prompt": f"bold {b.SIGNAL}",
                # The bottom toolbar's classes. The status line names these instead of embedding Rich
                # markup, which prompt_toolkit does not interpret -- the toolbar used to display the
                # literal tags `[success]…[/]` that rich would have consumed in a console.print.
                "status": b.TEXT_3,
                "status.ok": b.SIGNAL,
                "status.warn": b.AMBER,
                "status.bad": b.RED,
                "completion-menu.completion": f"bg:{b.INK_2} {b.TEXT}",
                "completion-menu.completion.current": f"bg:{b.SIGNAL} {b.SIGNAL_INK}",
                "completion-menu.meta.completion": f"bg:{b.INK_2} {b.TEXT_3}",
                "completion-menu.meta.completion.current": f"bg:{b.SIGNAL} {b.SIGNAL_INK}",
                "scrollbar.background": f"bg:{b.INK_3}",
                "scrollbar.button": f"bg:{b.LINE_2}",
            }
        )
        # FileHistory persists across launches (previously InMemoryHistory, which reset to
        # empty every session). Up/Down arrow recall and Ctrl-R reverse search carry over between
        # runs -- when the file is usable; see _build_history for how an unwritable path degrades
        # to in-memory instead of crashing the prompt.
        return PromptSession(
            completer=SlashCommandCompleter(custom_commands),
            style=style,
            history=_build_history(),
        )
    except Exception:
        logger.exception("Could not start the interactive prompt session; falling back to plain input.")
        return None
