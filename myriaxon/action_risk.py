"""Action risk: what an action would cost if it went wrong, and when to ask before taking it.

Level 5 requirement 5.6 -- *escalate on a risk threshold, not only on completion*. Level 3 escalates when it
is DONE; Level 5 escalates when the next action is dangerous. That needs a classifier, and this is it.

WHAT REPLACES WHAT, AND WHY

`constants.RISKY_BASH_PATTERNS` already exists and describes itself as *"a best-effort heuristic for Auto
mode's safety check -- NOT a security boundary"*. It is a regex over one tool's command text, so it sees
`rm -rf` and nothing else: it cannot tell a write inside the workspace from a write over the owner's documents,
and it has no opinion about any tool that is not a shell.

**Blast radius is the property that generalises**, because it is a fact about the ACTION rather than a
pattern in its text:

    LOW     reversible and confined -- reading, searching, planning
    MEDIUM  changes state inside the workspace -- writing a file, claiming a board task
    HIGH    leaves the workspace, cannot be undone, or costs money -- deleting outside the workspace,
            writing to the host, sending a message, running a shell command

The shell case is the interesting one and it is deliberate: a shell command is HIGH because it is UNBOUNDED,
not because it is necessarily dangerous. `ls` is harmless and `rm -rf /` is not, and nothing in the text
reliably separates them -- the existing regex is an attempt and it is honestly labelled as not being one. So a
shell command escalates unless the caller has said the workspace is the boundary, in which case the sandbox
already contains it and the classification drops to MEDIUM.

WHAT THIS IS NOT

Not a sandbox. It decides whether to ASK, and asking is a social control: a caller that ignores the verdict
has ignored it. The isolation is the container and the mount flags. This is the layer that stops an
unattended agent doing something irreversible without noticing, which is a different job from stopping it
being ABLE to.

Nor does it read intent. It classifies a tool call from its name and arguments, and a caller that can produce
a better verdict should say so -- `classify(..., override=)` exists for that rather than for convenience.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

LOW = "low"
MEDIUM = "medium"
HIGH = "high"

# Ordering matters for comparisons; the names are what a report shows.
_SEVERITY = {LOW: 0, MEDIUM: 1, HIGH: 2}

# Tools whose entire effect is reading. Confined and reversible by construction.
_READ_ONLY_TOOLS = frozenset({
    "read_file", "list_directory", "glob_search", "grep_search", "git_diff", "git_status",
    "session_status", "recall", "memory_recall", "swarm_list", "tool_search", "request_tools",
    "get_context_usage", "checkpoint_list", "skill_list", "mcp_list",
})

# Tools that change state INSIDE the confined workspace. Reversible by re-editing the same file.
_WORKSPACE_TOOLS = frozenset({
    "write_file", "edit_file", "apply_patch", "notebook_edit", "todo_write", "save_skill",
    "swarm_post", "swarm_claim", "swarm_finish", "swarm_release", "memory_share",
    "locus_goal_update", "locus_scratch", "checkpoint_restore", "branch_session",
})

# Tools that reach outside the workspace, cannot be undone, or spend money. These escalate.
_OUTBOUND_TOOLS = frozenset({
    "run_bash", "bash_output", "kill_bash", "run_python",      # unbounded: see the module docstring
    "web_search", "web_fetch",                                  # leave the machine
    "locus_run", "locus_create", "spawn_subagent", "task",      # spend tokens unattended
    "parallel_tasks", "send_to_subagent", "schedule_prompt",    # the same, multiplied or deferred
    "design_poster",                                            # writes an artifact outside the edit path
    "locus_watchlist", "locus_delete", "mcp__",
})

# Commands that are destructive regardless of where they run. Checked before the shell rule so the reason
# names the actual danger rather than the general one.
_DESTRUCTIVE = (
    (re.compile(r"\brm\s+-[a-z]*[rf]", re.IGNORECASE), "recursive force-delete"),
    (re.compile(r"\bRemove-Item\b[^\n]*-Recurse", re.IGNORECASE), "recursive delete"),
    (re.compile(r"\bformat\s+[A-Za-z]:", re.IGNORECASE), "filesystem format"),
    (re.compile(r"\bmkfs\.", re.IGNORECASE), "filesystem creation"),
    (re.compile(r"\bdiskpart\b", re.IGNORECASE), "partition editor"),
    (re.compile(r"\bgit\s+push\b[^\n]*--force", re.IGNORECASE), "force push"),
    (re.compile(r"\bshutdown\b|\breboot\b|\bhalt\b", re.IGNORECASE), "machine power control"),
    (re.compile(r":\(\)\s*\{.*\}\s*;?\s*:", re.DOTALL), "fork bomb"),
    (re.compile(r"\bdd\s+if=.*of=/dev/", re.IGNORECASE), "raw device write"),
    (re.compile(r">\s*/dev/[sh]d[a-z]", re.IGNORECASE), "raw device write"),
)

# Paths a write must not touch without being asked. Checked against the RESOLVED absolute path, because a
# relative path that escapes the workspace is the case a superficial check misses.
_PROTECTED_NAMES = frozenset({
    ".env", ".git", "id_rsa", "id_ed25519", "credentials", ".npmrc", ".pypirc", "known_hosts",
})


@dataclass
class Risk:
    """A verdict about one action."""

    level: str = MEDIUM
    reason: str = ""
    # True when the action cannot be undone by re-running something else -- a message sent, a file deleted,
    # money spent. Recorded separately from `level` because a caller may want to gate only on this.
    irreversible: bool = False

    @property
    def should_ask(self) -> bool:
        """Whether a person should be consulted before this runs."""
        return self.level == HIGH

    def to_dict(self) -> dict:
        return {"level": self.level, "reason": self.reason, "irreversible": self.irreversible,
                "should_ask": self.should_ask}


def classify(tool: str, args: dict | None = None, *, workspace: Path | str | None = None,
             confined: bool = True, override: Risk | None = None) -> Risk:
    """Decide how dangerous this action is.

    `confined` says whether the workspace is already a boundary the action cannot cross -- a sandboxed shell,
    or a Locus's confined mount. When it is true a shell command drops from HIGH to MEDIUM, because the thing
    that made it HIGH was being unbounded and it no longer is.

    `override` exists because a caller sometimes knows better than a name-and-arguments classifier. It is
    taken verbatim and its presence is the caller saying "I have already judged this".
    """
    if override is not None:
        return override

    name = str(tool or "")
    args = args if isinstance(args, dict) else {}

    if name in _READ_ONLY_TOOLS:
        return Risk(LOW, "reads only; confined and reversible")

    if name in _OUTBOUND_TOOLS or name.startswith("mcp__"):
        if name in ("run_bash", "bash_output", "kill_bash", "run_python"):
            command = str(args.get("command") or args.get("code") or "")
            for pattern, description in _DESTRUCTIVE:
                if pattern.search(command):
                    return Risk(HIGH, f"shell command performs a {description}", irreversible=True)
            if not confined:
                return Risk(HIGH, "shell command is unbounded: it can reach anything this account can",
                            irreversible=True)
            return Risk(MEDIUM, "shell command, but confined to the workspace by the sandbox")
        if name in ("web_search", "web_fetch"):
            return Risk(MEDIUM, "leaves the machine; read-only but not confined")
        return Risk(HIGH, f"{name} acts outside the workspace or spends tokens unattended")

    # A write: the risk is where it lands.
    if name in _WORKSPACE_TOOLS:
        target = str(args.get("path") or args.get("file_path") or "")
        if target:
            probe = _write_risk(target, workspace)
            if probe is not None:
                return probe
        return Risk(MEDIUM, "changes state inside the workspace; reversible by editing again")

    # Unknown tools are treated as outbound rather than as safe. A tool this module has never seen is exactly
    # the one whose blast radius is unknown, and the cost of asking is one prompt against the cost of an
    # irreversible mistake.
    return Risk(HIGH, f"{name or 'unknown tool'} is not classified; assume it can reach outside")


def _write_risk(target: str, workspace: Path | str | None) -> Risk | None:
    """A verdict when the write path itself is the danger, or None to keep the caller's default."""
    try:
        resolved = Path(target).expanduser()
        if not resolved.is_absolute():
            resolved = (Path(workspace) if workspace else Path.cwd()) / resolved
        resolved = resolved.resolve()
    except (OSError, ValueError):
        return None

    for part in resolved.parts:
        if part.lower() in _PROTECTED_NAMES:
            return Risk(HIGH, f"writes to {part!r}, which holds credentials or repository history",
                        irreversible=True)

    if workspace is not None:
        try:
            resolved.relative_to(Path(workspace).resolve())
        except ValueError:
            return Risk(HIGH, f"writes outside the workspace ({resolved})", irreversible=True)
    return None


def escalate_message(risk: Risk, tool: str) -> str:
    """What to say when a high-risk action is stopped.

    Written to be USEFUL to an agent rather than only alarming: it names the action, the reason, and the two
    legitimate ways forward. A refusal that only says no leaves a model to guess, and a model that guesses at
    a safety boundary guesses in the permissive direction.
    """
    return (
        f"Stopping before `{tool}`: {risk.reason}."
        + (" This cannot be undone." if risk.irreversible else "")
        + " If this is genuinely what you intend, ask the owner to confirm it. If it is not, take a narrower "
          "action instead -- work inside the workspace, or explain what you need and stop."
    )


def at_least(risk: Risk, level: str) -> bool:
    """Whether a verdict is at or above a level, for a caller gating on a threshold."""
    return _SEVERITY.get(risk.level, 1) >= _SEVERITY.get(level, 1)
