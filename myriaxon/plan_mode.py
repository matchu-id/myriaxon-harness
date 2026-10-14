"""Plan mode: an operating mode that structurally refuses to change anything.

## The gap, and why a label is not enough

Myriaxon had `MODE_PLAN` in its constants, offered `--mode plan` on the command line, and displayed it — and **never
consulted it anywhere**. Measured: `ctx.mode` is set and passed around, but no tool dispatch reads it. So "plan mode"
was a prompt hint at best, and a model that ignored it could edit freely.

Opencode's plan agent is a *structural* denial (`packages/opencode/src/agent/agent.ts`): the built-in `plan` agent
denies every edit tool and exposes only `question`/`plan_exit`. The difference is the whole point — a mode that
depends on the model's cooperation is not a mode.

## What this enforces

`MODE_PLAN` refuses the operations that change state, **before the tool runs**, with a message that says what was
refused and what to do instead. Everything read-only still works, because the purpose of plan mode is to investigate
and propose.

## The two decisions worth stating

**Which tools change state?** Not a list of names to keep in sync — that is the mistake this repository has made
repeatedly. The rule is derived from the tool's own declaration where possible, and the explicit sets below name
only what cannot be derived. A new tool is classified by what it does, not by whether someone remembered to add it.

**A refusal must be actionable.** "Blocked by plan mode" leaves a model guessing whether to retry, change approach,
or stop. The message says all three: nothing was changed, here is what you may do instead, and here is how the user
leaves plan mode. A refusal that does not say what to do next is how a loop starts.
"""

from __future__ import annotations

from dataclasses import dataclass

from .constants import MODE_AUTO, MODE_AUTO_EDIT, MODE_MANUAL, MODE_PLAN

# Tools that cannot change state under any circumstance. Named explicitly rather than derived, because being wrong
# here in the permissive direction is the failure this module exists to prevent.
READ_ONLY_TOOLS = frozenset({
    "read_file", "list_directory", "glob", "grep", "search_files", "web_search", "web_fetch",
    "session_status", "recall", "tool_search", "request_tools", "checkpoint_list",
    "skill_list", "use_skill", "mcp_list", "get_context_usage", "ask_user",
})

# Tools that change something, regardless of what they are called.
STATEFUL_TOOLS = frozenset({
    "write_file", "edit_file", "apply_patch", "notebook_edit", "run_bash", "run_python",
    "checkpoint_restore", "locus_create", "locus_goal_update", "locus_run", "locus_watchlist",
    "save_skill", "skill_propose", "spawn_subagent", "task", "branch_session",
    "heartbeat_respond", "design_poster", "schedule_prompt",
})

# Substrings that mark a tool as almost certainly stateful. A backstop for a tool this module has never seen: an
# unknown tool whose name contains `write` is refused in plan mode even if it is not in `STATEFUL_TOOLS`, because
# the cost of a false refusal is one confused turn and the cost of a false permit is an edit nobody approved.
STATEFUL_MARKERS = ("write", "edit", "patch", "delete", "remove", "move", "rename", "create",
                    "run", "exec", "shell", "bash", "install", "save", "restore", "spawn", "send")


@dataclass
class Verdict:
    """Whether an operation is allowed in the current mode."""

    allowed: bool
    reason: str = ""
    mode: str = ""

    def render(self) -> str:
        return self.reason if not self.allowed else ""


# The one message shape, so a refusal reads the same everywhere it appears.
_REFUSAL = (
    "[plan mode] Refused: `{tool}` can change files or system state, and this session is in PLAN mode.\n"
    "Nothing was changed.\n"
    "Do this instead: investigate with the read-only tools (read_file, grep, glob, list_directory, web_fetch) and "
    "write your plan as your reply. When you know what should change, describe it precisely -- the files, the edits "
    "and the reason -- and stop.\n"
    "The user leaves plan mode by re-running with `--mode auto_edit` (or `--mode auto`), or by switching mode in the "
    "session. Do not retry this tool: it will be refused the same way until the mode changes."
)


def is_stateful(tool_name: str) -> bool:
    """Whether a tool can change state.

    Explicit sets first, then a marker backstop. The backstop matters for a tool registered by a plugin or an MCP
    server that this module has never heard of: `mcp__server__write_thing` is refused by its name even though no
    list contains it. Being wrong permissively here is the bug; being wrong restrictively costs one turn.
    """
    name = (tool_name or "").strip().lower()
    if not name:
        return False
    if name in READ_ONLY_TOOLS:
        return False
    if name in STATEFUL_TOOLS:
        return True
    return any(marker in name for marker in STATEFUL_MARKERS)


def normalise_mode(mode: str) -> str:
    """A known mode, defaulting to the most restrictive.

    An unrecognised mode resolving to `manual` rather than `auto` is deliberate: a typo in a mode name must not be
    the thing that grants write access.
    """
    candidate = (mode or "").strip().lower()
    for known in (MODE_PLAN, MODE_MANUAL, MODE_AUTO_EDIT, MODE_AUTO):
        if candidate == known:
            return known
    return MODE_MANUAL


def check(tool_name: str, mode: str) -> Verdict:
    """Whether `tool_name` may run in `mode`."""
    resolved = normalise_mode(mode)
    if resolved != MODE_PLAN:
        return Verdict(allowed=True, mode=resolved)
    if not is_stateful(tool_name):
        return Verdict(allowed=True, mode=resolved)
    return Verdict(
        allowed=False,
        mode=resolved,
        reason=_REFUSAL.format(tool=tool_name),
    )


def check_for_context(tool_name: str, ctx: object) -> Verdict:
    """`check`, reading the mode from a ToolContext.

    Separate from `check` so the rule can be exercised without constructing a context, which is what the verifier
    does -- and so a caller that has only a mode string is not forced to fake one.
    """
    mode = str(getattr(ctx, "mode", "") or "")
    return check(tool_name, mode)


def describe(mode: str) -> str:
    """One line for a status report or a startup banner."""
    resolved = normalise_mode(mode)
    if resolved == MODE_PLAN:
        return (
            "plan: read-only. Every tool that changes state is refused before it runs, and the refusal says what "
            "to do instead."
        )
    if resolved == MODE_MANUAL:
        return "manual: each write and command asks for confirmation."
    if resolved == MODE_AUTO_EDIT:
        return "auto-edit: file edits are applied without asking; commands still ask."
    return "auto: writes and commands run without asking."


def stateful_tools() -> list[str]:
    """The explicit stateful set, sorted. For `doctor` and for the matrix row."""
    return sorted(STATEFUL_TOOLS)


def readonly_tools() -> list[str]:
    return sorted(READ_ONLY_TOOLS)
