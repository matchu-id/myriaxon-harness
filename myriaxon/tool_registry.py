import concurrent.futures
import json
import sys
import threading
import time

from rich.console import Console as RichConsole
from rich.markup import escape
from rich.panel import Panel
from rich.text import Text

from . import (
    ask_user,
    audit,
    checkpoints,
    compaction_audit,
    self_status,
    session_lineage,
    loci,
    uncertainty,
    context_budget,
    locus_memory,
    locus_swarm,
    poster,
    hooks,
    memory_store,
    notebook,
    server_tools,
    web_tools,
    skills,
    todo,
    tools,
)
from .constants import LOOP_DETECTION_THRESHOLD, TASK_RESULT_CHAR_CAP
from .logging_setup import logger
from .tool_context import ToolContext

TASK_TOOL_DEF = {
    "name": "task",
    "description": (
        "Hand work to another agent: launch a sub-agent to autonomously perform a self-contained task and "
        "return its final answer. Delegate open-ended exploration or multi-step work that would otherwise "
        "consume a lot of your own context. The sub-agent shares your tools (except spawning further "
        "sub-agents) but starts with no conversation history — give it all the context it needs "
        "in the prompt."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "description": {"type": "string", "description": "A short (3-6 word) summary shown to the user."},
            "prompt": {"type": "string", "description": "The full task for the sub-agent, including all necessary context."},
            "subagent_type": {
                "type": "string",
                "description": "Optional named sub-agent type (see the listing in the system prompt). Defaults to general-purpose.",
            },
        },
        "required": ["description", "prompt"],
    },
}

SEND_TO_SUBAGENT_TOOL_DEF = {
    "name": "send_to_subagent",
    "description": (
        "Send a follow-up message to a sub-agent previously spawned by task (see the sub-agent "
        "id noted in that call's result), instead of it being fire-and-forget. The sub-agent "
        "keeps its own conversation history from prior messages. Only reachable from the same "
        "top-level agent that spawned it -- the registry is in-memory only and doesn't survive "
        "session resume."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "message": {"type": "string"},
        },
        "required": ["id", "message"],
    },
}

BUILTIN_TOOL_NAMES = {t["name"] for t in tools.TOOL_DEFS}


def _builtin_tool_names() -> set[str]:
    """Every tool name the built-ins define, for reserving against plugin use.

    Read from the same static table the builders use rather than from `build_tool_defs()`, because
    that builder now includes plugin tools -- the set would otherwise be computed from a list that
    depends on the very plugins it is meant to constrain.
    """
    names: set[str] = {t["name"] for t in tools.TOOL_DEFS if "name" in t}
    for module_def in _static_tool_defs():
        if isinstance(module_def, dict) and "name" in module_def:
            names.add(module_def["name"])
    return names


def _static_tool_defs() -> tuple:
    """Every statically-registered tool definition, in one place.

    A function, not a module-level tuple: several of these names (PARALLEL_TASKS_TOOL_DEF and
    friends) are defined further down this file, so building the tuple at import time raised
    NameError. Deferring it to first call also means a tool added later is picked up.
    """
    return (
        todo.TODO_WRITE_TOOL_DEF,
        TASK_TOOL_DEF,
        SEND_TO_SUBAGENT_TOOL_DEF,
        PARALLEL_TASKS_TOOL_DEF,
        notebook.NOTEBOOK_TOOL_DEF,
        skills.USE_SKILL_TOOL_DEF,
        skills.SAVE_SKILL_TOOL_DEF,
        ask_user.ASK_USER_QUESTION_TOOL_DEF,
        memory_store.SEARCH_MEMORY_TOOL_DEF,
        checkpoints.CHECKPOINT_LIST_TOOL_DEF,
        checkpoints.CHECKPOINT_RESTORE_TOOL_DEF,
        server_tools.WEB_SEARCH_TOOL,
        server_tools.WEB_FETCH_TOOL,
        # Discovery for the deferred groups, registered here so the names are reserved against a plugin claiming
        # them -- the same protection `request_tools` needs, and for the same reason: a plugin that shadowed the
        # tool the model uses to find tools could hide the whole deferred set.
        REQUEST_TOOLS_TOOL_DEF,
        TOOL_SEARCH_TOOL_DEF,
        # Reading back a pruned result. Registered here so the name is reserved against a plugin claiming it:
        # a plugin that shadowed `recall` could make an entire removed tool result unreachable, and the
        # placeholder in the transcript would still be naming a handle that no longer resolves.
        compaction_audit.RECALL_TOOL_DEF,
        # Persistent agents. Available to every provider, and to sub-agents as well -- a locus is
        # the mechanism for work that outlives one session, so withholding it from a delegated
        # run would mean a sub-agent could not hand something to the locus that owns it.
        loci.LOCUS_LIST_TOOL_DEF,
        loci.LOCUS_CREATE_TOOL_DEF,
        loci.LOCUS_SCRATCH_TOOL_DEF,
        session_lineage.BRANCH_TOOL_DEF,
        loci.HEARTBEAT_RESPOND_TOOL_DEF,
        loci.LOCUS_GOAL_TOOL_DEF,
        loci.LOCUS_RUN_TOOL_DEF,
        # Design. A locus asked to produce artwork needs a tool that actually draws, rather
        # than a description of one it would have to pretend it used.
        poster.POSTER_TOOL_DEF,
    *uncertainty.DECLARE_UNCERTAINTY_TOOL_DEFS,
    *context_budget.CONTEXT_TOOL_DEFS,
    *locus_memory.MEMORY_TOOL_DEFS,
    # The swarm board. Coordination across agents is what makes a Locus a Locus, so these belong in
    # the shared catalogue rather than only in a Locus's starting set -- a normal session delegating
    # work to peers needs them too.
    *locus_swarm.SWARM_TOOL_DEFS,
    )


# ------------------------------------------------------------------------------------------------
# Deferred tools: send a tool only when the turn might need it
#
# Measured: the 31 tool schemas are 17,670 chars (~4,417 tokens) serialised, against a 1,407-char
# (~351 token) system prompt. Tool schemas were therefore about twelve times the prompt, and they are
# charged on *every* request. Seven of those tools -- poster drawing, locus management, delegation,
# delayed prompts, file rollback, notebook editing, session memory search -- are real capabilities a
# coding turn almost never reaches for, and together they are 10,515 chars, 60% of the whole tool
# payload.
#
# So they are withheld until asked for, and `request_tools` ships in their place with a compact
# catalogue. This is progressive disclosure, the same shape the skill system already uses: the model
# is told the capability exists and pays for the body only when it wants it.
#
# What this deliberately is NOT: removing capability, or making the model guess. Every deferred group
# is named in the unlock tool's own description, so the model can see the door even while it cannot
# see the room. An unknown group name is refused with the valid list, so a wrong guess is a cheap
# correction rather than a dead end.
DEFERRED_GROUPS: dict[str, dict[str, object]] = {
    "background_jobs": {
        # The polling companions to `run_bash(run_in_background=true)`, which stays core together with `bash_output`
        # and `kill_bash`. Deferred for a measured reason rather than a taste one: adding these two to the always-sent
        # set pushed the core payload from under half the full set to 50.5% of it, failing
        # `test_the_default_set_is_meaningfully_smaller` and `test_the_offered_payload_shrinks_by_more_than_half`.
        #
        # Deferring costs nothing, because a deferred tool still WORKS the moment it is loaded -- the point recorded
        # at the deferral site. And the two halves remain reachable differently: a job can always be *started* with
        # the core `run_bash` and polled with the core `bash_output`, so this group is a convenience for waiting and
        # promoting, not the only way to reach a background job.
        "tools": ("bg_jobs", "bg_wait"),
        "summary": "list, wait for, or promote a background job started with run_bash",
    },
    "loci": {
        # `heartbeat_respond` is deliberately NOT in this group. It is the silent path for an unattended wake-up,
        # and an unattended wake-up cannot unlock a deferred tool: it would have to call `tool_search` first, which
        # is a paid turn spent discovering the tool whose entire purpose is to make a quiet turn cheap. Core, so a
        # heartbeat can always answer.
        "tools": ("locus_list", "locus_create", "locus_goal_update", "locus_run", "locus_watchlist"),
        "summary": "persistent agents that work between sessions, on a timer or on a condition",
    },
    # The swarm board. Its own group rather than folded into `tools`, because a single-agent session has no
    # use for it and the deferred-group mechanism is exactly how a catalogue stays small for the common
    # case. A Locus gets it in its starting set (see `_spawn_subagent_inner`), because coordination is the
    # point of a Locus rather than an occasional need.
    "swarm": {
        "tools": ("swarm_post", "swarm_list", "swarm_claim", "swarm_finish", "swarm_release"),
        "summary": "share work with other agents through a durable board",
    },
    # Collective memory. Deliberately NOT folded into the existing `memory` group, which is session
    # transcripts of your own past work: this is what PEERS have learned, and it is only useful to a
    # session that is coordinating with peers.
    "collective_memory": {
        "tools": ("memory_share", "memory_recall"),
        "summary": "record a finding for other agents, or see what they already learned",
    },
    # Level 5.8: the agent managing its own context rather than having it managed on its behalf.
    #
    # Deliberately NOT in a Locus's starting set, unlike the board and memory. A Locus runs on a step budget
    # and reaches for this when it needs it; adding two tools to every Locus turn to serve an occasional need
    # is how a catalogue grows without anybody deciding it should.
    "context": {
        "tools": ("check_context", "spill"),
        "summary": "see the context budget, or move a large result to disk and keep a pointer",
    },
    # Level 5.5: declaring how a claim is known. Offered to every session rather than only to Loci, because
    # the confusion it prevents -- a verified fact and a reasonable guess reading identically -- is at its
    # worst in a single interactive answer that nobody will ever re-read.
    "uncertainty": {
        "tools": ("declare_uncertainty",),
        "summary": "record whether a claim was checked, inferred, or assumed",
    },
    "poster": {
        "tools": ("design_poster",),
        "summary": "design a poster or graphic as SVG/PNG/PDF",
    },
    "checkpoints": {
        "tools": ("checkpoint_list", "checkpoint_restore"),
        "summary": "list or roll a file back to an earlier snapshot",
    },
    "schedule": {
        "tools": ("schedule_prompt", "list_scheduled", "cancel_scheduled"),
        "summary": "run a prompt later in this session",
    },
    "notebook": {
        "tools": ("edit_notebook",),
        "summary": "edit a Jupyter notebook cell",
    },
    "memory": {
        "tools": ("search_memory",),
        "summary": "search past sessions for something you did before",
    },
    "delegation": {
        "tools": ("task", "send_to_subagent", "parallel_tasks"),
        "summary": "hand work to sub-agents",
    },
}


def deferred_group_summary() -> str:
    """The one-line-per-group catalogue that goes in the unlock tool's description."""
    return "; ".join(
        f"{name} ({spec['summary']})" for name, spec in DEFERRED_GROUPS.items()
    )


REQUEST_TOOLS_TOOL_DEF = {
    "name": "request_tools",
    "description": (
        "Load hidden tools by group, when you already know the group name: "
        + deferred_group_summary()
        + ".\n\nMost turns never need this. If you know what you want to DO but not the group, call "
        "tool_search instead -- it finds the group for you. Loaded tools become callable on your next step "
        "and stay available for the rest of the conversation, so batch any others you expect to need into "
        "the same step."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "group": {
                "type": "string",
                "enum": sorted(DEFERRED_GROUPS),
                "description": "Which group to load.",
            },
        },
        "required": ["group"],
    },
}


TOOL_SEARCH_TOOL_DEF = {
    "name": "tool_search",
    "description": (
        "Find a tool you do not currently have, by describing what you want to do. Use this when the task "
        "needs a capability you cannot see. It searches the tools that are not currently listed, returns the "
        "best matches with their descriptions, and loads them so they become callable on your next step.\n\n"
        "Use this rather than request_tools when you know what you want to DO but not which group it lives in."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "What you want to do, in a few words -- not a tool name.",
            },
            "limit": {
                "type": "integer",
                "description": "How many matches to return (default 6, max 12).",
            },
        },
        "required": ["query"],
    },
}

# How many tool groups one `tool_search` may load. See the comment at its use site in `_tool_search`: a relative
# score cut alone cannot bound the worst case, because terms that appear in many descriptions cluster the scores
# near the best.
MAX_GROUPS_LOADED = 2


# A ceiling for the tool classes where a hang is possible and a budget is meaningful, keyed by class rather than
# listed per tool, so a tool added later is covered by its class.
#
# Deliberately conservative about WHICH classes. A deadline here is enforced by *abandoning* the call -- Python
# cannot kill a thread -- so it must only apply to a class of tool that binds what a tool *reaches*, not what it
# *takes*. An earlier version applied a flat 60-second deadline to every tool and calculated that it would
# truncate a legitimate 2 GB grep: the budget would not have been a safety net, it would have broken correct long
# work. Classes where "slow" is always pathological, and where abandoning is the right response:
#
#   * native/web/MCP calls -- a network or third-party service that has not answered is not going to
#   * browser and media capture -- same, and the process can wedge
#   * interactive prompts -- the failure being prevented is a question nobody can answer, not a slow one
#
# `run_bash` is NOT here: it takes its own `timeout` argument, so it already has a budget the model can set, and
# imposing a lower one silently would override the caller's intent. `task`, `parallel_tasks` and the scheduling
# tools are exempt in `deadline.NO_DEADLINE` because they legitimately run until a condition the harness cannot
# predict.
TOOL_DEADLINE_SECONDS: dict[str, float] = {
    "web_search": 90.0,
    "web_fetch": 90.0,
    "mcp_call": 120.0,
    "figma": 120.0,
}


def deadline_for_tool(name: str, definition: dict | None = None) -> float | None:
    """The budget for a tool, or None for "no deadline".

    Order matters. A per-tool declaration wins over the class default, so a tool can widen or narrow what its
    class allows. An explicit exemption (`deadline.NO_DEADLINE`) wins over everything, because those tools are
    listed precisely because a budget would break them rather than the tool.
    """
    from . import deadline as _deadline

    if name in _deadline.NO_DEADLINE:
        return None
    declared = _deadline.declared_timeout(definition or {}, name)
    if declared:
        return declared
    # MCP tools arrive with server-chosen names, so the class covers them by prefix rather than by name.
    if name.startswith("mcp__") or name.startswith("mcp_"):
        return TOOL_DEADLINE_SECONDS["mcp_call"]
    return TOOL_DEADLINE_SECONDS.get(name)


# Re-exported so a reader (and the verifier) can reach the definition through the registry, which is where every
# other tool's definition is looked for. `self_status` owns it; the name is bound here rather than duplicated.
SESSION_STATUS_TOOL_DEF = self_status.SESSION_STATUS_TOOL_DEF


def core_tool_names() -> set[str]:
    """Every tool that ships without being asked for: built-ins minus the deferred groups."""
    deferred = {name for spec in DEFERRED_GROUPS.values() for name in spec["tools"]}  # type: ignore[union-attr]
    return {name for name in _builtin_tool_names() if name not in deferred}


def apply_tool_visibility(tools: list[dict] | None, unlocked: set[str] | None = None) -> list[dict] | None:
    """Drop deferred tools the caller has not unlocked.

    Applied to every provider's tool list, at the single point where a profile's own withholding is
    applied, so all three providers behave identically. Plugin tools and MCP tools are never deferred:
    this function only knows the built-in groups, so anything else is passed straight through.
    """
    if not tools:
        return tools
    unlocked = unlocked or set()
    visible: set[str] = set()
    for group, spec in DEFERRED_GROUPS.items():
        if group in unlocked:
            visible |= set(spec["tools"])  # type: ignore[arg-type]
    deferred = {
        name for spec in DEFERRED_GROUPS.values() for name in spec["tools"]  # type: ignore[union-attr]
    } - visible
    if not deferred:
        return tools
    return [t for t in tools if _def_name(t) not in deferred]


def _definition_index() -> dict[str, dict]:
    """Every module-level `*_TOOL_DEF` in the package, keyed by tool name.

    The deferred tools' definitions live as separate dicts across the modules that own them -- `SEARCH_MEMORY_TOOL_DEF`
    in `memory_store`, `LOCUS_CREATE_TOOL_DEF` in `loci`, and so on. There is no single registry of them, which is
    why the first version of `catalog_for_deferred_tools` could not find a description and fell back to the group
    summary: the schema index it consulted holds only parameter schemas, never a `description`.

    Searching the modules that are already imported costs nothing extra -- they are loaded for their executors
    regardless -- and it means a tool added in a new module is picked up without editing a list here.
    """
    index: dict[str, dict] = {}
    for module in list(sys.modules.values()):
        if module is None or not getattr(module, "__name__", "").startswith("myriaxon."):
            continue
        for attribute in dir(module):
            if not attribute.endswith("_TOOL_DEF"):
                continue
            value = getattr(module, attribute, None)
            if isinstance(value, dict) and value.get("name"):
                index.setdefault(str(value["name"]), value)
    return index


def catalog_for_deferred_tools() -> list:
    """Everything NOT in the core set, as catalog entries, so it can be found by description.

    ## The gap this closes

    `tool_group` unlocks by **name**: the model must already know that `"memory"` is a group before it can ask
    for it. A model that has never seen this harness has no way to discover that a memory subsystem exists, so in
    practice these tools are reachable only by a caller who read the source. That is not deferral, it is hiding.

    ## Why this covers more than the deferred groups

    The catalog is built from the **live tool list minus the core set**, not from `DEFERRED_GROUPS`. Those are not
    the same thing, and conflating them nearly cost a real capability twice over:

      * `tool_search` is not in any group, so a group-based catalog would leave it unfindable -- the discovery
        tool would itself be undiscoverable;
      * `recall` is what makes a pruned tool result recoverable, so putting it *in* a group would mean a model
        looking at a pruned placeholder could not ask for the content back without first knowing a group name.

    Taking the difference from the core set instead means a tool added later is covered by search automatically,
    whether or not somebody remembers to add it to a group.

    Everything here is a trusted built-in, so there is no untrusted schema for the catalog's traversal guard to
    protect.
    """
    from . import tool_catalog

    definitions = _definition_index()
    core = core_tool_names()

    # The live list when one is available -- it includes this session's plugin and MCP tools -- falling back to
    # the static definition table so the catalog works before an agent exists.
    names: list[str] = []
    try:
        for definition in build_tool_defs():
            name = _def_name(definition)
            if name and name not in core:
                names.append(name)
    except Exception:
        pass
    if not names:
        names = [n for n in sorted(definitions) if n not in core]

    entries: list[dict] = []
    seen: set[str] = set()
    for name in names:
        if name in seen:
            continue
        seen.add(name)
        definition = definitions.get(name) or {}
        description = str(definition.get("description") or "").strip()
        if not description:
            group = group_for_tool(name)
            # Named as a fallback rather than invented. A missing description is a defect in the tool
            # definition, and saying so is better than a plausible sentence that is not true.
            summary = str((DEFERRED_GROUPS.get(group) or {}).get("summary") or "").strip()
            note = f" (group: {group})" if group else ""
            description = f"{summary}{note}".strip() or f"{name} (no description declared)"
        entries.append({
            "name": name,
            "description": description,
            "input_schema": definition.get("input_schema") or _tool_schema(name) or {},
        })
    return tool_catalog.build_entries(entries, source=tool_catalog.OPENCLAW)


def _tool_schema(name: str) -> dict:
    try:
        schema = _get_tool_schema_index().get(name)
    except Exception:
        return {}
    return schema if isinstance(schema, dict) else {}


def group_for_tool(name: str) -> str:
    """Which group a tool belongs to, or "" when it ships by default."""
    for group, spec in DEFERRED_GROUPS.items():
        if name in spec["tools"]:  # type: ignore[operator]
            return group
    return ""


def _def_name(tool: dict) -> str:
    """A tool definition's name in either provider shape."""
    if not isinstance(tool, dict):
        return ""
    if tool.get("name"):
        return str(tool["name"])
    function = tool.get("function")
    if isinstance(function, dict) and function.get("name"):
        return str(function["name"])
    return ""


def plugin_tool_defs() -> list[dict]:
    """Tool definitions contributed by plugins, minus any that would shadow a built-in.

    The built-in names are handed to the plugin registry as *reserved* before any plugin registers,
    so a plugin cannot claim one. Verified failure without that: a plugin registering `write_file`
    intercepted the real tool -- identical name and arguments, silently routed to the plugin's
    handler.
    """
    from .plugins import get_registry

    registry = get_registry()
    registry.set_reserved_names(_builtin_tool_names())
    if not registry.loaded:
        return []
    return registry.tool_defs()


def _builtin_tool_defs(
    mcp_tool_defs: list[dict] | None = None,
    include_task: bool = True,
) -> list[dict]:
    """The built-in tool set only. Used wherever the *built-ins* must be known independently of any
    plugin: reserving names, and building the schema index used to validate arguments.

    Measured bug this prevents: the schema index was built from `build_tool_defs()`, which now
    includes plugin tools, so a plugin shadowing `write_file` replaced its schema in the index and the
    real `write_file` was then validated against the plugin's schema -- a silent, confusing failure
    that only appeared when a shadowing plugin was present.
    """
    defs = [t for t in tools.TOOL_DEFS]
    defs.append(server_tools.WEB_SEARCH_TOOL)
    defs.append(server_tools.WEB_FETCH_TOOL)
    defs.append(todo.TODO_WRITE_TOOL_DEF)
    if include_task:
        defs.append(TASK_TOOL_DEF)
    defs.append(notebook.NOTEBOOK_TOOL_DEF)
    defs.append(skills.USE_SKILL_TOOL_DEF)
    defs.append(skills.SAVE_SKILL_TOOL_DEF)
    defs.append(checkpoints.CHECKPOINT_LIST_TOOL_DEF)
    defs.append(checkpoints.CHECKPOINT_RESTORE_TOOL_DEF)
    defs.append(ask_user.ASK_USER_QUESTION_TOOL_DEF)
    defs.append(memory_store.SEARCH_MEMORY_TOOL_DEF)
    # Persistent agents (loci). Registered for every provider and in the built-in set used
    # for argument validation: a locus is how work outlives a session, so it cannot be
    # provider-specific, and a tool absent from the schema index is not validated at all.
    defs.append(loci.LOCUS_LIST_TOOL_DEF)
    defs.append(loci.LOCUS_CREATE_TOOL_DEF)
    defs.append(loci.LOCUS_SCRATCH_TOOL_DEF)
    defs.append(session_lineage.BRANCH_TOOL_DEF)
    defs.append(loci.HEARTBEAT_RESPOND_TOOL_DEF)
    defs.append(loci.LOCUS_GOAL_TOOL_DEF)
    defs.append(loci.LOCUS_RUN_TOOL_DEF)
    defs.append(poster.POSTER_TOOL_DEF)
    for _def in uncertainty.DECLARE_UNCERTAINTY_TOOL_DEFS:
        defs.append(_def)
    for _def in context_budget.CONTEXT_TOOL_DEFS:
        defs.append(_def)
    for _def in locus_memory.MEMORY_TOOL_DEFS:
        defs.append(_def)
    for _def in locus_swarm.SWARM_TOOL_DEFS:
        defs.append(_def)
    for _swarm_def in locus_swarm.SWARM_TOOL_DEFS:
        defs.append(_swarm_def)
    if include_task:
        defs.append(SEND_TO_SUBAGENT_TOOL_DEF)
        defs.append(PARALLEL_TASKS_TOOL_DEF)
    # The unlock tool ships with the core set, because it is the one that has to be visible for any
    # of the others to be reachable.
    defs.append(REQUEST_TOOLS_TOOL_DEF)
    defs.append(TOOL_SEARCH_TOOL_DEF)
    defs.append(compaction_audit.RECALL_TOOL_DEF)
    defs.append(self_status.SESSION_STATUS_TOOL_DEF)
    for tool_def in mcp_tool_defs or []:
        defs.append(tool_def)
    return defs


def build_tool_defs(
    mcp_tool_defs: list[dict] | None = None,
    url_mcp_server_names: list[str] | None = None,
    include_task: bool = True,
) -> list[dict]:
    defs = list(tools.TOOL_DEFS)
    defs.append(server_tools.WEB_SEARCH_TOOL)
    defs.append(server_tools.WEB_FETCH_TOOL)
    defs.append(todo.TODO_WRITE_TOOL_DEF)
    if include_task:
        defs.append(TASK_TOOL_DEF)
    defs.append(notebook.NOTEBOOK_TOOL_DEF)
    defs.append(skills.USE_SKILL_TOOL_DEF)
    defs.append(skills.SAVE_SKILL_TOOL_DEF)
    defs.append(checkpoints.CHECKPOINT_LIST_TOOL_DEF)
    defs.append(checkpoints.CHECKPOINT_RESTORE_TOOL_DEF)
    defs.append(ask_user.ASK_USER_QUESTION_TOOL_DEF)
    defs.append(memory_store.SEARCH_MEMORY_TOOL_DEF)
    # Persistent agents (loci). Registered for every provider and in the built-in set used
    # for argument validation: a locus is how work outlives a session, so it cannot be
    # provider-specific, and a tool absent from the schema index is not validated at all.
    defs.append(loci.LOCUS_LIST_TOOL_DEF)
    defs.append(loci.LOCUS_CREATE_TOOL_DEF)
    defs.append(loci.LOCUS_SCRATCH_TOOL_DEF)
    defs.append(session_lineage.BRANCH_TOOL_DEF)
    defs.append(loci.HEARTBEAT_RESPOND_TOOL_DEF)
    defs.append(loci.LOCUS_GOAL_TOOL_DEF)
    defs.append(loci.LOCUS_RUN_TOOL_DEF)
    defs.append(poster.POSTER_TOOL_DEF)
    for _def in uncertainty.DECLARE_UNCERTAINTY_TOOL_DEFS:
        defs.append(_def)
    for _def in context_budget.CONTEXT_TOOL_DEFS:
        defs.append(_def)
    for _def in locus_memory.MEMORY_TOOL_DEFS:
        defs.append(_def)
    for _def in locus_swarm.SWARM_TOOL_DEFS:
        defs.append(_def)
    for _swarm_def in locus_swarm.SWARM_TOOL_DEFS:
        defs.append(_swarm_def)
    if include_task:
        defs.append(SEND_TO_SUBAGENT_TOOL_DEF)
        defs.append(PARALLEL_TASKS_TOOL_DEF)
    # The unlock tool ships with the core set, because it is the one that has to be visible for any of
    # the deferred groups to be reachable at all.
    #
    # Added here as well as in `_builtin_tool_defs`: those two builders list the same tools in two
    # places, and editing only one is exactly the bug this change first shipped with -- `request_tools`
    # was in the validation index and absent from every list actually sent, so the saving applied and
    # the unlock did not exist. `test_the_two_builders_agree` now pins the invariant.
    defs.append(REQUEST_TOOLS_TOOL_DEF)
    defs.append(TOOL_SEARCH_TOOL_DEF)
    defs.append(compaction_audit.RECALL_TOOL_DEF)
    defs.append(self_status.SESSION_STATUS_TOOL_DEF)

    for tool_def in plugin_tool_defs():
        defs.append(tool_def)
    for tool_def in mcp_tool_defs or []:
        defs.append(tool_def)
    for server_name in url_mcp_server_names or []:
        defs.append({"type": "mcp_toolset", "mcp_server_name": server_name})

    return defs


def _to_openai_function(tool_def: dict) -> dict:
    """Convert an Anthropic-shaped tool def ({name, description, input_schema}) to OpenAI's
    function-calling shape ({type: "function", function: {name, description, parameters}})."""
    return {
        "type": "function",
        "function": {
            "name": tool_def["name"],
            "description": tool_def["description"],
            "parameters": tool_def["input_schema"],
        },
    }


def build_tool_defs_openai(
    mcp_tool_defs: list[dict] | None = None,
    include_task: bool = True,
) -> list[dict]:
    """Same core tool set as build_tool_defs, minus the Anthropic-only server tools and remote
    MCP connector (web_search/web_fetch/mcp_toolset have no OpenAI-compatible equivalent)."""
    defs = list(tools.TOOL_DEFS)
    defs.append(todo.TODO_WRITE_TOOL_DEF)
    if include_task:
        defs.append(TASK_TOOL_DEF)
    defs.append(notebook.NOTEBOOK_TOOL_DEF)
    defs.append(skills.USE_SKILL_TOOL_DEF)
    defs.append(skills.SAVE_SKILL_TOOL_DEF)
    defs.append(checkpoints.CHECKPOINT_LIST_TOOL_DEF)
    defs.append(checkpoints.CHECKPOINT_RESTORE_TOOL_DEF)
    defs.append(ask_user.ASK_USER_QUESTION_TOOL_DEF)
    defs.append(memory_store.SEARCH_MEMORY_TOOL_DEF)
    # Persistent agents (loci). Registered for every provider and in the built-in set used
    # for argument validation: a locus is how work outlives a session, so it cannot be
    # provider-specific, and a tool absent from the schema index is not validated at all.
    defs.append(loci.LOCUS_LIST_TOOL_DEF)
    defs.append(loci.LOCUS_CREATE_TOOL_DEF)
    defs.append(loci.LOCUS_SCRATCH_TOOL_DEF)
    defs.append(session_lineage.BRANCH_TOOL_DEF)
    defs.append(loci.HEARTBEAT_RESPOND_TOOL_DEF)
    defs.append(loci.LOCUS_GOAL_TOOL_DEF)
    defs.append(loci.LOCUS_RUN_TOOL_DEF)
    defs.append(poster.POSTER_TOOL_DEF)
    for _def in uncertainty.DECLARE_UNCERTAINTY_TOOL_DEFS:
        defs.append(_def)
    for _def in context_budget.CONTEXT_TOOL_DEFS:
        defs.append(_def)
    for _def in locus_memory.MEMORY_TOOL_DEFS:
        defs.append(_def)
    for _def in locus_swarm.SWARM_TOOL_DEFS:
        defs.append(_def)
    for _swarm_def in locus_swarm.SWARM_TOOL_DEFS:
        defs.append(_swarm_def)
    # Local web access. Anthropic runs these server-side, so its builder gets them from
    # `_static_tool_defs`; every other provider cannot, and without these the model is
    # offered two tools whose every call returns "Unknown tool" -- which reads as a broken
    # harness rather than as a missing capability.
    defs.append(web_tools.WEB_SEARCH_TOOL_DEF)
    defs.append(web_tools.WEB_FETCH_TOOL_DEF)
    if include_task:
        defs.append(SEND_TO_SUBAGENT_TOOL_DEF)
        defs.append(PARALLEL_TASKS_TOOL_DEF)
    # The unlock tool. Without it here, `apply_tool_visibility` still withholds the deferred groups
    # and nothing can load them back -- which is exactly what shipped: on OpenAI-compatible, DeepSeek,
    # OpenRouter, Netra and Gemini, twelve tools were withheld with no way to reach them. The
    # Anthropic builder had it, so the bug was invisible on the provider used for development.
    defs.append(REQUEST_TOOLS_TOOL_DEF)
    defs.append(TOOL_SEARCH_TOOL_DEF)
    defs.append(compaction_audit.RECALL_TOOL_DEF)
    defs.append(self_status.SESSION_STATUS_TOOL_DEF)

    for tool_def in plugin_tool_defs():
        defs.append(tool_def)
    for tool_def in mcp_tool_defs or []:
        defs.append(tool_def)

    return [_to_openai_function(d) for d in defs]


def _to_gemini_function(tool_def: dict) -> dict:
    """Convert an Anthropic-shaped tool def to Gemini's flat FunctionParam shape
    ({type: "function", name, description, parameters}) -- confirmed via real field
    inspection of the installed google-genai package's FunctionParam TypedDict."""
    return {
        "type": "function",
        "name": tool_def["name"],
        "description": tool_def["description"],
        "parameters": tool_def["input_schema"],
    }


def build_tool_defs_gemini(
    mcp_tool_defs: list[dict] | None = None,
    include_task: bool = True,
) -> list[dict]:
    """Same core tool set as build_tool_defs, minus the Anthropic-only server tools and remote
    MCP connector. Also appends Gemini's own native google_search grounding tool (confirmed
    present in the real API's ToolParam union) -- UNVERIFIED whether it coexists with function
    declarations in the same request (blocked on exhausted Gemini quota at the time this was
    written); flag clearly and fix on first real 400 error if it turns out they're mutually
    exclusive, same discipline as the original Gemini message-shape work."""
    defs = list(tools.TOOL_DEFS)
    defs.append(todo.TODO_WRITE_TOOL_DEF)
    if include_task:
        defs.append(TASK_TOOL_DEF)
    defs.append(notebook.NOTEBOOK_TOOL_DEF)
    defs.append(skills.USE_SKILL_TOOL_DEF)
    defs.append(skills.SAVE_SKILL_TOOL_DEF)
    defs.append(checkpoints.CHECKPOINT_LIST_TOOL_DEF)
    defs.append(checkpoints.CHECKPOINT_RESTORE_TOOL_DEF)
    defs.append(ask_user.ASK_USER_QUESTION_TOOL_DEF)
    defs.append(memory_store.SEARCH_MEMORY_TOOL_DEF)
    # Persistent agents (loci). Registered for every provider and in the built-in set used
    # for argument validation: a locus is how work outlives a session, so it cannot be
    # provider-specific, and a tool absent from the schema index is not validated at all.
    defs.append(loci.LOCUS_LIST_TOOL_DEF)
    defs.append(loci.LOCUS_CREATE_TOOL_DEF)
    defs.append(loci.LOCUS_SCRATCH_TOOL_DEF)
    defs.append(session_lineage.BRANCH_TOOL_DEF)
    defs.append(loci.HEARTBEAT_RESPOND_TOOL_DEF)
    defs.append(loci.LOCUS_GOAL_TOOL_DEF)
    defs.append(loci.LOCUS_RUN_TOOL_DEF)
    defs.append(poster.POSTER_TOOL_DEF)
    for _def in uncertainty.DECLARE_UNCERTAINTY_TOOL_DEFS:
        defs.append(_def)
    for _def in context_budget.CONTEXT_TOOL_DEFS:
        defs.append(_def)
    for _def in locus_memory.MEMORY_TOOL_DEFS:
        defs.append(_def)
    for _def in locus_swarm.SWARM_TOOL_DEFS:
        defs.append(_def)
    for _swarm_def in locus_swarm.SWARM_TOOL_DEFS:
        defs.append(_swarm_def)
    # Local web access. Anthropic runs these server-side, so its builder gets them from
    # `_static_tool_defs`; every other provider cannot, and without these the model is
    # offered two tools whose every call returns "Unknown tool" -- which reads as a broken
    # harness rather than as a missing capability.
    defs.append(web_tools.WEB_SEARCH_TOOL_DEF)
    defs.append(web_tools.WEB_FETCH_TOOL_DEF)
    if include_task:
        defs.append(SEND_TO_SUBAGENT_TOOL_DEF)
        defs.append(PARALLEL_TASKS_TOOL_DEF)
    # The unlock tool. See the note in build_tool_defs_openai: omitting it here withheld twelve tools
    # with no way to load them back on every Gemini session.
    defs.append(REQUEST_TOOLS_TOOL_DEF)
    defs.append(TOOL_SEARCH_TOOL_DEF)
    defs.append(compaction_audit.RECALL_TOOL_DEF)
    defs.append(self_status.SESSION_STATUS_TOOL_DEF)

    for tool_def in plugin_tool_defs():
        defs.append(tool_def)
    for tool_def in mcp_tool_defs or []:
        defs.append(tool_def)

    # google_search is already in Gemini's native tool shape (not Anthropic-shaped), so it's
    # appended after conversion rather than passed through _to_gemini_function.
    return [_to_gemini_function(d) for d in defs] + [{"type": "google_search"}]


_SUBAGENT_EXCLUDED_TOOLS = ("task", "ask_user_question", "send_to_subagent", "parallel_tasks")
_subagent_registry_lock = threading.Lock()
# The hard ceiling on sub-agents running at once, enforced rather than merely requested.
#
# This is a correctness limit, not a cost preference. Every sub-agent shares one ToolContext
# (background_procs, todos, living_subagents), all of them write against the same process
# working directory, and each completed write records a checkpoint via `git add -A` + `git
# commit` into a single shadow repository -- so concurrent sub-agents contend on one git index
# and one working tree. Past a small number the extra agents stop adding throughput and start
# producing index.lock failures and interleaved edits to the same files.
#
# Previously this was only implied by the tool description ("multiple ... concurrently", no
# number) and enforced nowhere: a single `parallel_tasks` call with twenty entries started
# twenty agents. A limit that lives only in a prompt is not a limit.
MAX_PARALLEL_SUBAGENTS = 3

# Bounds how many sub-agents may run at once *across the whole session*, so nesting or a
# sequence of calls cannot multiply past the ceiling. Declared here rather than on the context
# because it is a property of the process, not of a conversation.
_subagent_slots = threading.BoundedSemaphore(MAX_PARALLEL_SUBAGENTS)

# How long a caller waits for a free sub-agent slot before giving up.
#
# Generous on purpose -- sub-agents legitimately take minutes and queueing is the intended behaviour
# -- but FINITE, because the previous unbounded `acquire` was a permanent hang whenever every slot was
# held by a thread that could not finish. Measured: three threads each spawning a nested sub-agent left
# all three parked in `acquire` with no way out. A task that fails after two minutes is recoverable; a
# wedged process is not.
SUBAGENT_SLOT_WAIT_SECONDS = 120.0

PARALLEL_TASKS_TOOL_DEF = {
    "name": "parallel_tasks",
    "description": (
        f"Launch multiple independent sub-agents concurrently (at most {MAX_PARALLEL_SUBAGENTS} "
        "run at once; extra tasks queue) and wait for all of them to finish, instead of calling "
        "task repeatedly one at a time. Use only when the tasks are genuinely independent (e.g. "
        "investigate three unrelated files/areas) -- each sub-agent shares your tools but starts "
        "with no conversation history and cannot see the others' work or results, so don't use "
        "this when one task's output feeds another."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "tasks": {
                "type": "array",
                "minItems": 2,
                "maxItems": MAX_PARALLEL_SUBAGENTS,
                "items": {
                    "type": "object",
                    "properties": {
                        "description": {"type": "string"},
                        "prompt": {"type": "string"},
                        "subagent_type": {"type": "string"},
                    },
                    "required": ["description", "prompt"],
                },
            }
        },
        "required": ["tasks"],
    },
}


def _spawn_subagent(
    prompt: str, subagent_type: str | None, ctx: ToolContext, quiet: bool = False,
    persona: str = "",
) -> tuple[str, bool]:
    """Shared by execute_task and execute_parallel_tasks. quiet=True (used for parallel runs)
    gives the sub-agent its own fully-suppressed console instead of the shared one -- Rich's
    Live display doesn't support multiple concurrent live regions on the same console (it
    silently stacks rather than raising, so the symptom would be garbled/suppressed streaming
    output, not a crash), so concurrent sub-agents must not share a Live-rendering console.

    The whole body runs while holding one of ``MAX_PARALLEL_SUBAGENTS`` session-wide slots. The
    acquisition is here rather than in the callers so that *every* path into a sub-agent is
    bounded by the same ceiling -- a nesting sub-agent, a `task` call, and a `parallel_tasks`
    batch all contend for the same slots. A caller that has more work than slots blocks until
    one frees up, which is the intended behaviour: queueing is safe, over-subscription is not.

    NESTED SPAWNING IS REFUSED, and that guard is what makes the ceiling safe rather than a
    deadlock. A sub-agent holds a slot for its whole run; if it could spawn, it would wait for a
    slot it is itself occupying. With three `parallel_tasks` entries each spawning, all three slots
    are held by parents parked in `acquire` and nothing can ever release one -- measured: three
    threads through this function with a nested `run_turn` left `_value == 0` with every thread
    blocked, and the cancel token cannot break it because it is only checked inside the stream loop.
    Nesting also buys little: a sub-agent's window is smaller than its parent's, so work that does not
    fit upstairs rarely fits downstairs. Refused with a message the model can act on rather than
    silently flattened, because "the tool said nothing" reads as a harness bug.

    The timeout is a second line of defence for any path that reaches `acquire` while holding a slot
    by some route not covered above. Failing a task after two minutes is a bad outcome; hanging the
    process forever is a worse one.
    """
    if getattr(ctx, "_in_subagent", False):
        return (
            "Sub-agents cannot spawn further sub-agents. Do this work with your own tools, or return "
            "and let the parent delegate it separately.",
            True,
        )
    acquired = _subagent_slots.acquire(timeout=SUBAGENT_SLOT_WAIT_SECONDS)
    if not acquired:
        return (
            f"No sub-agent slot was free within {SUBAGENT_SLOT_WAIT_SECONDS}s. The other sub-agents "
            "are still running; retry shortly or do the work with your own tools.",
            True,
        )
    try:
        return _spawn_subagent_inner(prompt, subagent_type, ctx, quiet, persona)
    finally:
        _subagent_slots.release()


def _spawn_subagent_inner(
    prompt: str, subagent_type: str | None, ctx: ToolContext, quiet: bool = False,
    persona: str = "",
) -> tuple[str, bool]:
    type_name = subagent_type or "general-purpose"
    stype = ctx.subagent_types.get(type_name) or ctx.subagent_types.get("general-purpose")

    subagent_tools = [t for t in ctx.tools if t.get("name") not in _SUBAGENT_EXCLUDED_TOOLS]
    if stype is not None and stype.tool_allowlist is not None:
        subagent_tools = [t for t in subagent_tools if t.get("name") in stype.tool_allowlist]

    full_prompt = prompt
    if stype is not None and stype.instructions:
        full_prompt = f"{stype.instructions}\n\n{full_prompt}"

    # Auto-trigger reflection: baked into the existing prompt (not a separate follow-up
    # run_turn call) so it costs nothing extra -- the sub-agent considers it as part of the one
    # turn it was already going to do. Only appended if save_skill actually survived the
    # subagent_type's tool allowlist (e.g. "explore" doesn't get it).
    if any(t.get("name") == "save_skill" for t in subagent_tools):
        full_prompt = (
            f"{full_prompt}\n\n"
            "Before concluding, consider: did you discover a genuinely reusable approach here? "
            "If so, call save_skill to preserve it for future tasks. If nothing here is "
            "generalizable, don't call it -- that's a normal, expected outcome."
        )

    sub = ctx.agent_factory(tools=subagent_tools)
    # Marks the sub-agent's OWN context as one layer down, which is what `_spawn_subagent` reads to
    # refuse a nested spawn. Set on the sub-agent rather than the parent so the refusal applies to the
    # child and not to the session that legitimately delegates.
    if getattr(sub, "tool_context", None) is not None:
        sub.tool_context._in_subagent = True
        # A confined Locus inherits its boundary. `execute_locus_run` sets `locus_workspace` on the
        # PARENT context for the duration of the run, and this copies it to the sub-agent that actually
        # executes the turn -- without it the permission layer would see an ordinary sub-agent and go
        # back to prompting a console nobody is reading, which is the failure this whole path fixes.
        if getattr(ctx, "locus_workspace", ""):
            sub.tool_context.locus_workspace = ctx.locus_workspace
        # The sandbox setting travels with the boundary, and forgetting it made every Locus shell
        # command fail.
        #
        # MEASURED, and it is the root cause of seven benchmark runs of apparent model stubbornness:
        # the model DID call `run_bash` every time, and the permission layer refused it with
        # "a shell command cannot be confined to this Locus's workspace, and no sandbox is available".
        # The parent context had `sandbox_enabled = True` -- the harness sets it automatically when
        # Docker is present -- but the sub-agent that actually executes the turn got a fresh
        # ToolContext where the flag defaults to False. The guard then read the sub-agent's context,
        # concluded no sandbox existed, and blocked the call.
        #
        # Everything downstream followed from that one missing line: the model was told the shell was
        # blocked, correctly repeated it, and I spent four attempts rewriting its description and
        # briefing to correct a claim that was TRUE. The agent was never confused; the harness was.
        if getattr(ctx, "sandbox_enabled", False):
            sub.tool_context.sandbox_enabled = True
        # A Locus always gets its OWN tool group, without spending a step asking for it.
        #
        # Measured in the first benchmark run: the brief tells a self-directed Locus to record
        # objectives with `locus_goal_update`, and the tool was not in its starting set -- so it
        # called `request_tools`, was told the group would load on the NEXT step, spent a step
        # waiting, and then ran out of budget without ever recording anything. `request_tools` exists
        # to keep a large catalogue out of the context of a NORMAL turn; a Locus's own bookkeeping
        # tools are not a catalogue, they are the mechanism by which it keeps its identity, and
        # deferring them costs a step of every run.
        if persona:
            try:
                # Built from the same module attributes the core catalogue uses, so a Locus's tool set
                # cannot drift from the definitions the dispatcher actually validates against.
                #
                # The name lives at `["function"]["name"]`, NOT `["name"]`. That shape cost a
                # debugging round: the first version deduped on `t.get("name")`, which is None for
                # every OpenAI-format tool, so the check passed trivially and the tools were appended
                # even when already present. Reading the structure instead of assuming it is what
                # found this.
                def _tool_name(definition: dict) -> str:
                    function = definition.get("function")
                    if isinstance(function, dict):
                        return str(function.get("name") or "")
                    return str(definition.get("name") or "")

                already = {_tool_name(t) for t in subagent_tools}
                for definition in (loci.LOCUS_GOAL_TOOL_DEF, loci.LOCUS_SCRATCH_TOOL_DEF,
                                   loci.LOCUS_LIST_TOOL_DEF):
                    if _tool_name(definition) not in already:
                        subagent_tools.append(definition)

                # The swarm board, by the same argument. A Locus that cannot see the board cannot
                # coordinate, and coordination is what makes it a Locus rather than a scheduled script.
                # Deferring these behind `request_tools` would cost a step of every run that needs them --
                # and the step it costs is the one where it would have noticed a peer had already done the
                # work.
                #
                # Appended as raw definitions rather than unlocked as a group: `unlocked_tool_groups` is
                # the per-turn deferral mechanism for a NORMAL session, and a Locus's coordination surface
                # is not a catalogue entry. The names must stay in step with `DEFERRED_GROUPS["swarm"]`,
                # which is how a normal session reaches them.
                from . import locus_swarm as _swarm

                for definition in _swarm.SWARM_TOOL_DEFS:
                    if _tool_name(definition) not in already:
                        subagent_tools.append(definition)

                # Collective memory, for the same reason the board is here: a Locus that cannot read what
                # its peers learned will rediscover it, and one that cannot record what it learned makes the
                # next Locus repeat the work. Memory is the other half of coordination.
                from . import locus_memory as _memory

                for definition in _memory.MEMORY_TOOL_DEFS:
                    if _tool_name(definition) not in already:
                        subagent_tools.append(definition)
            except Exception:  # noqa: BLE001 - a missing definition must not break the run
                logger.debug("could not add the loci tools to a Locus", exc_info=True)
    if persona:
        # Set before `run_turn`, which is what builds the system prompt. A locus running through this
        # path has to speak as that locus: sending its briefing as a message while the system prompt
        # still said "You are Myriaxon, a terminal-based coding agent" is why `/loci run analyst
        # "are you analyst?"` answered "I'm a coding agent". The identity has to change, not just the
        # prompt, because a request cannot override an instruction defining the same agent.
        sub.persona_identity = persona
    if quiet:
        quiet_console = RichConsole(quiet=True)
        sub.console = quiet_console
        sub.tool_context.console = quiet_console

    with _subagent_registry_lock:
        ctx.subagent_id_counter += 1
        sub_id = f"subagent_{ctx.subagent_id_counter}"
        ctx.living_subagents[sub_id] = sub

    sub.run_turn(full_prompt)
    text = sub.last_assistant_text()
    if len(text) > TASK_RESULT_CHAR_CAP:
        text = text[:TASK_RESULT_CHAR_CAP] + "\n... (truncated)"
    return sub_id, text


def _task_specs(raw) -> tuple[list[dict] | None, str]:
    """Sub-agent task specs, accepting what a model plainly means.

    A plain string is a prompt; a spec without a description gets one from its prompt. A spec with no prompt at all is the
    one thing that cannot be guessed, and is refused by number so the model can fix that one.
    """
    specs: list[dict] = []
    for index, item in enumerate(raw or [], start=1):
        if isinstance(item, str):
            item = {"prompt": item}
        if not isinstance(item, dict):
            return None, f"task {index} must be an object with 'prompt' and an optional 'description', got {type(item).__name__}."
        prompt = str(item.get("prompt") or item.get("task") or item.get("instructions") or "").strip()
        if not prompt:
            return None, f"task {index} has no 'prompt'."
        spec = dict(item)
        spec["prompt"] = prompt
        spec["description"] = str(item.get("description") or "").strip() or " ".join(prompt.split())[:50]
        specs.append(spec)
    if not specs:
        return None, "give at least one task."
    return specs, ""


def execute_task(tool_input: dict, ctx: ToolContext) -> tuple[str, bool]:
    type_name = tool_input.get("subagent_type", "general-purpose")
    description = str(tool_input.get("description") or "").strip() or " ".join(str(tool_input["prompt"]).split())[:50]
    ctx.console.print(
        Panel(
            Text(tool_input["prompt"]),
            title=f"[tool.name]Task ({type_name}):[/] {escape(description)}",
            border_style="tool.pending",
        )
    )
    sub_id, text = _spawn_subagent(tool_input["prompt"], tool_input.get("subagent_type"), ctx)
    return f"(sub-agent id: {sub_id} -- use send_to_subagent to follow up)\n\n{text}", False


def execute_parallel_tasks(tool_input: dict, ctx: ToolContext) -> tuple[str, bool]:
    task_specs, problem = _task_specs(tool_input["tasks"])
    if task_specs is None:
        return f"parallel_tasks: {problem}", True
    for spec in task_specs:
        ctx.console.print(
            Panel(
                Text(spec["prompt"]),
                title=(
                    f"[tool.name]Task ({spec.get('subagent_type', 'general-purpose')}, "
                    f"parallel):[/] {escape(spec['description'])}"
                ),
                border_style="tool.pending",
            )
        )

    results: list[tuple[str, str, str] | None] = [None] * len(task_specs)
    # max_workers is capped even though the schema already caps the list: the schema is advisory
    # (a provider may send more items than maxItems, and MCP-derived callers never see it at all),
    # while this is the enforcement point. Excess tasks queue rather than being rejected, so an
    # over-eager model still gets its work done, just serialised.
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=min(len(task_specs), MAX_PARALLEL_SUBAGENTS)
    ) as pool:
        future_to_index = {
            pool.submit(_spawn_subagent, spec["prompt"], spec.get("subagent_type"), ctx, True): i
            for i, spec in enumerate(task_specs)
        }
        for future in concurrent.futures.as_completed(future_to_index):
            i = future_to_index[future]
            try:
                sub_id, text = future.result()
            except Exception as exc:
                logger.exception("parallel_tasks sub-agent %d failed", i)
                sub_id, text = "(failed)", f"Error: {exc}"
            results[i] = (task_specs[i]["description"], sub_id, text)

    combined = "\n\n".join(
        f"--- Task {i + 1} ({desc}, sub-agent id: {sub_id}) ---\n{text}"
        for i, (desc, sub_id, text) in enumerate(results)
    )
    return combined, False


def execute_send_to_subagent(tool_input: dict, ctx: ToolContext) -> tuple[str, bool]:
    sub = ctx.living_subagents.get(tool_input["id"])
    if sub is None:
        return f"No living sub-agent found with id {tool_input['id']!r}.", True

    sub.run_turn(tool_input["message"])
    text = sub.last_assistant_text()
    if len(text) > TASK_RESULT_CHAR_CAP:
        text = text[:TASK_RESULT_CHAR_CAP] + "\n... (truncated)"
    return text, False


def execute_locus_run(tool_input: dict, ctx: ToolContext) -> tuple[str, bool]:
    """Run one turn as a persistent locus, with its standing brief prepended.

    The locus is not a special kind of agent -- it is an ordinary agent whose *prompt* is rebuilt
    from durable state (role, instructions, objectives, recent activity) instead of coming from
    a session transcript. That is the whole difference, and it is why a locus can resume work that
    no session remembers.

    Everything around the turn comes from :mod:`myriaxon.loci`: the preflight refusal, the run
    slot, the cross-process lease, the retry policy and the outcome record. None of that is
    re-implemented here, because a guarantee implemented twice is a guarantee that holds in one
    place.
    """
    registry = loci.get_registry()
    locus = registry.get(tool_input["locus"])
    if locus is None:
        return f"No locus named {tool_input['locus']!r}. Use locus_list to see them.", True

    # Preflight first: it is static, so it cannot hang or cost anything, and refusing before
    # spending tokens is the whole point of it. The reasons are returned verbatim so the caller
    # learns what to fix rather than that something went wrong.
    #
    # `for_message` reflects whether the owner actually said something. A message is a direct
    # instruction; the objective checks exist to stop a *proactive* wake-up inventing work, not to
    # stop a person asking their own agent for something.
    owner_said = bool(str(tool_input.get("message") or "").strip())
    problems = locus.preflight(for_message=owner_said)
    if problems:
        return f"{locus.name!r} is not ready to run:\n  - " + "\n  - ".join(problems), True

    # The briefing is the sub-agent's IDENTITY here, not a preamble in the message. Sending it as
    # text while the system prompt asserted the generic Myriaxon identity is why a locus answered
    # "I'm a coding agent" when asked whether it was the analyst: the model believed the system
    # prompt, correctly, because a request cannot override an instruction defining the same agent.
    persona = locus.briefing()
    prompt = f"Message from your owner:\n{tool_input['message']}"

    try:
        with locus.run_scope():
            locus.log("turn", f"asked: {tool_input['message']}")

            # Full autonomy, inside an enforced boundary. Both halves are required and they are set
            # together here so they cannot drift:
            #
            #   * `locus_workspace` tells the permission layer to ALLOW mutating tools instead of
            #     printing "Proceed?" to a console with no input. Measured: without it an unattended
            #     Locus retried a refused command and died on "invalid message format" after 235
            #     seconds, so no mutating tool was usable and a Locus could only ever restate what it
            #     had been told.
            #   * `tools.confine_to` makes every path-taking tool REFUSE anything outside that
            #     directory, which is what makes the autonomy safe to grant. Auto-approving without it
            #     would be no oversight and no boundary, which is the worst of both.
            #
            # Set for the duration of the run and restored afterwards by the token, so a session turn
            # in the same process is unaffected -- a stale boundary would make the interactive agent
            # refuse paths it has every right to touch.
            workspace = str(locus.workspace_path())
            previous_workspace = getattr(ctx, "locus_workspace", "")
            ctx.locus_workspace = workspace
            boundary_token = tools.confine_to(workspace)
            # Level 4.12: grant the BOARD, and nothing else.
            #
            # Measured before this: the board lives under the config dir, which is outside every member's
            # workspace, so `_resolve` refused it and a confined Locus could not use the board at all. The
            # alternative fixes are both worse -- widening the boundary to the config dir would expose
            # `sessions/`, `audit.jsonl`, `config.json` and every PEER workspace, while mounting the board
            # inside each workspace would give each member a private copy and share nothing.
            #
            # A PEER-WORKSPACE GRANT WAS HERE AND IT WAS A HOLE. The line read
            #
            #     tools.grant_shared("member-workspaces", _swarm_mod.members_dir())
            #
            # and `members_dir()` is the PARENT of every member's own workspace -- so granting it handed each
            # confined Locus read and write access to all of its peers' private directories. That is precisely
            # the per-member isolation requirement 4.12 exists to enforce, undone by a grant meant to help.
            #
            # It survived the 4.12 tests because those tests granted the board only, while the RUN LOOP
            # granted both -- a test that exercised a narrower configuration than production. Found by the
            # live end-to-end check, which applied the real grants and then asked whether a peer's file could
            # be read. It could.
            #
            # Nothing needs this grant. A Locus's own workspace is the confinement, and the swarm's shared
            # space is reached through `swarm_root()`, which stays granted.
            try:
                from . import locus_swarm as _swarm_mod

                tools.grant_shared("swarm", _swarm_mod.swarm_root())
            except Exception:  # noqa: BLE001 - a missing board must not stop a run
                pass
            try:
                def turn() -> tuple[str, str]:
                    # A Locus gets the same tool set as any other delegated run; what changes is that
                    # inside its own workspace it does not have to ask, because there is nobody to ask
                    # and the boundary is enforced rather than requested.
                    return _spawn_subagent(prompt, None, ctx, quiet=False, persona=persona)

                outcome = loci.run_with_retry(turn, locus=locus)
            finally:
                tools._LOCUS_WORKSPACE.reset(boundary_token)
                # Revoked with the boundary. A grant left standing would let a later, differently-confined run
                # in this process reach a directory granted for a run that has finished -- and grants are
                # process-global, so "later" includes an interactive turn in the same process.
                tools.grant_shared("swarm", None)
                ctx.locus_workspace = previous_workspace
    except loci.RunInProgress as exc:
        # Two processes running the same locus in one workspace is not a retryable condition: the
        # correct answer is to say who holds it, not to try again.
        return str(exc), True
    except Exception as exc:  # noqa: BLE001
        # Something outside the retry policy threw -- a programming error, or an interruption.
        # Recorded through the same outcome shape the retry path uses, so the failure is visible in
        # the locus's activity either way rather than only in the log.
        logger.exception("locus_run failed for %s", locus.name)
        loci.record_failure(
            locus,
            locus.next_goal(),
            loci.RunOutcome(
                ok=False,
                kind="error",
                attempts=0,
                error=f"{type(exc).__name__}: {exc}",
                error_kind="permanent",
                result=None,
                exception=exc,
            ),
        )
        registry.touch(locus)
        return f"The locus {locus.name!r} failed: {exc}", True

    if not outcome.ok:
        registry.touch(locus)
        detail = outcome.error or "no further detail"
        attempts = f" after {outcome.attempts} attempt(s)" if outcome.attempts else ""
        return f"The locus {locus.name!r} failed{attempts} ({outcome.error_kind}): {detail}", True

    sub_id, text = outcome.result
    locus.log("turn", f"replied: {text[:400]}")
    registry.touch(locus)
    return f"{locus.name} [{locus.id}] (run {sub_id}):\n\n{text}", False


_tool_schema_index: dict[str, dict] | None = None


def _get_tool_schema_index() -> dict[str, dict]:
    """Name -> input_schema for every statically-known tool, built from `_builtin_tool_defs()` --
    the built-ins only, deliberately, not `build_tool_defs()`, which now also carries plugin tools.
    Built from the wider list a plugin registering an existing name would replace that name's schema
    in the index and its arguments would be validated against the plugin's shape. Computed lazily on
    first use (not at module load) since it depends on the builder defined below in this same file.
    MCP tools (mcp__*) aren't in this index -- their schemas come from the MCP server itself and are
    validated there, not here."""
    global _tool_schema_index
    if _tool_schema_index is None:
        # Built-ins only: a plugin must not be able to replace the schema a built-in tool is
        # validated against. The dict comprehension still skips entries without a schema, so
        # mcp_toolset and the url-connector pseudo-tools remain excluded as before.
        _tool_schema_index = {
            t["name"]: t["input_schema"] for t in _builtin_tool_defs() if "input_schema" in t
        }
    return _tool_schema_index


_JSON_SCHEMA_TYPES: dict[str, type | tuple[type, ...]] = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "array": list,
    "object": dict,
}


def describe_tool_call(name: str, tool_input: dict, *, width: int = 100) -> str:
    """One-line, human-readable summary of a tool call.

    Replaces dumping `str(tool_input)` -- a raw dict repr -- with the argument that actually
    identifies the call (the path, the command, the pattern). A user watching a session needs to see
    *what* the agent is doing at a glance; the full arguments are still available via /trace and in
    the trajectory artifacts, so nothing is hidden by summarizing here.
    """
    if not isinstance(tool_input, dict):
        return _clip(str(tool_input), width)
    for key in ("command", "path", "file_path", "pattern", "query", "url", "name", "description", "prompt", "id"):
        if key in tool_input and isinstance(tool_input[key], str):
            return _clip(tool_input[key], width)
    if "tasks" in tool_input and isinstance(tool_input["tasks"], list):
        return f"{len(tool_input['tasks'])} parallel task(s)"
    return _clip(json.dumps(tool_input, default=str), width)


def _clip(text: str, width: int) -> str:
    flat = " ".join(str(text).split())
    return flat if len(flat) <= width else flat[: width - 1] + "\u2026"


def _emit_tool_line(ctx: ToolContext, name: str, tool_input: dict) -> None:
    """Print a compact call line.

    Every tool call used to print a Rich Panel with the full argument dump, which on a real session
    scrolled the actual answer off screen -- the opposite of what a status display is for. A single
    line per call, with the identifying argument, keeps a multi-step turn readable.
    """
    ctx.console.print(f"[tool.name]{escape(name)}[/] [dim]{escape(describe_tool_call(name, tool_input))}[/]")


def _maybe_coach(error: str, tool_name: str, ctx: ToolContext) -> str:
    """Give a weak model the exact expected call shape instead of a bare rejection.

    Only applied when the active profile says the model needs it: on a strong model the extra ~200
    characters per malformed call are pure context noise, and strong models rarely produce them.
    """
    profile = getattr(ctx, "profile", None)
    if profile is None or not getattr(profile, "needs_argument_coaching", False):
        return error
    from .model_adapt import coach_argument_error

    return coach_argument_error(error, tool_name)


_NEVER_EMPTY_WHEN_REQUIRED = frozenset({"path", "pattern", "url", "query", "id", "locus", "name", "prompt", "description"})


def _validate_tool_input(name: str, tool_input: dict, schema: dict) -> str | None:
    """Cheap, dependency-free structural check against a tool's own input_schema. Catches the
    common weak-model failure modes -- a missing required field, or a string where a number was
    expected -- with one specific, actionable sentence, instead of letting a raw Python exception
    (e.g. KeyError: 'path') surface from deep inside the tool's own implementation, which is far
    less useful feedback for a weak model to actually act on. Deliberately lenient: unrecognized
    schema features and unexpected extra arguments are ignored rather than blocked, since a false
    "your call is invalid" is worse than letting a merely-unusual-but-workable call through."""
    if not isinstance(tool_input, dict):
        return f"{name}: arguments must be a JSON object, got {type(tool_input).__name__}."

    for required_key in schema.get("required", []):
        if required_key not in tool_input:
            return f"{name}: missing required argument '{required_key}'."

    properties = schema.get("properties", {})
    required = set(schema.get("required", []))
    for key, value in tool_input.items():
        prop_schema = properties.get(key)
        if not prop_schema:
            continue
        # A REQUIRED path, pattern, id or name that is empty is never what was meant. An empty `path` used to resolve to the
        # workspace folder itself and come back as "Permission denied: <the folder>"; an empty `pattern` raised ValueError.
        if key in required and key in _NEVER_EMPTY_WHEN_REQUIRED and isinstance(value, str) and not value.strip():
            return f"{name}: argument '{key}' is required and was empty."
        expected = prop_schema.get("type")
        py_type = _JSON_SCHEMA_TYPES.get(expected)
        if py_type is None:
            continue
        if expected in ("integer", "number") and isinstance(value, bool):
            return f"{name}: argument '{key}' must be {expected}, got boolean."
        if not isinstance(value, py_type):
            return f"{name}: argument '{key}' must be of type {expected}, got {type(value).__name__}."
        enum = prop_schema.get("enum")
        if enum and value not in enum:
            return f"{name}: argument '{key}' must be one of {enum}, got {value!r}."

    return None


def _check_tool_loop(name: str, tool_input: dict, ctx: ToolContext) -> str | None:
    """Refuses a tool call that exactly repeats (same name, same arguments) the previous one
    LOOP_DETECTION_THRESHOLD times in a row -- a cheap, provider-agnostic signal the model is
    stuck rather than making progress. Any variation (even a slightly different argument) resets
    the streak, since that's still forward motion even if misguided."""
    try:
        signature = (name, json.dumps(tool_input, sort_keys=True, default=str))
    except TypeError:
        return None  # unserializable input isn't worth blocking over

    if ctx.last_tool_signature == signature:
        ctx.tool_repeat_count += 1
    else:
        ctx.last_tool_signature = signature
        ctx.tool_repeat_count = 1

    if ctx.tool_repeat_count >= LOOP_DETECTION_THRESHOLD:
        ctx.tool_repeat_count = 0  # one fresh attempt before this can trip again
        return (
            f"Loop detected: {name} was just called with identical arguments "
            f"{LOOP_DETECTION_THRESHOLD} times in a row with no apparent progress. Stop repeating "
            "this exact call -- try a materially different approach, explain the blocker in your "
            "reply, or use ask_user_question to get guidance."
        )
    return None


_PATH_TAKING_TOOLS = {"read_file", "write_file", "edit_file", "glob_search", "grep_search", "git_diff"}


def dispatch_tool(name: str, tool_input: dict, ctx: ToolContext) -> tuple[str, bool]:
    """Records one trajectory event per tool call when a benchmark recorder is attached.

    The recorder is optional and inert in normal CLI use (`ctx.bench_trace` is None) -- it exists
    so IQB-14 can score tool-use correctness, guardrail behavior, loop/repeat rates and step
    budgets from what the agent *actually did*, rather than reconstructing behavior from prose or
    from a regex over captured stdout. Timing wraps the whole call, including confirmation
    prompts, so a hang is visible as a duration rather than an absence of evidence.
    """
    recorder = getattr(ctx, "bench_trace", None)
    call_index = recorder.begin() if recorder is not None else None
    # Recorded for the duration of the call so the spill layer can name the file it writes after the tool.
    # Restored afterwards rather than left behind: a stale name would mislabel whatever spilled next.
    previous_tool_name = getattr(ctx, "current_tool_name", "")
    try:
        ctx.current_tool_name = name
    except Exception:
        pass
    try:
        budget = deadline_for_tool(name)
        if budget:
            # A declared budget means the call is run under a scoped deadline, so a wedged web/MCP/browser call
            # cannot park the turn forever. `outer_aborted` is the misattribution guard: when the run itself was
            # cancelled, the verdict is withheld rather than blamed on this tool's budget.
            from . import deadline as _deadline

            capability = getattr(ctx, "run_capability", None)

            def _outer_aborted() -> bool:
                if capability is None:
                    return False
                return not getattr(capability, "active", True)

            outcome = _deadline.run_with_deadline(
                lambda: _dispatch_tool_inner(name, tool_input, ctx),
                seconds=budget,
                tool=name,
                outer_aborted=_outer_aborted,
            )
            if outcome.error is not None:
                raise outcome.error
            if outcome.timed_out:
                content, is_error = str(outcome.result), True
            else:
                content, is_error = outcome.result, False
        else:
            content, is_error = _dispatch_tool_inner(name, tool_input, ctx)
    except BaseException:
        if recorder is not None and call_index is not None:
            recorder.abort(call_index)
        try:
            ctx.current_tool_name = previous_tool_name
        except Exception:
            pass
        # A call that raised still counts. A tool failing the same way every time is the clearest loop there
        # is, and recording only successes would make exactly that case invisible.
        _record_for_loop_detection(name, tool_input, f"{name} raised", failed=True, ctx=ctx)
        raise
    if recorder is not None:
        recorder.record(name, tool_input, is_error, content)
        # Budget enforcement at the tool-call boundary. Deliberately outside the try above: a
        # BudgetExceeded raised in here must propagate to the agent's turn-level rollback, not be
        # swallowed into a tool error string by _dispatch_tool_inner's handler.
        guard = getattr(ctx, "bench_guard", None)
        if guard is not None:
            guard.check_after_tool_call()

    _record_for_loop_detection(name, tool_input, content, failed=is_error, ctx=ctx)
    try:
        ctx.current_tool_name = previous_tool_name
    except Exception:
        pass
    return content, is_error


def _record_for_loop_detection(
    name: str, tool_input: dict, result, *, failed: bool, ctx: ToolContext
) -> None:
    """Feed one completed call to the loop detector, and surface a verdict.

    Wired here rather than inside each tool because this is the single point every tool result passes through --
    the same reasoning as the trajectory recorder just above. A detector nothing calls is a library, not a guard,
    and before this the `loop_detection` module was exactly that.

    The detector is read from the *context* first, falling back to the module-level one, so two agents in one
    process -- a session turn and a Locus waking -- cannot pollute each other's counts.

    Never raises. A detector failure must not become a tool failure: losing a detection costs a warning, while
    raising here would cost the tool result.
    """
    try:
        from . import loop_detection

        detector = getattr(ctx, "loop_detector", None)
        if detector is not None:
            detection = detector.record(name, tool_input, result, failed=failed)
        else:
            detection = loop_detection.record(name, tool_input, result, failed=failed)
        if detection is None:
            return
    except Exception:
        logger.exception("Loop detection failed for %s", name)
        return

    # Surfaced rather than swallowed: a warning nobody sees is the same as no warning. The caller's own policy
    # decides what to do about it, which is how every other soft refusal in this harness behaves.
    try:
        ctx.console.print(f"[warning]{escape(detection.render())}[/]")
    except Exception:
        pass


def _owner_agent(ctx: ToolContext):
    """The agent whose tool list this context belongs to, if it can be found.

    `ToolContext.owner` is set by the agent that builds it. This is a *reference* rather than a
    search: the first version of this walked `living_subagents`, but the main agent is never placed
    there -- only spawned sub-agents are -- so the lookup would have failed for every ordinary
    session, which is the only kind that matters here. `getattr` rather than an attribute access so a
    context built by an older caller, or by a test, degrades to a refusal instead of an AttributeError
    in the middle of a tool call.
    """
    owner = getattr(ctx, "owner", None)
    if owner is not None and hasattr(owner, "unlocked_tool_groups"):
        return owner
    # A sub-agent's own context is its own; but a locus running as a sub-agent inherits the parent's ctx
    # in some paths, and its owner is then the parent. Falling back is better than refusing.
    for agent in list((getattr(ctx, "living_subagents", None) or {}).values()):
        if getattr(agent, "tool_context", None) is ctx and hasattr(agent, "unlocked_tool_groups"):
            return agent
    return None


def _request_tools(tool_input: dict, ctx: ToolContext) -> tuple[str, bool]:
    """Unlock a deferred tool group for the rest of the conversation.

    The list is recomputed from the agent's OWN full set rather than rebuilt from a builder. Rebuilding
    was wrong in three ways the audit found, and all three are fixed by using what the agent already
    holds:

      * a builder knows nothing about the profile, so a group unlocked here could reintroduce a tool
        the profile deliberately withholds (`delegation` under `agent_mode="single"`);
      * a builder knows nothing about this session's plugin or MCP tools, so they vanished from
        `agent.tools` after the first unlock;
      * `_builtin_tool_defs()` and `build_tool_defs()` are separate lists that had already drifted.

    Order comes from the agent's full set, so a second unlock never reshuffles what was visible --
    a reshuffled prefix invalidates the provider's cache, which costs far more than the schemas save.
    """
    requested = str(tool_input.get("group") or "").strip().lower()
    valid = sorted(DEFERRED_GROUPS)

    agent = _owner_agent(ctx)
    if agent is None:
        return (
            "request_tools could not reach the running agent, so no tools were loaded. "
            f"Report this rather than retrying. Valid groups: {', '.join(valid)}.",
            True,
        )

    # `all_tools` is the pre-visibility list the agent kept for exactly this purpose. Falling back to
    # the profile-filtered `tools` plus the withheld ones keeps an older agent object working.
    full = getattr(agent, "all_tools", None)
    if not full:
        return (
            "request_tools found no tool list to extend. "
            f"Report this rather than retrying. Valid groups: {', '.join(valid)}.",
            True,
        )

    if not requested:
        return f"Which group? Choose one of: {', '.join(valid)}.", True
    if requested not in DEFERRED_GROUPS:
        # Refused with the list, so a near miss is one cheap correction instead of a dead end.
        close = [g for g in valid if g.startswith(requested[:3])]
        hint = f" Did you mean {close[0]!r}?" if close else ""
        return f"There is no group {requested!r}.{hint} Valid groups: {', '.join(valid)}.", True

    unlocked = getattr(agent, "unlocked_tool_groups", None)
    if unlocked is None:
        return "request_tools could not record the unlock. Report this rather than retrying.", True
    already = requested in unlocked
    unlocked.add(requested)

    agent.tools = apply_tool_visibility(full, unlocked)
    # The shared context still holds the OLD list, and `_spawn_subagent_inner` builds sub-agents from
    # `ctx.tools` -- so without this a group the parent unlocked was never available to a sub-agent it
    # spawned, which is a silent capability hole. Kept in step here rather than read from the agent at
    # spawn time because a sub-agent must see the same tools its parent had, not the parent's *current*
    # list after a later unlock.
    if getattr(ctx, "tools", None) is not None:
        ctx.tools = list(agent.tools)

    names = list(DEFERRED_GROUPS[requested]["tools"])  # type: ignore[arg-type]
    listed = ", ".join(names)
    # The cache caveat is stated to the model because it is the model's decision to make: on a
    # provider that caches the prefix (tools included), loading a group mid-conversation re-bills the
    # conversation at full price. Per-group schemas are ~130-700 tokens; the re-bill is ~0.9x the
    # conversation. So loading early is far cheaper than loading late, and loading twice is worse
    # still. This is stated once, here, rather than as prose in every request.
    if already:
        return f"{requested} was already loaded. Its tools are: {listed}. Call one now.", False
    return (
        f"Loaded the {requested} group: {listed}. "
        "Available from your next step -- call the one you need now. If you expect to need another "
        "group later in this conversation, load it in the same step: each load is cheap on its own "
        "but re-sends the conversation context, so batching them is far cheaper than returning here.",
        False,
    )


def _unlock_by_group(agent, ctx, group: str) -> tuple[bool, str]:
    """Unlock one deferred group on a live agent. Returns `(newly_unlocked, message)`.

    Extracted from `_request_tools` so the name-based path in `_tool_search` reuses the same unlock rather than a
    second implementation. The reasoning recorded on `_request_tools` about rebuilding from the agent's own
    `all_tools` -- rather than from a builder, which would reintroduce profile-withheld tools and drop this
    session's plugin and MCP tools -- applies identically here, and a second copy would drift.
    """
    unlocked = getattr(agent, "unlocked_tool_groups", None)
    if unlocked is None:
        return False, "could not record the unlock"
    already = group in unlocked
    unlocked.add(group)
    agent.tools = apply_tool_visibility(getattr(agent, "all_tools", None), unlocked)
    # Kept in step with the shared context, because `_spawn_subagent_inner` builds sub-agents from `ctx.tools`:
    # without this a group the parent unlocked was never available to a sub-agent it spawned, which is a silent
    # capability hole.
    if getattr(ctx, "tools", None) is not None:
        ctx.tools = list(agent.tools)
    return not already, ""


def _tool_search(tool_input: dict, ctx: ToolContext) -> tuple[str, bool]:
    """Find a deferred tool by describing what you want, instead of naming its group.

    ## Why this exists

    `request_tools` unlocks by **group name**, so the model has to already know that `"memory"` is a group before
    it can ask for it. A model that has never seen this harness cannot discover that a memory subsystem exists,
    which makes the deferred tools reachable in practice only by a caller who read the source. That is hiding,
    not deferral.

    This searches the deferred set by description and unlocks the group of anything it returns, so a single call
    both finds the tool and makes it usable. It answers with the tool names and their descriptions, and says
    explicitly that they are not callable until the next step -- because a model that tries to call one
    immediately gets an unknown-tool error and concludes the search lied.
    """
    from . import tool_catalog

    query = str(tool_input.get("query") or "").strip()
    limit = tool_input.get("limit") or 6
    try:
        limit = max(1, min(int(limit), 12))
    except (TypeError, ValueError):
        limit = 6

    if not query:
        groups = ", ".join(sorted(DEFERRED_GROUPS))
        return (
            "What are you looking for? Describe it in a few words -- for example 'remember what happened in an "
            f"earlier session' or 'run something on a timer'. Deferred groups: {groups}.",
            True,
        )

    entries = catalog_for_deferred_tools()
    result = tool_catalog.search(entries, query, limit=limit)
    if not result.hits:
        # Named the failure honestly and offered the other route. A search that returns nothing while a group
        # list exists should say so rather than leaving the model to conclude the capability is absent.
        groups = ", ".join(sorted(DEFERRED_GROUPS))
        return (
            f"No deferred tool matches {query!r}. The groups are: {groups}. "
            "You can also call request_tools with a group name directly.",
            False,
        )

    agent = _owner_agent(ctx)
    # Only the STRONGEST matches unlock their group, and only a few of them.
    #
    # Measured: an unbounded version loaded five groups from one query, restoring eleven tool definitions -- the
    # entire token saving this mechanism exists for, thrown away by one call. The deferred set costs roughly 2,600
    # tokens, so loading most of it to answer one question is worse than never having deferred anything.
    #
    # Two limits, because either alone leaks. The score cut is *relative to the best match*, so a query with one
    # strong hit loads one group and a query where several tools tie loads all of those. The count cap bounds the
    # worst case, which the relative cut cannot: when a term like "session" appears in many descriptions, the
    # scores cluster near the best and every one of them clears a relative threshold.
    #
    # 0.6 rather than 0.75 because the ranking is lexical and a paraphrase often puts the right tool second:
    # "remember what happened in an earlier session" matches `locus_create` on "session" more strongly than it
    # matches `search_memory` on "remember", even though the second is the intended answer.
    best = result.hits[0].score
    strong = [hit for hit in result.hits if hit.score >= best * 0.6][:MAX_GROUPS_LOADED]

    unlocked_groups: list[str] = []
    for hit in strong:
        group = group_for_tool(hit.entry.name)
        if not group or agent is None:
            continue
        _, _problem = _unlock_by_group(agent, ctx, group)
        if group not in unlocked_groups:
            unlocked_groups.append(group)

    lines = [f"Found {len(result.hits)} deferred tool(s) matching {query!r}:"]
    for hit in result.hits:
        description = hit.entry.description.split(".")[0][:110]
        marker = "" if hit in strong else "  [not loaded]"
        lines.append(f"  - {hit.entry.name}: {description}{marker}")

    if unlocked_groups:
        lines.append(
            f"\nLoaded the group(s) {', '.join(unlocked_groups)}. These become callable on your NEXT step -- "
            "calling one now returns unknown-tool. Load any other group you expect to need in this same step, "
            "because each load re-sends the conversation context."
        )
    else:
        lines.append("\nThis tool could not reach the running agent, so nothing was loaded.")

    others = sorted({group_for_tool(hit.entry.name) for hit in result.hits if hit not in strong}
                    - set(unlocked_groups) - {""})
    if others:
        lines.append(
            f"Not loaded (weaker matches): {', '.join(others)}. "
            "Call tool_search again with a narrower query, or request_tools with the group name, if you need one."
        )
    if result.truncated:
        lines.append("(More matched than are shown; narrow the query to see others.)")
    return "\n".join(lines), False


def _recall(tool_input: dict, ctx: ToolContext) -> tuple[str, bool]:
    """Read back a tool result that compaction removed, via the handle its placeholder names.

    Handled in `_dispatch_tool_inner` alongside `request_tools` rather than through the normal path, because it
    must never be blocked by a hook written for a visible tool and it has no workspace side effects to confirm.
    It reads the compaction audit store, which is the harness's own directory rather than the project, so it is
    not subject to the write boundary.
    """
    from . import compaction_audit

    handle = str(tool_input.get("handle") or "").strip()
    query = str(tool_input.get("query") or "").strip()
    session = str(getattr(ctx, "session_id", "") or "")

    if query:
        hits = compaction_audit.search(query, session=session, limit=tool_input.get("limit") or 20)
        if not hits:
            return (
                f"Nothing in the removed results matches {query!r}. The store holds only results that were "
                "pruned on this machine, so older sessions may predate it.",
                False,
            )
        lines = [f"{len(hits)} removed result(s) matching {query!r}:"]
        for hit in hits:
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(hit["at"]))
            lines.append(
                f"  - {hit['handle'][:12]}...  {hit['chars']:,} chars, {when}, "
                f"matched in the {hit['matched_in']}"
                + (f", tool {hit['tool']}" if hit["tool"] else "")
            )
        lines.append("\nCall recall with one of these handles to read it.")
        return "\n".join(lines), False

    if not handle:
        return (
            "Give either a handle (from a pruned result's placeholder) or a query to search the removed "
            "results.",
            True,
        )

    if tool_input.get("trace"):
        events = compaction_audit.trace(handle, session=session)
        if not events:
            return f"No recorded events refer to handle {handle[:12]}....", False
        lines = [f"{len(events)} event(s) referring to {handle[:12]}...:"]
        for event in events:
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(float(event.get("at") or 0)))
            lines.append(
                f"  - {event.get('relation')}: {event.get('kind')} -- {event.get('reason')} "
                f"({event.get('chars', 0):,} chars, {when})"
            )
        return "\n".join(lines), False

    content, problem = compaction_audit.recall(handle, session=session)
    if content is None:
        return problem, True
    return compaction_audit.render_recall(content, handle), False


def _check_plan_mode(name: str, ctx: ToolContext) -> str | None:
    """The refusal message when plan mode forbids this tool, else None.

    Kept as a thin wrapper so the dispatch site stays readable and the rule itself stays testable without a context
    -- `plan_mode.check()` takes a mode string and is what the verifier exercises directly.
    """
    from . import plan_mode

    verdict = plan_mode.check_for_context(name, ctx)
    if verdict.allowed:
        return None
    return verdict.render()


def _check_guardrails(name: str, tool_input: dict, ctx: ToolContext) -> tuple[str, bool] | None:
    """Evaluate the operator's guardrail rules before the call runs.

    Returns a refusal, or None to proceed. Rules are read from disk once per process and cached: a rule file is
    author-time configuration, and re-reading it on every tool call would put a directory listing on the hot path
    of a session.

    **Fail-closed.** A rule that cannot be parsed or evaluated refuses the call, because the operator wrote it
    expecting it to be in force and a guardrail that silently stopped guarding is worse than none. A rule whose
    event does not match is simply not applicable, which is not a failure.

    This deliberately differs from Claude Code's `hookify`, which ends every hook in `sys.exit(0)` so a broken
    "block" rule degrades to a warning.
    """
    from . import guardrails

    rules = _guardrail_rules()
    if not rules:
        return None
    try:
        decision = guardrails.evaluate(
            rules,
            {
                "tool": name,
                "arguments": tool_input if isinstance(tool_input, dict) else {},
                "session": str(getattr(ctx, "session_id", "") or ""),
            },
            event=guardrails.EVENT_PRE_TOOL,
            honour_project_rules=not bool(getattr(ctx, "guardrails_ignore_project", False)),
        )
    except Exception as exc:  # noqa: BLE001
        # The evaluator is fail-closed internally; reaching here means the evaluator itself broke, and the same
        # reasoning applies one level up.
        return (
            f"Blocked by the guardrail layer, which could not be evaluated ({type(exc).__name__}: {exc}). "
            "The call was refused rather than allowed through unchecked.",
            True,
        )

    for warning in decision.warnings:
        try:
            ctx.console.print(f"[warning]guardrail: {escape(warning)}[/]")
        except Exception:
            pass

    if decision.blocked:
        detail = "\n".join(f"  - {reason}" for reason in decision.reasons)
        return (
            f"Blocked by a guardrail rule:\n{detail}\n"
            "A guardrail is operator policy, not a suggestion. Report the conflict rather than working around it.",
            True,
        )
    return None


_GUARDRAIL_CACHE: list | None = None
_GUARDRAIL_PROBLEMS: list[str] = []


def _guardrail_rules() -> list:
    """The loaded guardrail rules, read once and cached. Problems are reported once, on first load."""
    global _GUARDRAIL_CACHE
    if _GUARDRAIL_CACHE is None:
        from . import guardrails

        try:
            rules, problems = guardrails.load_rules()
        except Exception:
            logger.exception("could not load guardrail rules")
            rules, problems = [], []
        _GUARDRAIL_CACHE = rules
        _GUARDRAIL_PROBLEMS[:] = problems
        for problem in problems:
            # Surfaced, not swallowed: a caller running with fewer guardrails than its author believes needs to
            # know, and a malformed rule file is exactly how that happens.
            logger.warning("guardrail rule not loaded: %s", problem)
    return _GUARDRAIL_CACHE


def reset_guardrail_cache() -> None:
    """Forget the cached rules, so the next call re-reads them. For a `/reload`-style command and for tests."""
    global _GUARDRAIL_CACHE
    _GUARDRAIL_CACHE = None
    _GUARDRAIL_PROBLEMS.clear()


def guardrail_problems() -> list[str]:
    """Rule files that could not be loaded. Empty means every rule on disk is in force."""
    _guardrail_rules()
    return list(_GUARDRAIL_PROBLEMS)


def _session_status(tool_input: dict, ctx: ToolContext) -> tuple[str, bool]:
    """Report the agent's own current state.

    Handled alongside the other introspection tools, before validation and hooks: it has no side effects, and a
    guardrail rule scoped to a *tool* should not be able to prevent the agent from finding out what state it is in.

    Never fails. A section that cannot be read says so and the rest still renders -- a self-report that failed as a
    whole would be useless exactly when the model most needed it.
    """
    from . import self_status

    section = str(tool_input.get("section") or "all").strip().lower()
    wanted = None if section in ("", "all") else [section]
    return self_status.report(ctx, sections=wanted), False


def _heartbeat_respond(tool_input: dict, ctx: ToolContext) -> tuple[str, bool]:
    """Record the outcome of a periodic check.

    The structured form of the silent path: `notify=false` with a note stays invisible now and is carried as
    bounded context to the next turn, which is what lets an unattended agent accumulate context across quiet
    checks without a single visible message. `notify=true` is an alert.

    Handled before validation and hooks, like the other introspection tools: it has no workspace side effects and
    must not be blockable by a rule written for a visible tool.
    """
    from . import heartbeat

    notify = bool(tool_input.get("notify"))
    text = str(tool_input.get("text") or "")
    if notify and not text.strip():
        return (
            "notify=true needs `text`: an alert with nothing in it is not worth delivering. Use notify=false to "
            "stay silent, or NO_REPLY if you simply have nothing to say.",
            True,
        )

    owner = _owner_agent(ctx)
    if owner is not None and hasattr(owner, "record_heartbeat"):
        outcome = heartbeat.classify(text, structured=heartbeat.StructuredReply(notify, text))
        owner.record_heartbeat(
            outcome.verdict, text=outcome.text, note=outcome.remembered
        )
        return heartbeat.respond(notify, text), False

    return heartbeat.respond(notify, text), False


def _dispatch_tool_inner(name: str, tool_input: dict, ctx: ToolContext) -> tuple[str, bool]:

    # Handled before validation and before hooks. `request_tools` changes which tools exist, so it
    # must not be blockable by a hook written for a tool that is already visible, and it has no side
    # effects on the workspace to confirm. `tool_search` is here for the same reason: it is the other
    # half of the same mechanism. `recall` reads the harness's own audit store, not the project, so it is
    # outside the write boundary's remit and must not be gated by a project-scoped policy.
    if name == "request_tools":
        return _request_tools(tool_input, ctx)
    if name == "tool_search":
        return _tool_search(tool_input, ctx)
    if name == "recall":
        return _recall(tool_input, ctx)
    if name == "session_status":
        return _session_status(tool_input, ctx)

    # Plan mode runs FIRST, before guardrails and before validation.
    #
    # Before guardrails, because plan mode is not a policy someone configured -- it is the operating mode of the
    # session, and a user rule permitting `write_file` must not be able to re-enable writes in a mode whose entire
    # definition is that nothing changes. Before validation, because a refusal should not depend on whether the
    # rest of the arguments happen to parse.
    #
    # This is the check that turns `--mode plan` from a prompt hint into an enforced mode. Before it, `MODE_PLAN`
    # existed in the constants, was offered on the command line, was displayed -- and was consulted nowhere.
    plan_verdict = _check_plan_mode(name, ctx)
    if plan_verdict is not None:
        return plan_verdict, True

    # Guardrails run before validation, so a rule can refuse a malformed call too -- "no writing to that path"
    # should hold whether or not the rest of the arguments happen to be valid.
    refusal = _check_guardrails(name, tool_input, ctx)
    if refusal is not None:
        return refusal

    loop_reason = _check_tool_loop(name, tool_input, ctx)
    if loop_reason is not None:
        return loop_reason, True

    if name in _PATH_TAKING_TOOLS and isinstance(tool_input.get("path"), str):
        # Feeds /ground's grounding check (see grounding.py) -- recorded regardless of whether
        # the call ultimately succeeds, since "the model looked at this path" is what matters,
        # not whether the read happened to succeed.
        ctx.touched_paths_this_turn.add(tool_input["path"])

    # Recorded before validation and before the call itself, because the question this answers is
    # "did the agent attempt this capability", not "did it succeed". An attempt that failed still
    # proves the agent tried; a claim with no attempt is what needs catching.
    try:
        ctx.tools_called_this_turn.add(name)
    except AttributeError:
        pass          # an older or stub context; recording is best-effort

    schema = _get_tool_schema_index().get(name)
    if schema is not None:
        validation_error = _validate_tool_input(name, tool_input, schema)
        if validation_error is not None:
            return _maybe_coach(validation_error, name, ctx), True

    block_reason = hooks.run_pre_tool_use(name, tool_input, ctx)
    if block_reason is not None:
        return block_reason, False

    # In-process plugin hooks run alongside the shell hooks. A plugin's pre_tool_use returning a
    # string blocks the call, matching the shell hook contract.
    from .plugins import get_registry

    plugin_registry = get_registry()
    if plugin_registry.loaded:
        plugin_block = plugin_registry.first_blocking_result("pre_tool_use", name, tool_input, ctx)
        if plugin_block:
            return plugin_block, False
        plugin_tool = plugin_registry.get_tool(name)
        if plugin_tool is not None:
            # A plugin tool declared `mutating` goes through the same confirmation and policy gate as
            # a built-in mutating tool, so a settings.json `deny` rule applies to it unchanged. A
            # non-mutating plugin tool runs directly, like a read.
            if plugin_tool.mutating:
                panel = Panel(
                    Text(str(tool_input)), title=f"[tool.confirm.question]{name}[/]",
                    border_style="tool.confirm.question",
                )
                proceed, blocked = tools.confirm_action(ctx.console, panel, ctx, name, tool_input=tool_input)
                if blocked:
                    return blocked, False
                if not proceed:
                    return "User declined to run this tool call.", False
            return plugin_registry.call(name, tool_input, ctx)

    if ctx.trace_enabled:
        try:
            args_preview = json.dumps(tool_input, default=str)
        except TypeError:
            args_preview = str(tool_input)
        ctx.console.print(f"[dim]→ {escape(name)}({escape(args_preview)})[/]")

    try:
        content, is_error = _dispatch_tool(name, tool_input, ctx)
    except Exception as exc:
        # A model can call a tool with missing/malformed arguments (e.g. a required field
        # omitted). That's untrusted input from the model's own generation, not a scenario we
        # control -- report it back as a tool error instead of crashing the whole turn.
        logger.exception("Tool dispatch failed for %s", name)
        content, is_error = _describe_tool_failure(name, exc, ctx), True

    if ctx.trace_enabled:
        preview = content if len(content) <= 500 else content[:500] + "... (truncated)"
        style = "error" if is_error else "dim"
        marker = "ERROR" if is_error else "OK"
        ctx.console.print(f"[{style}]← {escape(name)} {marker}: {escape(preview)}[/]")

    hooks.run_post_tool_use(name, tool_input, content, is_error, ctx)
    # `ctx` is passed so the entry can name WHO acted. Without it the audit trail recorded what
    # happened and not who did it, which `mission_control` reported as "there is no run to attribute
    # these rows to".
    audit.log_tool_call(name, tool_input, is_error, ctx)
    return content, is_error


# Exceptions that, raised from inside a tool, almost always mean an argument was not the shape the tool expected:
# `KeyError: 'status'` (an item without a status), `'str' object has no attribute 'get'` (a string where an object belongs).
_SHAPE_ERRORS = (KeyError, TypeError, AttributeError, IndexError, ValueError)


def _describe_tool_failure(name: str, exc: BaseException, ctx: ToolContext) -> str:
    """The text the model sees when a tool raises. Always starts with `Tool error:` (the benchmark counts those).

    A bare `Tool error: 'status'` tells a model nothing it can act on. For the errors that mean "an argument had the wrong
    shape" the message says so, and a model whose profile asks for coaching also gets the tool's expected-input description.
    """
    detail = f"{type(exc).__name__}: {exc}"
    if isinstance(exc, _SHAPE_ERRORS):
        return _maybe_coach(
            f"Tool error: {name} could not use these arguments ({detail}). "
            "Check each argument against the tool's input schema: the type of every item in a list matters.",
            name, ctx,
        )
    return f"Tool error: {name} failed: {detail}"


def _dispatch_tool(name: str, tool_input: dict, ctx: ToolContext) -> tuple[str, bool]:
    if name in BUILTIN_TOOL_NAMES:
        return tools.execute_tool(name, tool_input, ctx)

    ctx.console.print(f"[tool.name]{name}[/] [dim]{escape(describe_tool_call(name, tool_input))}[/]")

    if name == "todo_write":
        return todo.execute_todo_write(tool_input, ctx)
    if name == "task":
        return execute_task(tool_input, ctx)
    if name == "edit_notebook":
        return notebook.execute_edit_notebook(tool_input, ctx)
    if name == "use_skill":
        return skills.execute_use_skill(tool_input, ctx.skills)
    if name == "save_skill":
        return skills.execute_save_skill(tool_input, ctx)
    if name == "checkpoint_list":
        return checkpoints.execute_checkpoint_list(tool_input, ctx)
    if name == "checkpoint_restore":
        return checkpoints.execute_checkpoint_restore(tool_input, ctx)
    if name == "ask_user_question":
        return ask_user.execute_ask_user_question(tool_input, ctx)
    if name == "send_to_subagent":
        return execute_send_to_subagent(tool_input, ctx)
    if name == "parallel_tasks":
        return execute_parallel_tasks(tool_input, ctx)
    if name == "search_memory":
        return memory_store.execute_search_memory(tool_input)
    if name == "locus_list":
        return loci.execute_locus_list(tool_input)
    if name == "locus_create":
        return loci.execute_locus_create(tool_input)
    if name == "locus_watchlist":
        return loci.execute_locus_watchlist(tool_input)
    if name == "branch_session":
        return session_lineage.execute_branch_session(tool_input, ctx)
    if name == "heartbeat_respond":
        return _heartbeat_respond(tool_input, ctx)
    if name == "locus_goal_update":
        # `ctx` is passed so the handler can see this turn's provenance. Without it the completion gate resolves
        # to the least authoritative source, which is the intended default -- but then a Locus's own run could not
        # report *why* it was refused, only that something failed.
        return loci.execute_locus_goal_update(tool_input, ctx)
    if name == "locus_run":
        return execute_locus_run(tool_input, ctx)
    if name in ("swarm_post", "swarm_list", "swarm_claim", "swarm_finish", "swarm_release"):
        # The swarm board. Each handler is a thin wrapper over `locus_swarm`, which owns the lease
        # semantics; passing `member` explicitly rather than inferring it means the board records a name a
        # reader can act on rather than an internal id.
        from . import locus_swarm

        handlers = {
            "swarm_post": locus_swarm.execute_swarm_post,
            "swarm_list": locus_swarm.execute_swarm_list,
            "swarm_claim": locus_swarm.execute_swarm_claim,
            "swarm_finish": locus_swarm.execute_swarm_finish,
            "swarm_release": locus_swarm.execute_swarm_release,
        }
        return handlers[name](tool_input)
    if name in ("memory_share", "memory_recall"):
        # Collective memory -- what PEERS have learned, as distinct from `search_memory`, which reads your
        # own past session transcripts. Kept separate because the two answer different questions and
        # merging them would make "did I work on this" and "does anyone know this" the same query.
        from . import locus_memory

        if name == "memory_share":
            return locus_memory.execute_memory_share(tool_input)
        return locus_memory.execute_memory_recall(tool_input)
    if name in ("check_context", "spill"):
        # Level 5.8. `check_context` is handed the AGENT rather than the context, because the budget it
        # reports has to be the one that agent will actually be held to -- reading it from anywhere else
        # risks the agent deciding on a number nobody enforces.
        from . import context_budget

        if name == "check_context":
            return context_budget.execute_check_context(getattr(ctx, "owner", None))
        workspace = getattr(ctx, "locus_workspace", "") or None
        return context_budget.execute_spill(tool_input, workspace=workspace)
    if name == "declare_uncertainty":
        # Level 5.5. The run id is threaded through so a report can show one run's claims rather than every
        # claim ever made in this config directory.
        from . import uncertainty

        claim = getattr(ctx, "run_state", None)
        run_id = str(getattr(claim, "run_id", "") or "")
        return uncertainty.execute_declare_uncertainty(tool_input, run_id=run_id)
    if name == "design_poster":
        return poster.poster_tool(tool_input, ctx)
    if name in web_tools.LOCAL_WEB_TOOL_NAMES:
        # Reached only when the provider did not run these itself. On Anthropic they
        # arrive as server_tool_use blocks and never get here; on every other provider
        # this is the implementation, so the model can finally fetch what it cites.
        return web_tools.execute(name, tool_input)
    if name.startswith("mcp__"):
        if ctx.mcp_bridge is None:
            return f"No MCP bridge configured for {name}", True
        # MCP tool calls used to bypass confirm_action entirely -- unlike every other mutating
        # tool (write_file/edit_file/run_bash/edit_notebook/checkpoint_restore), they ran
        # unconditionally regardless of mode/yolo, and settings.json deny rules couldn't touch
        # them at all since the policy engine was never even consulted. An MCP server's tools
        # are arbitrary and not under this codebase's control, so they're gated the same as any
        # other mutating action now, not treated as unconditionally trusted.
        panel = Panel(
            Text(str(tool_input)), title=f"[tool.confirm.question]{name}[/]", border_style="tool.confirm.question"
        )
        # `tool_input` is passed so an argument-aware rule can address an MCP tool's own arguments
        # (e.g. "mcp__db__query:SELECT *"). Tool-wide and "*" rules behave as before.
        proceed, blocked = tools.confirm_action(ctx.console, panel, ctx, name, tool_input=tool_input)
        if blocked:
            return blocked, False
        if not proceed:
            return "User declined to run this tool call.", False
        return ctx.mcp_bridge.call_tool(name, tool_input)

    return f"Unknown tool: {name}", True
