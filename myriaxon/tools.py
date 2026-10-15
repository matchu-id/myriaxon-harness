import contextvars
import difflib
import fnmatch
import os
import platform
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.prompt import Prompt
from rich.text import Text

from . import checkpoints
from .constants import (
    DEFAULT_BASH_TIMEOUT,
    DEFAULT_IGNORE_PATTERNS,
    LEGACY_IGNORE_FILENAME,
    MYRIAXON_IGNORE_FILENAME,
    MODE_AUTO,
    MODE_AUTO_EDIT,
    MODE_PLAN,
    RISKY_BASH_PATTERNS,
)
from .job_object import ProcessJob, job_objects_available
from .scheduler import CANCEL_SCHEDULED_TOOL_DEF, LIST_SCHEDULED_TOOL_DEF, SCHEDULE_PROMPT_TOOL_DEF
from .tool_context import ToolContext

READ_FILE_LINE_CAP = 2000
READ_FILE_BYTE_CAP = 250_000
SEARCH_MATCH_CAP = 200
# Hard budget for glob_search/grep_search -- without `path`, these default to the shell's cwd,
# which may not be the project root (e.g. a parent folder holding several large, unrelated
# projects); confirmed live this made a search feel permanently "stuck" with no bound at all.
SEARCH_TIME_BUDGET_SECONDS = 8.0
SEARCH_SCAN_CAP = 20_000


def _search_truncated_notice(prefix: str | None) -> str:
    notice = (
        f"stopped early after scanning ~{SEARCH_SCAN_CAP:,} entries / {SEARCH_TIME_BUDGET_SECONDS:.0f}s -- "
        "pass a narrower `path` (this defaults to the current directory, which may be larger "
        "than intended) or a more specific pattern for a complete scan"
    )
    return f"{prefix} ({notice})." if prefix else f"... ({notice})"

TOOL_DEFS = [
    {
        "name": "bg_jobs",
        "description": (
            "List background jobs started with run_bash(run_in_background=true), and say whether each has "
            "finished. Use this instead of polling bash_output in a loop: a finished job is also announced "
            "automatically at the next step, exactly once."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "bg_wait",
        "description": (
            "Wait for a background job to finish and return what it produced. Bounded by `timeout` seconds so it "
            "cannot block the turn indefinitely; if the job is still running the reply says so rather than "
            "pretending it finished. Use `promote=true` for the same thing with an explicit result header."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "The job id from run_bash or bg_jobs."},
                "timeout": {
                    "type": "number",
                    "description": "Seconds to wait before reporting that it is still running. Default 30.",
                },
                "promote": {
                    "type": "boolean",
                    "description": "Bring the job's full result into the foreground. Default false.",
                },
            },
            "required": ["id"],
        },
    },
    {
        "name": "read_file",
        "description": "Read a file's contents, optionally a line range. Returns text with 1-indexed line numbers.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "offset": {"type": "integer", "description": "1-indexed line to start from. Optional."},
                "limit": {"type": "integer", "description": "Max lines to return. Optional."},
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "Create or overwrite a file with the given content. Requires user confirmation.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "edit_file",
        "description": (
            "Replace an exact string occurrence in a file. old_string must match exactly and be "
            "unique unless replace_all is true. Requires user confirmation."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old_string": {"type": "string"},
                "new_string": {"type": "string"},
                "replace_all": {"type": "boolean", "description": "Defaults to false."},
            },
            "required": ["path", "old_string", "new_string"],
        },
    },
    {
        "name": "run_bash",
        "description": (
            "Execute a shell command and return stdout, stderr, and exit code. "
            # WHICH shell depends on WHERE it runs, and the old wording named only one of the two.
            # It said "Windows PowerShell on this host" unconditionally -- but a confined Locus runs
            # inside a Debian container where the shell is bash/sh and there is no PowerShell
            # semantics at all. An agent that trusted the description would write `Get-ChildItem` and
            # get "command not found", then reasonably conclude the shell was broken. The environment
            # manifest in the Locus briefing states the full picture; this line states the part that
            # decides how to write the command.
            "On the host that is PowerShell on Windows and /bin/sh elsewhere; inside a sandbox it is "
            "Debian bash/sh, where `pwsh -Command '...'` is available if you need PowerShell. "
            "In an interactive session this asks for confirmation first. In a Locus or another "
            "unattended run it executes directly -- so call it rather than assuming it is unavailable."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "timeout": {"type": "integer", "description": "Seconds. Defaults to 120. Ignored if run_in_background is true."},
                "run_in_background": {
                    "type": "boolean",
                    "description": (
                        "Start the command and return immediately instead of waiting for it to "
                        "finish. Use bash_output to poll for new output and kill_bash to stop it. "
                        "Only reachable from the same agent/session that started it -- a "
                        "background process started inside a task() sub-agent becomes unreachable "
                        "once that sub-agent's turn finishes."
                    ),
                },
            },
            "required": ["command"],
        },
    },
    {
        "name": "bash_output",
        "description": "Get new output (since the last check) from a background command started with run_bash's run_in_background. Read-only, no confirmation.",
        "input_schema": {
            "type": "object",
            "properties": {"id": {"type": "string"}},
            "required": ["id"],
        },
    },
    {
        "name": "kill_bash",
        "description": "Terminate a background command started with run_bash's run_in_background. No confirmation.",
        "input_schema": {
            "type": "object",
            "properties": {"id": {"type": "string"}},
            "required": ["id"],
        },
    },
    {
        "name": "glob_search",
        "description": "Find files matching a glob pattern (e.g. '**/*.py'), sorted by modification time descending.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "path": {"type": "string", "description": "Directory to search from. Defaults to cwd."},
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "grep_search",
        "description": "Search file contents for a regex pattern under a directory tree.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "path": {"type": "string", "description": "Directory to search from. Defaults to cwd."},
                "glob": {"type": "string", "description": "Optional filename filter, e.g. '*.py'."},
                "case_insensitive": {"type": "boolean"},
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "git_status",
        "description": "Show the working tree status (git status --porcelain). Read-only, no confirmation. Returns a clear message if the directory isn't a git repository.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Directory to run in. Defaults to cwd."},
            },
        },
    },
    {
        "name": "git_diff",
        "description": "Show a diff of working tree (or staged) changes. Read-only, no confirmation. Returns a clear message if the directory isn't a git repository.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File or directory to diff. Defaults to the whole working tree."},
                "staged": {"type": "boolean", "description": "Show staged (--cached) changes instead of unstaged. Defaults to false."},
            },
        },
    },
    SCHEDULE_PROMPT_TOOL_DEF,
    LIST_SCHEDULED_TOOL_DEF,
    CANCEL_SCHEDULED_TOOL_DEF,
]

GIT_DIFF_BYTE_CAP = 100_000

_RISKY_BASH_RE = re.compile("|".join(RISKY_BASH_PATTERNS), re.IGNORECASE)


def _is_risky_bash(command: str) -> bool:
    return bool(_RISKY_BASH_RE.search(command))


def shell_may_run(*, confined: str | Path | None = None, sandbox_requested: bool = False) -> tuple[bool, str]:
    """Whether a SHELL COMMAND may run here. Returns (allowed, reason-when-not).

    ONE DECISION, NAMED ONCE, because the alternative has already failed twice in this repository and both
    failures were the same shape -- a control that held on the path it was written for and not on another path
    that does the same thing:

      * `run_bash` refused a shell to a confined Locus unless a real sandbox was available. A **Locus
        condition**, which also runs a shell, consulted none of that and executed freely. Verified: a condition
        whose command was `echo pwned > <path>` wrote the file, while the identical string through `run_bash`
        was refused.
      * The confinement gate for file tools lives in `_resolve`, and `member_workspace()` returned a path inside
        the directory that gate was told to trust.

    Neither was a missing check in a place somebody forgot to look. Both were a check that existed in one of two
    places that needed it, which is what duplicated decisions produce. So this function is the answer to "may a
    shell run here", and callers ask it rather than re-deriving it.

    WHAT IT DOES NOT DECIDE: whether the action is *wanted*. `confirm_action` still owns mode, yolo, governance
    policy and the prompt; this owns only the question that is identical on every path -- is a shell
    COMPATIBLE with the isolation this code is running under.

    `confined` is the Locus workspace, or None for a session. A session is unconfined by construction: the
    person is present and `confirm_action` will ask them. A confined Locus has nobody to ask, so a shell is
    permitted only when a real sandbox is available to enforce the isolation that was requested -- otherwise it
    would run with the full rights of this process under the name of isolation.
    """
    if not confined:
        # A session, with a person present. The prompt is the control, and `confirm_action` owns it.
        return True, ""

    if sandbox_requested:
        if docker_available():
            return True, ""
        if windows_sandbox_available():
            return True, ""

    return False, (
        "a shell command cannot be confined to this Locus's workspace, and no sandbox is available to isolate "
        "it. File tools are unrestricted inside the workspace; a shell would run with the full rights of the "
        "account that started Myriaxon, which is not the isolation this Locus was given.\n"
        "Install Docker Desktop (or enable Windows Sandbox) and set `sandbox_enabled` for Locus runs, or use "
        "the file tools, which ARE confined."
    )


def confirm_action(
    console: Console, panel: Panel, ctx: ToolContext, tool_name: str, command: str | None = None,
    tool_input: dict | None = None,
) -> tuple[bool, str | None]:
    """Returns (proceed, blocked_reason).

    If blocked_reason is set, proceed is always False and no confirmation prompt is ever shown
    (Plan mode: nothing runs, full stop). Otherwise mode/yolo may auto-approve without a prompt,
    or fall through to the normal interactive y/n gate.

    `tool_input` is the call's arguments, used so argument-aware permission rules
    (`"run_bash:git *"`) can be evaluated. It defaults to None because some callers only have the
    command string; in that case a bash rule still matches, since `command` is folded in below.
    """
    if tool_input is None and command is not None:
        tool_input = {"command": command}
    if ctx.mode == MODE_PLAN:
        return False, (
            "Blocked: currently in Plan mode. Describe your plan in text instead of calling "
            "this tool -- the user needs to switch modes (e.g. /mode auto-edit) before any "
            "edits or commands can run."
        )

    # A confined Locus. Checked BEFORE yolo and mode, and before any prompt, because the failure this
    # fixes is not "the wrong answer was given" -- it is that there was nobody to give one.
    #
    # Measured: an unattended Locus ran `run_bash`, printed "Proceed? [y/n]" to a console with no
    # input, retried, and died on "invalid message format" after 235 seconds with an empty workspace.
    # Every mutating tool was effectively unavailable to it, which is why a locus could only ever
    # produce the one artifact it had been given.
    #
    # `run_bash` is the exception, and deliberately: a shell command is not confined by the workspace
    # path check that guards the file tools, so auto-approving it would grant the whole machine under
    # the name of isolation. It is allowed only when a real sandbox is available to enforce the
    # isolation the owner actually asked for.
    if getattr(ctx, "locus_workspace", ""):
        if tool_name != "run_bash":
            return True, None
        # The shell question is asked of `shell_may_run`, not answered again here. See its docstring for the
        # two failures that made one shared decision necessary.
        allowed, reason = shell_may_run(
            confined=ctx.locus_workspace,
            sandbox_requested=bool(getattr(ctx, "sandbox_enabled", False)),
        )
        if allowed:
            return True, None
        return False, f"Blocked: {reason}"

    # Governance policy (settings.json permissions) is checked before yolo/mode -- "deny" must
    # hold even if the user has yolo on, and "allow" is an explicit policy decision, not just a
    # personal convenience toggle. The tool input is passed so argument-aware rules
    # ("run_bash:git *") can be evaluated; a rule with no pattern behaves exactly as before.
    if ctx.settings is not None:
        rule = ctx.settings.resolve_permission(tool_name, tool_input)
        if rule == "deny":
            return False, (
                f"Blocked by policy: '{tool_name}' is denied by the active settings.json "
                "permission rules. This cannot be overridden by yolo mode or the user's own "
                "confirmation -- it requires a settings.json change."
            )
        if rule == "allow":
            return True, None
        if rule == "ask":
            # An explicit "ask" must keep asking even with yolo on -- otherwise the rule could not
            # express "auto-approve everything except this", which is most of its value.
            console.print(panel)
            proceed = Prompt.ask("Proceed?", choices=["y", "n"], default="n", console=console) == "y"
            return proceed, None

    if ctx.yolo:
        return True, None

    if ctx.mode == MODE_AUTO_EDIT and tool_name != "run_bash":
        return True, None

    if ctx.mode == MODE_AUTO and (tool_name != "run_bash" or not (command and _is_risky_bash(command))):
        return True, None

    console.print(panel)
    proceed = Prompt.ask("Proceed?", choices=["y", "n"], default="n", console=console) == "y"
    return proceed, None


# The directory a confined Locus may touch, or None for an ordinary turn.
#
# A contextvar rather than a module global, and rather than a parameter threaded through every tool.
# A global would leak between concurrent runs -- the dashboard can be running a Locus while the
# terminal runs a session, and in one process those would share it. A parameter would have to be added
# to `read_file`, `write_file`, `edit_file`, `grep_search`, `glob_search` and `git_diff`, and a
# boundary that must be remembered at seven call sites is a boundary that will be forgotten at one of
# them. Every path-taking tool funnels through `_resolve`, so the check lives there and applies to all
# of them by construction.
_LOCUS_WORKSPACE: contextvars.ContextVar[Path | None] = contextvars.ContextVar(
    "myriaxon_locus_workspace", default=None
)


def confine_to(path: str | Path | None):
    """Context manager: every path resolved inside it must stay under `path`.

    Returns a token-restoring object, so nesting and exceptions both restore the previous value.
    """
    return _LOCUS_WORKSPACE.set(Path(path).resolve() if path else None)


def confined_workspace() -> Path | None:
    """The active confinement, or None. Exposed for tests and for a tool that wants to explain it."""
    return _LOCUS_WORKSPACE.get()


# Directories a confined Locus may reach OUTSIDE its own workspace. Level 4.12.
#
# This exists because the swarm board and the collective memory live under the config directory, which is
# outside every member's workspace -- so `_resolve` refused them and a confined Locus could not use the board
# at all. Measured: `board inside l1 workspace: NO`.
#
# THE SHAPE OF THE FIX MATTERS, and the obvious alternative is worse. Widening the boundary to the config
# directory would give every member read/write access to `sessions/`, `audit.jsonl`, `config.json` and every
# PEER's workspace -- three separate confinements destroyed to solve one problem. Mounting the board inside
# each workspace would give a different private copy to each member, so two members would share nothing while
# appearing to share everything.
#
# An explicit allowlist does neither: a NAMED directory, granted deliberately, recorded here so it can be
# audited and refused. Adding to this tuple is the only way to grant anything, which makes the whole set of
# exceptions visible in one place -- the property a widened boundary cannot have.
_SHARED_GRANTS: list[Path] = []
_SHARED_GRANT_NAMES: dict[str, Path] = {}


def grant_shared(name: str, path: str | Path | None) -> None:
    """Allow confined Loci to reach `path`, recording it under `name`.

    Idempotent, and called with `None` it revokes. A grant with no path is the unconfined case -- there is
    nothing to allow because nothing is being refused.
    """
    if path is None:
        _SHARED_GRANT_NAMES.pop(name, None)
        _refresh_grants()
        return
    try:
        _SHARED_GRANT_NAMES[name] = Path(path).resolve()
    except OSError:
        return
    _refresh_grants()


def _refresh_grants() -> None:
    _SHARED_GRANTS[:] = sorted(set(_SHARED_GRANT_NAMES.values()))


def shared_grants() -> dict[str, str]:
    """The current grants, for a diagnostic. Names a reader can check."""
    return {name: str(path) for name, path in sorted(_SHARED_GRANT_NAMES.items())}


class WorkspaceEscape(PermissionError):
    """Raised when a confined Locus reaches outside its workspace."""


def _resolve(path_str: str) -> Path:
    """Resolve a tool's path argument, refusing anything outside a confined Locus's workspace.

    `resolve()` is applied BEFORE the containment check on purpose: it collapses `..` and follows
    symlinks, so `workspace/../../etc/passwd` and a symlink pointing out of the tree are both caught.
    Comparing unresolved strings would pass both, which is the usual way a confinement like this is
    defeated.

    Explicitly granted shared directories (`grant_shared`) are allowed through. The check is on the RESOLVED
    path, so a symlink inside a granted directory pointing elsewhere is still refused -- granting a directory
    grants that directory, not whatever it happens to link to.
    """
    path = Path(path_str).expanduser().resolve()
    boundary = _LOCUS_WORKSPACE.get()
    if boundary is not None:
        allowed = False
        try:
            path.relative_to(boundary)
            allowed = True
        except ValueError:
            for granted in _SHARED_GRANTS:
                try:
                    path.relative_to(granted)
                    allowed = True
                    break
                except ValueError:
                    continue
        if not allowed:
            shared = (" Shared directories you CAN reach: " + ", ".join(str(p) for p in _SHARED_GRANTS)
                      if _SHARED_GRANTS else "")
            raise WorkspaceEscape(
                f"{path} is outside this Locus's workspace ({boundary}). A confined Locus can only "
                f"read and write inside its own folder -- that confinement is what lets it run "
                f"without asking for approval. Work that needs files elsewhere has to be brought in "
                f"first, or the Locus needs to be given a workspace that contains them.{shared}"
            ) from None
    return path


def _read_file(tool_input: dict) -> tuple[str, bool]:
    path = _resolve(tool_input["path"])
    if not path.exists():
        return f"File not found: {path}", True
    if not path.is_file():
        return f"Not a file: {path}", True

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"Could not read {path}: {exc}", True

    lines = text.splitlines()
    offset = max(1, tool_input.get("offset", 1))
    limit = tool_input.get("limit", READ_FILE_LINE_CAP)
    limit = min(limit, READ_FILE_LINE_CAP)
    selected = lines[offset - 1 : offset - 1 + limit]

    numbered = "\n".join(f"{i:>6}\t{line}" for i, line in enumerate(selected, start=offset))
    if len(numbered.encode("utf-8", errors="replace")) > READ_FILE_BYTE_CAP:
        numbered = numbered.encode("utf-8", errors="replace")[:READ_FILE_BYTE_CAP].decode(
            "utf-8", errors="ignore"
        ) + "\n... (truncated)"
    return numbered, False


def _write_boundary_violation(path: Path) -> str:
    return (
        f"Blocked: {path} is outside the project directory ({Path.cwd()}) -- write_file and "
        "edit_file are restricted to the project root for safety, the same protection a real "
        "sandbox's mount boundary would give for free. If this path genuinely needs to be "
        "writable, add it to settings.json's allowed_write_paths."
    )


def _is_within_write_boundary(path: Path, ctx: ToolContext) -> bool:
    """Real, enforced boundary (not a heuristic) for write_file/edit_file -- unlike run_bash,
    these tools take an explicit target path as an argument, so it can be checked directly
    rather than needing process-level sandboxing. Deliberately does NOT cover run_bash: an
    arbitrary shell command has no single inspectable "target path" to validate against, so
    enforcing a real filesystem boundary there needs actual sandboxing (Docker/Windows Sandbox),
    not a path check.

    A confined Locus's workspace counts as inside, and it is checked FIRST because for a Locus it is
    the correct boundary. This was found by running a real Locus, not by reasoning: its workspace is
    `~/.myriaxon/loci/<id>`, which is OUTSIDE the project root, so this check rejected every write it
    attempted. Measured end to end -- `dispatch_tool("write_file", ...)` returned "Blocked: ... is
    outside the project dir", the workspace stayed EMPTY after a 30-minute run, and the Locus failed.
    Its confinement had already been established and enforced by `_resolve`, so allowing it here is
    not a hole: the path has passed the stricter Locus check immediately above in the same call.
    """
    try:
        resolved = path.resolve()
        root = Path.cwd().resolve()
    except OSError:
        return False
    boundary = _LOCUS_WORKSPACE.get()
    if boundary is not None:
        # Deliberately inside the try's `resolved`, so a symlinked workspace cannot be used to reach
        # outside: `_resolve` already collapsed this path, and `relative_to` compares the collapsed
        # forms.
        if resolved == boundary or resolved.is_relative_to(boundary):
            return True
    if resolved == root or resolved.is_relative_to(root):
        return True
    if ctx.settings is not None:
        for allowed in ctx.settings.allowed_write_paths:
            try:
                allowed_resolved = Path(allowed).expanduser().resolve()
            except OSError:
                continue
            if resolved == allowed_resolved or resolved.is_relative_to(allowed_resolved):
                return True
    return False


def render_diff(console: Console, path: Path, old_text: str, new_text: str) -> None:
    diff_lines = list(
        difflib.unified_diff(
            old_text.splitlines(keepends=True),
            new_text.splitlines(keepends=True),
            fromfile=str(path),
            tofile=str(path),
        )
    )
    body = []
    for line in diff_lines:
        rendered = escape(line.rstrip())
        if line.startswith("+") and not line.startswith("+++"):
            body.append(f"[diff.add]{rendered}[/]")
        elif line.startswith("-") and not line.startswith("---"):
            body.append(f"[diff.remove]{rendered}[/]")
        else:
            body.append(f"[diff.context]{rendered}[/]")
    console.print(Panel("\n".join(body) or "[dim](no textual diff)[/]", title=f"[path]{escape(str(path))}[/]"))


def _write_file(tool_input: dict, ctx: ToolContext) -> tuple[str, bool]:
    console = ctx.console
    path = _resolve(tool_input["path"])
    if not _is_within_write_boundary(path, ctx):
        return _write_boundary_violation(path), True

    new_content = tool_input["content"]
    old_content = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""

    render_diff(console, path, old_content, new_content)
    panel = Panel(
        f"Write {len(new_content)} bytes to [path]{escape(str(path))}[/]?",
        style="tool.confirm.question",
    )
    proceed, blocked = confirm_action(console, panel, ctx, "write_file")
    if blocked:
        return blocked, False
    if not proceed:
        return "User declined to run this tool call.", False

    path.parent.mkdir(parents=True, exist_ok=True)
    checkpoints.snapshot_and_write(path, new_content, "write_file", ctx)
    return f"Wrote {len(new_content)} bytes to {path}" + _diagnostic_suffix(path), False


def _edit_file(tool_input: dict, ctx: ToolContext) -> tuple[str, bool]:
    console = ctx.console
    path = _resolve(tool_input["path"])
    if not _is_within_write_boundary(path, ctx):
        return _write_boundary_violation(path), True
    if not path.exists():
        return f"File not found: {path}", True

    old_string = tool_input["old_string"]
    new_string = tool_input["new_string"]
    replace_all = tool_input.get("replace_all", False)

    content = path.read_text(encoding="utf-8", errors="replace")

    # The exactly-once guarantee comes from `patching`, not from an inline count, so that every caller of a
    # targeted edit shares one implementation of it. Two things the inline version got wrong:
    #
    #   * an EMPTY `old_string` matches everywhere, so `content.replace("", new)` would splice `new_string` in
    #     front of every character in the file. `count == 0` is false for an empty string, so the guard below
    #     never fired for it. `patching` refuses it by name.
    #   * the "not found" message gave no reason. `patching` reports a near-miss caused by indentation or
    #     trailing whitespace, which is the actual cause most of the time and saves a whole round trip.
    #
    # `replace_all` keeps the inline path deliberately: replacing every occurrence is the opposite intent, and
    # routing it through a exactly-once guard would mean defeating the guard rather than using it.
    if not replace_all:
        from . import patching

        prepared = patching.prepare(path, old_string)
        if not prepared.ok:
            joined = "\n".join(prepared.errors)
            # `patching` refuses a non-unique span without offering the escape hatch this tool has, and the
            # caller needs to know it exists or it will keep adding context until the span is unique by luck.
            if "must match exactly once" in joined:
                joined += " (Or pass replace_all=true to replace every occurrence.)"
            return joined, True
        # Read through `patching`'s strict decode: the write must be made against the exact bytes the span was
        # matched in. The `errors="replace"` decode above is only for rendering a diff of a file that is not
        # valid UTF-8 -- writing that lossy version back would corrupt the file.
        content = path.read_text(encoding="utf-8")
        new_content = content.replace(old_string, new_string, 1)
    else:
        count = content.count(old_string)
        if count == 0:
            return "old_string not found in file — no changes made. Try a different match.", True
        new_content = content.replace(old_string, new_string)

    render_diff(console, path, content, new_content)
    panel = Panel(f"Apply this edit to [path]{escape(str(path))}[/]?", style="tool.confirm.question")
    proceed, blocked = confirm_action(console, panel, ctx, "edit_file")
    if blocked:
        return blocked, False
    if not proceed:
        return "User declined to run this tool call.", False

    checkpoints.snapshot_and_write(path, new_content, "edit_file", ctx)
    return f"Edited {path}" + _diagnostic_suffix(path), False


def _diagnostic_suffix(path: Path) -> str:
    """Append what the edit broke, if anything, to a write/edit tool result.

    ## Why this exists

    Opencode queries a language server after every `edit`/`write`/`apply_patch`. Myriaxon had nothing, so **a change
    that compiles nowhere looked exactly like a change that works** -- the only signal was whatever test the agent
    chose to run next, and it had no reason to run one.

    ## The three properties that matter

    **It never changes the success of the write.** The edit succeeded; a diagnostic is information about the new
    state of the file, not a veto. Blocking here would be wrong about half of legitimate work-in-progress, and
    Opencode appends and continues for the same reason.

    **It never fails the write.** Every failure mode -- no checker, a timeout, a spawn error -- is caught and either
    omitted or reported as a note. A post-edit convenience that can break an edit is worse than no convenience.

    **It always says which tier ran.** "No problems found" from a syntax check is a much weaker claim than the same
    words from a type checker, and a model that cannot tell them apart will treat the first as proof. The report's
    own heading carries that.
    """
    try:
        from . import diagnostics

        report = diagnostics.check([path], Path.cwd())
        rendered = report.render()
        if not rendered:
            return ""
        return "\n\n" + rendered
    except Exception:  # noqa: BLE001
        # Deliberately silent: this is additive information, and a diagnostic failure must never obscure a write
        # that actually happened.
        return ""


def shell_argv(command: str) -> list[str]:
    if platform.system() == "Windows":
        return ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command]
    return ["/bin/sh", "-c", command]


SANDBOX_DOCKER_IMAGE = "mcr.microsoft.com/powershell:latest"

# Environment override for the sandbox image.
#
# The PowerShell image was chosen so command syntax stays identical to the unsandboxed path, and it
# is a good default: `pwsh` plus coreutils, nothing else. Measured, that also means NO python, git,
# node, curl or package manager -- so a confined agent can write and edit files and run shell scripts,
# and cannot build anything. That is the difference between a locus that writes a poster and one that
# builds a project, and hardcoding it made the richer image unreachable without editing the source.
#
# `MYRIAXON_SANDBOX_IMAGE` takes any image with a `pwsh` entrypoint (a `Dockerfile` in `docker/`
# builds one with python, git and node on top of this base). Deliberately an environment variable
# rather than a settings.json entry: it selects the CONTAINER the agent runs in, which is a deployment
# decision, and a per-project file should not be able to change what isolation an agent gets.
SANDBOX_IMAGE_ENV = "MYRIAXON_SANDBOX_IMAGE"


def sandbox_image() -> str:
    """The container image for sandboxed shell commands.

    Read per call rather than captured at import: a process that changes the variable (a test, a
    wrapper script) must not keep running the old image, and the cost is a dictionary lookup.
    """
    return (os.environ.get(SANDBOX_IMAGE_ENV) or "").strip() or SANDBOX_DOCKER_IMAGE


def _wsl_running_distros() -> list[str]:
    """Distributions that are ALREADY running, or [].

    The distinction this draws is the whole point, and it was learned the hard way: asking
    `wsl -d Ubuntu -- docker info` when Ubuntu is stopped STARTS UBUNTU, and on Windows that means a
    window appears. Measured consequence -- a scheduled task probing for Docker every five minutes
    made a WSL window pop up on the user's screen every five minutes.

    A background capability check must not have a visible side effect, so the probe only interrogates
    a distribution that is already up. If WSL is stopped, Docker is reported unavailable for this
    process and nothing is started; the next run after the user opens WSL themselves finds it at once.
    """
    if platform.system() != "Windows":
        return []
    try:
        completed = subprocess.run(
            ["wsl", "--list", "--quiet", "--running"],
            capture_output=True, timeout=30, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    raw = completed.stdout or b""
    if b"\x00" in raw:                        # UTF-16LE
        try:
            text = raw.decode("utf-16-le", "replace")
        except Exception:  # noqa: BLE001
            text = raw.decode("utf-8", "replace")
    else:
        text = raw.decode("utf-8", "replace")
    names: list[str] = []
    for line in text.splitlines():
        name = line.strip().lstrip("*").strip()
        # A run with nothing up prints a sentence, not an empty result, so the notice has to be
        # filtered or it becomes a "distribution" the probe then tries to launch by name.
        if not name or "no distributed" in name.lower() or "no installed" in name.lower():
            continue
        names.append(name)
    return names


def _wsl_distros() -> list[str]:
    """Every installed WSL distribution, or [] on a machine without WSL.

    `wsl --list --quiet` emits UTF-16LE with a BOM on Windows, which is why the noise is stripped
    rather than trusted: a naive decode yields 'U\\x00b\\x00u\\x00n\\x00t\\x00u\\x00' and every
    comparison against a distro name then fails silently.
    """
    if platform.system() != "Windows":
        return []
    try:
        completed = subprocess.run(
            ["wsl", "--list", "--quiet"], capture_output=True, timeout=30, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return []
    raw = completed.stdout or b""
    if b"\x00" in raw:                        # UTF-16LE
        try:
            text = raw.decode("utf-16-le", "replace")
        except Exception:  # noqa: BLE001
            text = raw.decode("utf-8", "replace")
    else:
        text = raw.decode("utf-8", "replace")
    return [line.strip().lstrip("*").strip() for line in text.splitlines() if line.strip()]


def environment_manifest() -> str:
    """A plain-language statement of the environment a shell command will run in.

    Built because an agent that does not know its environment reasons about it instead of testing it.
    Measured across six benchmark runs: a Locus was asked to fix a bug and prove it by running the
    test, and instead spent every step announcing that "shell execution is blocked (no sandbox)" --
    while `run_bash` was present, permitted and working. Two attempted fixes (a briefing rule and a
    tool-description rewrite) both failed, because the agent was not short of permission, it was
    short of FACTS about where it was running.

    Everything here is a fact the harness already knows at build time, so stating it costs one prompt
    block and removes the need to guess. Deliberately flat and specific -- "bash, not PowerShell",
    "no network" -- because the failures it prevents are all cases of an agent assuming the wrong
    shell or assuming it can download something.
    """
    if not docker_available():
        return (
            "Shell commands run DIRECTLY ON THE HOST. The shell is PowerShell on Windows and "
            "/bin/sh elsewhere. There is no sandbox: commands run with the full rights of the "
            "account that started Myriaxon, and the host's filesystem and network are reachable."
        )
    return (
        f"Shell commands run inside a THROWAWAY CONTAINER ({sandbox_image()}), not on the host.\n"
        "  OS and shell : Debian Linux. `sh` and `bash` are the default shells; `pwsh` is also\n"
        "                 installed. Write POSIX shell syntax for pipelines (|, &&, $VAR), NOT\n"
        "                 PowerShell -- though `pwsh -Command '...'` works if you want PowerShell.\n"
        "  Network      : NONE. `--network none` is the isolation. curl and wget exist but cannot\n"
        "                 reach anything, and nothing can be installed at run time. Whatever is\n"
        "                 baked into the image is all there is.\n"
        "  Filesystem   : only the workspace is mounted, at /workspace. The host's other paths are\n"
        "                 not visible inside, so do not reference C:\\... from a shell command.\n"
        "  Language     : python3 (PEP-standard library plus numpy, pandas, matplotlib, Pillow),\n"
        "                 node, npm, gcc, g++, make, git.\n"
        "  Tools        : bash, jq, sed, awk, grep, tar, zip, unzip, sqlite3, pandoc, gnuplot,\n"
        "                 graphviz `dot`, ImageMagick `convert`, ssh, rsync, diff, patch, file, bc.\n"
        "  NOT present  : docker, java, go, rustc, ruby, php, R, a browser, ffmpeg, LaTeX.\n"
        "  Every command starts a NEW container: files survive in /workspace, but installed\n"
        "  packages, environment variables and background processes do not."
    )


def _docker_in_wsl(distros: list[str] | None = None) -> list[str] | None:
    """The argv prefix that runs Docker inside WSL, or None when WSL has no Docker.

    Why this exists: Docker Desktop installs its engine inside WSL and only puts a shim on the
    Windows PATH, so `shutil.which("docker")` is the wrong question on a machine like that --
    measured on this host, Docker 29.8.2 with a live daemon was running in Ubuntu while the Windows
    lookup returned nothing and every shell command was refused for want of a sandbox that was
    already there.

    Probes the DAEMON, not the binary: a `docker` on PATH whose engine is not running cannot run
    anything, and reporting it as an available sandbox would turn a clear refusal into a confusing
    container error at the moment of use.

    Cached, because this spawns a process and is consulted on every shell command.

    **Only interrogates a distribution that is ALREADY RUNNING.** See `_wsl_running_distros`: this
    function used to start WSL as a side effect of asking whether WSL could run anything, and a
    scheduled task calling it every five minutes made a WSL window appear on the user's screen every
    five minutes. A capability check must not have a visible side effect.
    """
    cached = globals().get("_WSL_DOCKER_CACHE", "unset")
    if cached != "unset":
        return cached
    result: list[str] | None = None
    for distro in (distros if distros is not None else _wsl_running_distros()):
        # The docker command must be ONE string for `bash -lc`.
        #
        # Measured, and it cost a debugging round: passing `["bash", "-lc", "docker", "info", ...]`
        # makes bash run `docker` with NO arguments and treat the rest as positional parameters, so
        # the probe saw docker's usage text on stderr and exit 0 -- which read as "docker answered"
        # at a glance and as "no version" to the check. Joining the command fixes it.
        shell_command = "docker info --format {{.ServerVersion}}"
        actual = ["wsl", "-d", distro, "--", "bash", "-lc", shell_command]
        try:
            completed = subprocess.run(
                actual, capture_output=True, timeout=90, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if completed.returncode == 0 and (completed.stdout or b"").strip():
            # `["wsl", "-d", distro, "--", "docker"]` is the INVOCATION form: `bash -lc` was only
            # how the probe reached the binary. Passing the login-shell wrapper downstream would
            # require every argument to be shell-quoted into one string, which is how a path with a
            # space becomes two arguments.
            result = ["wsl", "-d", distro, "--", "docker"]
            break
    globals()["_WSL_DOCKER_CACHE"] = result
    return result


def docker_available() -> bool:
    """Whether a usable container engine exists -- binary AND running daemon.

    Previously `shutil.which("docker") is not None`, which is true for an installed-but-dead engine
    and false for the WSL-hosted engine that Docker Desktop actually uses. Both mistakes were
    measured on this host.
    """
    # `_probe_docker` owns the cache check; guarding here as well made every call re-probe, which
    # measured 5.5 seconds per shell command.
    _probe_docker()
    return bool(_docker_available_cache.get("answer"))


_docker_available_cache: dict = {}


def _probe_docker() -> None:
    """Fill the cache once: native engine first, then WSL."""
    if _docker_available_cache:
        return
    prefix: list[str] | None = None
    if shutil.which("docker"):
        try:
            completed = subprocess.run(
                ["docker", "info", "--format", "{{.ServerVersion}}"],
                capture_output=True, timeout=45, check=False,
            )
            if completed.returncode == 0 and (completed.stdout or b"").strip():
                prefix = ["docker"]
        except (OSError, subprocess.SubprocessError):
            prefix = None
    if prefix is None:
        prefix = _docker_in_wsl()
    _docker_available_cache["prefix"] = prefix
    _docker_available_cache["answer"] = prefix is not None


def docker_prefix() -> list[str] | None:
    """The argv that invokes Docker here, or None. Cached; see `_probe_docker`."""
    if not _docker_available_cache:
        _probe_docker()
    return _docker_available_cache.get("prefix")


def _container_path(path: str) -> str:
    """Translate a host path into the form the container runtime can mount.

    A WSL-hosted engine is a LINUX engine: it cannot mount `C:\\Users\\...`, so the path has to
    become `/mnt/c/Users/...`. Getting this wrong does not fail loudly -- Docker creates an empty
    directory and the agent silently loses its workspace -- which is why the translation lives here,
    in one place, with the reason recorded.
    """
    prefix = docker_prefix() or []
    if not prefix or prefix[0] != "wsl":
        return path
    distro = prefix[2] if len(prefix) > 2 else ""
    try:
        completed = subprocess.run(
            ["wsl", "-d", distro, "--", "bash", "-lc", f"wslpath -a '{path}'"],
            capture_output=True, timeout=45, check=False,
        )
        translated = (completed.stdout or b"").decode("utf-8", "replace").strip()
        if completed.returncode == 0 and translated:
            return translated
    except (OSError, subprocess.SubprocessError):
        pass
    # Fallback for a drive-letter path when `wslpath` is unavailable: the default WSL mount scheme.
    match = re.match(r"^([A-Za-z]):[\\/](.*)$", path)
    if match:
        return f"/mnt/{match.group(1).lower()}/" + match.group(2).replace("\\", "/")
    return path


def _sandboxed_shell_argv(command: str, cwd: str) -> list[str]:
    """Runs the command inside a throwaway, network-isolated PowerShell container instead of
    directly on the host: --network none blocks all network access, and the only filesystem
    access is a volume mount of cwd. Uses Microsoft's own cross-platform PowerShell image so
    command syntax stays identical to the unsandboxed path (still PowerShell, not POSIX sh).

    VERIFIED end-to-end on this host against Docker 29.8.2 hosted in WSL Ubuntu: the container
    started, `pwsh` ran, and the command's output came back. That verification is why the argv is
    built from `docker_prefix()` rather than a bare "docker" -- the engine here is reachable only
    through WSL, and the hardcoded path made this function unreachable in practice.

    The working directory is translated to the engine's filesystem (`_container_path`), because a
    Linux engine mounting a Windows path would mount an empty directory instead.
    """
    prefix = docker_prefix() or ["docker"]
    mount = _container_path(cwd)
    return [
        *prefix, "run", "--rm", "--network", "none",
        "-v", f"{mount}:/workspace",
        "-w", "/workspace",
        sandbox_image(),
        "pwsh", "-NoProfile", "-NonInteractive", "-Command", command,
    ]


WINDOWS_SANDBOX_EXE = "WindowsSandbox.exe"


def windows_sandbox_available() -> bool:
    return platform.system() == "Windows" and shutil.which(WINDOWS_SANDBOX_EXE) is not None


def _run_in_windows_sandbox(command: str, cwd: str, timeout: int) -> tuple[str, bool]:
    """Docker-free isolation fallback: runs the command inside a fresh, disposable Windows
    Sandbox VM (a real hypervisor boundary, stronger than a container) instead of a Docker
    container. Command escaping is sidestepped entirely by writing the command to a plain .ps1
    file in a shared folder rather than embedding it in the .wsb XML/command line. Completion is
    detected via the sandboxed script shutting the VM down itself (Stop-Computer) when it's
    done, which makes the host's `WindowsSandbox.exe` process exit -- there's no other clean
    signal for "the command finished" against this API. UNVERIFIED end-to-end: the Windows
    Sandbox optional feature isn't enabled on the machine this was built on (Windows 11 Pro
    supports it, but turning it on requires admin elevation + a reboot that wasn't performed).
    Expect materially higher latency than Docker -- a full VM boot (commonly 10-30s) happens on
    every single call, with no state persisted between calls."""
    with tempfile.TemporaryDirectory(prefix="myriaxonsandbox_") as shared:
        shared_path = Path(shared)
        output_path = shared_path / "output.txt"
        (shared_path / "run.ps1").write_text(
            "Set-Location 'C:\\workspace'\n"
            "try {\n"
            f"    {command} *>&1 | Out-File -Encoding utf8 'C:\\myriaxonsandbox\\output.txt'\n"
            "} catch {\n"
            "    $_.Exception.Message | Out-File -Encoding utf8 'C:\\myriaxonsandbox\\output.txt'\n"
            "}\n"
            "Stop-Computer -Force\n",
            encoding="utf-8",
        )
        wsb_path = shared_path / "run.wsb"
        wsb_path.write_text(
            "<Configuration>\n"
            "  <Networking>Disable</Networking>\n"
            "  <MappedFolders>\n"
            f"    <MappedFolder><HostFolder>{cwd}</HostFolder><SandboxFolder>C:\\workspace</SandboxFolder>"
            "<ReadOnly>false</ReadOnly></MappedFolder>\n"
            f"    <MappedFolder><HostFolder>{shared_path}</HostFolder><SandboxFolder>C:\\myriaxonsandbox</SandboxFolder>"
            "<ReadOnly>false</ReadOnly></MappedFolder>\n"
            "  </MappedFolders>\n"
            "  <LogonCommand>\n"
            "    <Command>powershell -NoProfile -ExecutionPolicy Bypass -File C:\\myriaxonsandbox\\run.ps1</Command>\n"
            "  </LogonCommand>\n"
            "</Configuration>\n",
            encoding="utf-8",
        )

        try:
            proc = subprocess.Popen([WINDOWS_SANDBOX_EXE, str(wsb_path)])
        except OSError as exc:
            return f"Failed to launch Windows Sandbox: {exc}", True

        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.terminate()
            return f"Windows Sandbox timed out after {timeout}s (VM boot + command).", True

        if output_path.exists():
            return output_path.read_text(encoding="utf-8", errors="replace"), False
        return "Windows Sandbox closed with no output captured -- it may have been closed manually.", True


class _BackgroundProcess:
    """Tracks a run_bash call started with run_in_background=True. A daemon thread continuously
    drains proc.stdout (a pipe that's never read will eventually fill and block the child), so
    poll_new_output() can return just the lines added since the caller's last check."""

    def __init__(self, proc: subprocess.Popen, command: str, job: "ProcessJob | None" = None) -> None:
        self.proc = proc
        self.command = command
        self.job = job
        self.exit_code: int | None = None
        self.terminated = False
        # When the job started, for jobs.view's elapsed figure. Set here rather than in the caller so every
        # construction path gets it, and named with the module prefix so it is obviously not part of the public shape.
        self._myriaxon_started_at = time.time()
        # Whether this job's completion has been reported to the model. The jobs layer sets it, and it exists so a
        # finished job is announced EXACTLY ONCE -- a job that re-reported on every check would nag until the model
        # learned to ignore the runtime-context block.
        self.notified = False
        self._lines: list[str] = []
        self._read_offset = 0
        self._lock = threading.Lock()
        threading.Thread(target=self._drain, daemon=True).start()

    def _drain(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            with self._lock:
                self._lines.append(line)
        self.proc.wait()
        self.exit_code = self.proc.returncode

    def poll_new_output(self) -> str:
        with self._lock:
            new_lines = self._lines[self._read_offset :]
            self._read_offset = len(self._lines)
        return "".join(new_lines)

    def status_text(self) -> str:
        if self.terminated:
            return "killed"
        if self.exit_code is not None:
            return f"exited with code {self.exit_code}"
        return "running"


def _run_bash(tool_input: dict, ctx: ToolContext) -> tuple[str, bool]:
    console = ctx.console
    command = tool_input["command"]
    timeout = tool_input.get("timeout", DEFAULT_BASH_TIMEOUT)
    run_in_background = tool_input.get("run_in_background", False)

    panel = Panel(Text(command), title="[tool.confirm.question]run_bash[/]", border_style="tool.confirm.question")
    proceed, blocked = confirm_action(console, panel, ctx, "run_bash", command=command, tool_input=tool_input)
    if blocked:
        # RETURNED AS AN ERROR, and the flag is the fix rather than the message.
        #
        # This was `return blocked, False` -- the refusal text with a success flag. Measured consequence: an
        # agent that asked for a shell it was not allowed to have received "Blocked: a shell command cannot be
        # confined..." as a NON-ERROR result, so it read the refusal as something that had happened, and the
        # loop detector -- which keys partly on failures -- never saw a failing call. In a confined Locus this
        # is the only outcome `run_bash` can ever produce, which makes it the whole of that agent's shell
        # experience.
        #
        # The MESSAGE is unchanged and deliberately so: the tests assert it explains what to do, and it does.
        # Only the flag changes, because "this did not run" is what an error flag means.
        return blocked, True
    if not proceed:
        return "User declined to run this tool call.", True

    # WHAT GETS MOUNTED, and this is a confinement boundary rather than a detail.
    #
    # A confined Locus must see its OWN workspace in the container, not the process's working
    # directory. Measured before the fix: a confined Locus's `run_bash` mounted the PROJECT ROOT, so
    # `Get-Content /workspace/host.txt` failed to find the file the host had just written into the
    # Locus workspace -- and, worse, the "confined" agent had read/write access to the whole project
    # through the mount. The confinement checks in `_resolve` guard the FILE tools by path; `run_bash`
    # bypasses them entirely, so the mount is the only boundary there is.
    sandbox_cwd = _LOCUS_WORKSPACE.get()
    mount_cwd = str(sandbox_cwd) if sandbox_cwd is not None else os.getcwd()
    if ctx.sandbox_enabled and not docker_available():
        # Docker-free fallback -- if Windows Sandbox is available, use it instead of failing
        # outright. It can't be streamed/backgrounded like the Docker/direct paths below (no
        # live stdout pipe, just a result file after the VM shuts down), so it returns directly.
        if run_in_background:
            return "Sandbox mode without Docker (falling back to Windows Sandbox) doesn't support run_in_background.", True
        if windows_sandbox_available():
            return _run_in_windows_sandbox(command, mount_cwd, timeout)
        return (
            "Sandbox mode is on but neither 'docker' nor Windows Sandbox is available -- "
            "install/start Docker Desktop, enable the 'Windows Sandbox' optional Windows "
            "feature (Windows 11 Pro/Enterprise), or turn sandboxing off with /sandbox off.",
            True,
        )
    argv = _sandboxed_shell_argv(command, mount_cwd) if ctx.sandbox_enabled else shell_argv(command)

    try:
        proc = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            # UTF-8 WITH REPLACEMENT, and both halves are load-bearing.
            #
            # `text=True` alone decodes with the PROCESS LOCALE, which on Windows is cp1252. Measured live: a
            # command emitting a non-ASCII byte killed the reader thread --
            #
            #     UnicodeDecodeError: 'charmap' codec can't decode byte 0x81 in position 75
            #       File "myriaxon\tools.py", line 1193, in _drain_foreground
            #         for line in proc.stdout:
            #
            # -- and the failure was INVISIBLE from the outside. The traceback went to stderr, the command
            # still "ran", and the model received less output than the command produced with nothing anywhere
            # saying so. That is the worst shape a bug can take: it does not stop the work, it silently
            # shrinks what the agent can see, on any command whose output contains a non-ASCII byte. Real
            # ones: a build tool's box-drawing characters, a test runner's check marks, a path with an accent,
            # and this project's own arrow and em-dash output.
            #
            # `errors="replace"` because a decode failure must never cost the WHOLE command: one undecodable
            # byte becomes U+FFFD and the other ten thousand lines survive. Without it, a single stray byte in
            # a log file throws away everything the command said.
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            cwd=os.getcwd(),
        )
    except OSError as exc:
        return f"Failed to start command: {exc}", True

    # Best-effort: assign the freshly-spawned process to a Job Object immediately, before it has
    # a chance to spawn any children of its own -- confirmed live that job membership only covers
    # children spawned *after* assignment, so any delay here would silently narrow the guarantee.
    # Creation/assignment failure just means we fall back to the pre-existing taskkill/terminate
    # path (see _kill_process_tree) -- never block the command over this extra safety layer.
    job: ProcessJob | None = None
    if job_objects_available():
        try:
            job = ProcessJob()
            job.assign(proc.pid)
        except OSError:
            job = None

    if run_in_background:
        ctx.background_id_counter += 1
        bg_id = f"bash_{ctx.background_id_counter}"
        ctx.background_procs[bg_id] = _BackgroundProcess(proc, command, job=job)
        return f"Started background process {bg_id}: {command}", False

    # `for line in proc.stdout` blocks on each readline() with no timeout of its own -- a command
    # that produces no output until it exits (confirmed live: a silent parent that spawns a
    # detached child and then sleeps) would never hit the `timeout` bound at all, since
    # proc.wait(timeout=...) was only ever reached *after* that loop already finished. Draining
    # stdout on a background thread and gating on proc.wait(timeout=...) in the main thread makes
    # `timeout` a real wall-clock cap regardless of the command's output behavior -- the actual
    # point of adding the Job Object hard-kill below.
    output_lines: list[str] = []
    output_lock = threading.Lock()

    def _drain_foreground() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            with output_lock:
                output_lines.append(line)
            console.print(f"[dim]{escape(line.rstrip())}[/]")

    reader = threading.Thread(target=_drain_foreground, daemon=True)
    reader.start()
    try:
        proc.wait(timeout=timeout)
        reader.join(timeout=1)
    except subprocess.TimeoutExpired:
        _kill_process_tree(proc, job)
        reader.join(timeout=1)
        with output_lock:
            output = "".join(output_lines)
        # The guidance matters as much as the verdict. Measured: a plain `Get-Content` against a project
        # file hit this path, and the message was the bare line "[timed out after 120s]" -- which tells
        # the model nothing about what to do differently, so it retried the same read. Two facts are
        # worth stating, because they are the two things that make a repeat pointless:
        #
        #   * `timeout` is a parameter, so a command that legitimately needs longer can have it;
        #   * a command that is still running is a BACKGROUND job, which is what `run_in_background` is
        #     for -- the foreground path exists for commands that finish.
        #
        # The distinction the model most needs is that raising the timeout is the wrong move when the
        # command was never going to finish: a sandbox cold start or a server that runs forever both look
        # identical from here, and only one of them is fixed by waiting longer.
        return (
            output
            + f"\n[timed out after {timeout}s and was killed]"
            + "\nThis command did not finish. Before retrying: a command that legitimately needs longer "
              "can be given a larger `timeout` in seconds, but a command that does not finish at all "
              "(a server, a watch, a long build) belongs in the background -- call this again with "
              "`run_in_background: true` and read it with the job tools. Do not repeat the identical "
              f"call: it will block for the same {timeout}s and fail the same way."
        ), True
    except KeyboardInterrupt:
        _kill_process_tree(proc, job)
        raise
    finally:
        if job is not None:
            job.close()

    with output_lock:
        output = "".join(output_lines)
    return f"{output}\nExit code: {proc.returncode}", False


def _kill_process_tree(proc: subprocess.Popen, job: "ProcessJob | None" = None) -> None:
    # Prefer the Job Object -- confirmed live it kills the whole tree unconditionally, including
    # a detached grandchild taskkill /F /T can miss depending on timing. Falls back to taskkill
    # when no job was ever assigned (creation/assignment failed, or not on Windows).
    if job is not None:
        job.close()
    elif platform.system() == "Windows":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
    else:
        proc.terminate()


def _bash_output(tool_input: dict, ctx: ToolContext) -> tuple[str, bool]:
    bg_id = tool_input["id"]
    bg = ctx.background_procs.get(bg_id)
    if bg is None:
        return f"No background process found with id {bg_id!r}.", True

    new_output = bg.poll_new_output()
    return f"[{bg.status_text()}]\n{new_output}", False


def _bg_jobs(tool_input: dict, ctx: ToolContext) -> tuple[str, bool]:
    """List background jobs, with the registry cap applied."""
    from . import jobs

    return jobs.render_list(ctx), False


def _bg_wait(tool_input: dict, ctx: ToolContext) -> tuple[str, bool]:
    """Wait for a background job, bounded, with an optional promotion.

    The timeout is bounded because `run_budget.check()` runs at the STEP boundary: a tool call that never returns
    means a budget that is never consulted, so an unbounded wait would quietly defeat the ceiling above it.
    """
    from . import jobs

    job_id = str(tool_input.get("id") or "").strip()
    if not job_id:
        return "Provide `id`: the job to wait for. Use bg_jobs to list them.", True
    try:
        timeout = float(tool_input.get("timeout") or jobs.DEFAULT_BACKGROUND_WAIT_SECONDS)
    except (TypeError, ValueError):
        return "`timeout` must be a number of seconds.", True
    # A negative timeout would busy-loop through `wait` and return immediately, which is not what the caller means.
    timeout = max(0.0, min(timeout, 600.0))

    if job_id not in getattr(ctx, "background_procs", {}):
        return (
            f"No background process with id {job_id!r}. Use bg_jobs to see the ids that exist -- a job dropped "
            "by the registry cap is no longer listed and its output is gone."
        ), True

    if tool_input.get("promote"):
        ok, text = jobs.promote(ctx, job_id, timeout)
        return text, not ok
    finished, text = jobs.wait(ctx, job_id, timeout)
    if not finished:
        return text, True
    view = jobs.view(job_id, jobs.get(ctx, job_id))
    return f"[{view.job_id} {view.state}]\n{text}", False


def _kill_bash(tool_input: dict, ctx: ToolContext) -> tuple[str, bool]:
    bg_id = tool_input["id"]
    bg = ctx.background_procs.get(bg_id)
    if bg is None:
        return f"No background process found with id {bg_id!r}.", True

    if bg.exit_code is None and not bg.terminated:
        _kill_process_tree(bg.proc, bg.job)
        bg.terminated = True
    return f"Background process {bg_id} killed.", False


def _load_ignore_patterns(root: Path) -> list[str]:
    """Gitignore-style patterns from .gitignore and .myriaxonignore (both optional, both additive),
    plus a built-in default set (.git, __pycache__, .venv, node_modules, etc.) that's always
    active -- most projects never bother writing an ignore file for an agent tool, and without
    this, grep_search silently crawls the entire .venv/node_modules tree on every call. This is
    a plain fnmatch-based approximation of gitignore syntax (no negation, no anchoring), not a
    full implementation -- good enough for directory-name and extension exclusions.

    The pre-rename `.iohignore` is read as well, so a repository that added one keeps being
    honoured instead of silently having the agent crawl paths the user had excluded."""
    patterns = list(DEFAULT_IGNORE_PATTERNS)
    for name in (".gitignore", MYRIAXON_IGNORE_FILENAME, LEGACY_IGNORE_FILENAME):
        path = root / name
        if not path.is_file():
            continue
        try:
            lines = path.read_text(encoding="utf-8-sig", errors="ignore").splitlines()
        except OSError:
            continue
        patterns += [ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith("#")]
    return patterns


def _is_ignored(path: Path, root: Path, patterns: list[str]) -> bool:
    try:
        rel_parts = path.relative_to(root).parts
    except ValueError:
        rel_parts = path.parts
    rel = "/".join(rel_parts)
    for pat in patterns:
        pat = pat.strip().rstrip("/")
        if not pat:
            continue
        if fnmatch.fnmatch(rel, pat) or any(fnmatch.fnmatch(part, pat) for part in rel_parts):
            return True
    return False


# Directories a search must never descend into. These are the ones that turn a bounded scan into a
# filesystem walk: a package cache, a virtual environment or a container image can each hold a million
# files, and none of them can contain an answer to a question about the user's project.
_SEARCH_NEVER_DESCEND = frozenset({
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    ".venv", "venv", "env", ".tox", ".nox", "site-packages", "dist-info", ".egg-info",
    "Windows", "System32", "SysWOW64", "WinSxS", "ProgramData", "Program Files", "Program Files (x86)",
    "$Recycle.Bin", "System Volume Information", "AppData", "proc", "sys", "dev", "run", "snap",
    "Library", "Applications", ".Trash", ".cache", "Caches",
})


def _is_filesystem_root(path: Path) -> bool:
    """True for `/`, `C:\\`, and a UNC share root.

    A search from here is never what somebody means. It cannot be completed quickly on any machine, and its
    result -- "no files matched" -- is indistinguishable from "nothing exists", while having cost minutes. The
    measured case: `glob_search(path="/", pattern="**/anker/**")` ran for **132 seconds** before returning
    "No files matched", which an agent reads as a finding about the filesystem rather than as its own mistake.
    """
    try:
        resolved = path.resolve()
    except OSError:
        resolved = path
    if resolved.parent == resolved:
        return True
    # A drive root on Windows: `C:\` has itself as its own parent.
    return bool(resolved.drive) and str(resolved).rstrip("\\/") in (resolved.drive, resolved.drive + "\\")


def _glob_to_regex(pattern: str):
    """Compile a glob into a regex over a POSIX-style RELATIVE path.

    WHY NOT `fnmatch`. `fnmatch` treats `*` as "anything, including separators", so `**/*.py` and `*.py`
    become the same expression and a recursive search silently returns the wrong set. Worse, `**/*.py` does not
    even match `src/main.py` under it, because the compiled pattern demands a literal separator before the
    filename -- which is exactly the bug an earlier version of this function shipped: the walk was correct, the
    pruning was correct, and the match returned nothing for a pattern every user would write.

    The mapping is the standard one:
        `**/`  -> zero or more whole path segments   (so `**/*.py` matches `a.py` AND `src/a.py`)
        `**`   -> anything at all, separators included
        `*`    -> anything EXCEPT a separator        (so `*.py` stays this directory only)
        `?`    -> one character, not a separator
    """
    out: list[str] = []
    index = 0
    length = len(pattern)
    while index < length:
        char = pattern[index]
        if char == "*":
            if pattern.startswith("**/", index):
                # Zero or more segments: "any/prefix/" or nothing at all.
                out.append("(?:[^/]*/)*")
                index += 3
                continue
            if pattern.startswith("**", index):
                out.append(".*")
                index += 2
                continue
            out.append("[^/]*")
            index += 1
            continue
        if char == "?":
            out.append("[^/]")
            index += 1
            continue
        if char in "[{":
            # Let `fnmatch.translate` handle a character class rather than reimplementing it.
            close = "]" if char == "[" else "}"
            end = pattern.find(close, index + 1)
            if end != -1:
                body = pattern[index:end + 1]
                out.append(fnmatch.translate(body)[4:-3] if char == "[" else ".*")
                if char == "{":
                    # Brace expansion is not worth reimplementing; treat the set loosely.
                    out[-1] = "(?:" + "|".join(re.escape(o) for o in body[1:-1].split(",")) + ")"
                index = end + 1
                continue
        out.append(re.escape(char))
        index += 1
    return re.compile("^" + "".join(out) + "$")


def _walk_bounded(base: Path, pattern: str, ignore_patterns) -> tuple[list[Path], bool]:
    """Yield files under `base` matching `pattern`, bounded in TIME and COUNT, with directory pruning.

    WHY THIS EXISTS RATHER THAN `base.glob(pattern)`. The previous implementation checked a time budget inside
    the loop over `glob()` results, and that check could not fire: `Path.glob` discovers every directory it is
    going to yield from BEFORE it yields the first result, so a recursive pattern over a large tree spends its
    whole cost inside the generator and the budget is consulted for the first time after the work is done. The
    budget was real, and unreachable.

    `os.walk` visits lazily and `dirnames[:] =` prunes IN PLACE -- so a pruned subtree is never descended into,
    which is where nearly all of the cost was. The time check below therefore runs between directories, while
    the expensive thing is still ahead of it.

    Returns (matches, truncated). A caller must report truncation rather than present a partial scan as
    complete, because "No files matched" from a scan that stopped early is a false statement about the system.
    """
    matches: list[Path] = []
    truncated = False
    scanned = 0
    started = time.monotonic()
    matcher = _glob_to_regex(pattern)

    for root, dirnames, filenames in os.walk(base, topdown=True, followlinks=False):
        if time.monotonic() - started > SEARCH_TIME_BUDGET_SECONDS or scanned > SEARCH_SCAN_CAP:
            truncated = True
            break

        # Pruned in place, so `os.walk` never enters them. This is the line that turns a 132-second walk into
        # an instant answer, because the cost was in the directories nobody wanted searched.
        dirnames[:] = [
            name for name in dirnames
            if name not in _SEARCH_NEVER_DESCEND and not _is_ignored(Path(root) / name, base, ignore_patterns)
        ]

        for filename in filenames:
            scanned += 1
            if scanned > SEARCH_SCAN_CAP:
                truncated = True
                break
            candidate = Path(root) / filename
            try:
                relative = candidate.relative_to(base).as_posix()
            except ValueError:
                relative = candidate.as_posix()
            # Matched as a RELATIVE path, which is what makes `**/x` span directories and `*` not.
            if matcher.match(relative) and not _is_ignored(candidate, base, ignore_patterns):
                matches.append(candidate)
        if scanned > SEARCH_SCAN_CAP:
            break

    return matches, truncated


def _glob_search(tool_input: dict) -> tuple[str, bool]:
    base = _resolve(tool_input.get("path", ".") or ".")
    if not base.exists():
        return f"Directory not found: {base}", True

    if _is_filesystem_root(base):
        # Refused rather than attempted, and the refusal says what to pass instead. An agent that asked for
        # `/` did not want the whole machine; it wanted to find something and did not know where, so the
        # message has to teach the narrower question rather than only deny this one.
        return (
            f"Refused: {base} is a filesystem root, and searching from here would scan the whole machine "
            "-- measured at over two minutes, and it cannot match anything a project search would want. "
            "Pass the directory you actually mean (for example the project root, or a subdirectory), or use "
            "`path` with a narrower `pattern`. If you are looking for a named directory and do not know where "
            "it is, search from the project root or ask the user where their projects live.",
            True,
        )

    ignore_patterns = _load_ignore_patterns(base)
    pattern = tool_input["pattern"]
    # Bounded scan -- confirmed live this matters: with no `path` given, this defaults to the *shell's* cwd,
    # which may not be the project root (e.g. a parent folder containing several large, unrelated projects).
    matches, truncated = _walk_bounded(base, pattern, ignore_patterns)

    matches = sorted(matches, key=lambda p: p.stat().st_mtime, reverse=True)[:SEARCH_MATCH_CAP]
    if not matches:
        if truncated:
            return _search_truncated_notice("No files matched"), False
        return "No files matched.", False
    result = "\n".join(str(m) for m in matches)
    if truncated:
        result += "\n" + _search_truncated_notice(None)
    return result, False


def _grep_search(tool_input: dict) -> tuple[str, bool]:
    base = _resolve(tool_input.get("path", ".") or ".")
    if not base.exists():
        return f"Directory not found: {base}", True

    flags = re.IGNORECASE if tool_input.get("case_insensitive") else 0
    try:
        pattern = re.compile(tool_input["pattern"], flags)
    except re.error as exc:
        return f"Invalid regex: {exc}", True

    ignore_patterns = _load_ignore_patterns(base)
    name_filter = tool_input.get("glob")
    results: list[str] = []
    start = time.monotonic()
    scanned = 0
    truncated = False
    for file_path in base.rglob("*"):
        if len(results) >= SEARCH_MATCH_CAP:
            break
        scanned += 1
        if scanned > SEARCH_SCAN_CAP or time.monotonic() - start > SEARCH_TIME_BUDGET_SECONDS:
            truncated = True
            break
        if not file_path.is_file():
            continue
        if _is_ignored(file_path, base, ignore_patterns):
            continue
        if name_filter and not fnmatch.fnmatch(file_path.name, name_filter):
            continue
        try:
            text = file_path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if pattern.search(line):
                results.append(f"{file_path}:{lineno}: {line.strip()}")
                if len(results) >= SEARCH_MATCH_CAP:
                    break

    if not results:
        if truncated:
            return _search_truncated_notice("No matches found"), False
        return "No matches found.", False
    result = "\n".join(results)
    if truncated:
        result += "\n" + _search_truncated_notice(None)
    return result, False


def _run_git(args: list[str], cwd: Path) -> tuple[str, bool]:
    """Shared plumbing for the read-only git tools. Not-a-git-repo and git-not-installed are
    both expected, non-crashing outcomes -- this project's own directory isn't a git repo, so
    that path gets exercised immediately."""
    try:
        result = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace"
        )
    except FileNotFoundError:
        return "git is not installed or not on PATH.", True

    if result.returncode != 0:
        stderr = result.stderr.strip()
        if "not a git repository" in stderr.lower():
            return f"{cwd} is not a git repository.", True
        return stderr or f"git exited with code {result.returncode}.", True

    return result.stdout, False


def _git_status(tool_input: dict) -> tuple[str, bool]:
    base = _resolve(tool_input.get("path", ".") or ".")
    if not base.exists():
        return f"Directory not found: {base}", True

    output, is_error = _run_git(["status", "--porcelain=v1", "-b"], base)
    if is_error:
        return output, True
    return output.strip() or "Working tree clean.", False


def _git_diff(tool_input: dict) -> tuple[str, bool]:
    base = _resolve(tool_input.get("path", ".") or ".")
    if not base.exists():
        return f"Directory not found: {base}", True

    path = tool_input.get("path")
    args = ["diff"]
    if tool_input.get("staged"):
        args.append("--cached")
    # `path` here doubles as both "where to run git" (resolved above, always a directory-ish
    # target) and, if it's actually a specific file, the diff scope -- git diff itself accepts a
    # trailing pathspec, so pass it through if it was given.
    if path:
        args += ["--", path]

    output, is_error = _run_git(args, base if base.is_dir() else base.parent)
    if is_error:
        return output, True
    if not output.strip():
        return "No changes.", False
    if len(output.encode("utf-8", errors="replace")) > GIT_DIFF_BYTE_CAP:
        output = output.encode("utf-8", errors="replace")[:GIT_DIFF_BYTE_CAP].decode(
            "utf-8", errors="ignore"
        ) + "\n... (truncated)"
    return output, False


def _schedule_prompt(tool_input: dict, ctx: ToolContext) -> tuple[str, bool]:
    if ctx.scheduler is None:
        return "No scheduler configured for this session.", True
    entry_id = ctx.scheduler.schedule(tool_input["delay_seconds"], tool_input["prompt"])
    return f"Scheduled (id: {entry_id}), firing in {tool_input['delay_seconds']}s.", False


def _list_scheduled(tool_input: dict, ctx: ToolContext) -> tuple[str, bool]:
    if ctx.scheduler is None:
        return "No scheduler configured for this session.", True
    entries = ctx.scheduler.list_entries()
    if not entries:
        return "No prompts scheduled.", False
    return "\n".join(f"{e['id']}  in {e['seconds_remaining']}s  {e['prompt']!r}" for e in entries), False


def _cancel_scheduled(tool_input: dict, ctx: ToolContext) -> tuple[str, bool]:
    if ctx.scheduler is None:
        return "No scheduler configured for this session.", True
    if ctx.scheduler.cancel(tool_input["id"]):
        return f"Cancelled {tool_input['id']}.", False
    return f"No scheduled prompt found with id {tool_input['id']!r}.", True


def execute_tool(name: str, tool_input: dict, ctx: ToolContext) -> tuple[str, bool]:
    ctx.console.print(f"[tool.name]{name}[/] [dim]{escape(str(tool_input))}[/]")

    if name == "read_file":
        return _read_file(tool_input)
    if name == "write_file":
        return _write_file(tool_input, ctx)
    if name == "edit_file":
        return _edit_file(tool_input, ctx)
    if name == "run_bash":
        return _run_bash(tool_input, ctx)
    if name == "bash_output":
        return _bash_output(tool_input, ctx)
    if name == "bg_jobs":
        return _bg_jobs(tool_input, ctx)
    if name == "bg_wait":
        return _bg_wait(tool_input, ctx)
    if name == "kill_bash":
        return _kill_bash(tool_input, ctx)
    if name == "glob_search":
        return _glob_search(tool_input)
    if name == "grep_search":
        return _grep_search(tool_input)
    if name == "git_status":
        return _git_status(tool_input)
    if name == "git_diff":
        return _git_diff(tool_input)
    if name == "schedule_prompt":
        return _schedule_prompt(tool_input, ctx)
    if name == "list_scheduled":
        return _list_scheduled(tool_input, ctx)
    if name == "cancel_scheduled":
        return _cancel_scheduled(tool_input, ctx)

    return f"Unknown tool: {name}", True
