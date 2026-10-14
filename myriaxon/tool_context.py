from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from rich.console import Console

from .constants import DEFAULT_MODE


@dataclass
class ToolContext:
    client: object  # anthropic.Anthropic or openai.OpenAI, depending on the active provider
    model: str
    max_tokens: int
    console: Console
    yolo: bool
    tools: list[dict]
    mode: str = DEFAULT_MODE
    todos: list[dict] = field(default_factory=list)
    skills: dict = field(default_factory=dict)
    mcp_bridge: object | None = None
    url_mcp_servers: list[dict] = field(default_factory=list)
    agent_factory: Callable[..., object] | None = None
    subagent_types: dict = field(default_factory=dict)
    hooks: dict = field(default_factory=dict)
    background_procs: dict = field(default_factory=dict)
    background_id_counter: int = 0
    living_subagents: dict = field(default_factory=dict)
    subagent_id_counter: int = 0
    scheduler: object | None = None
    settings: object | None = None
    # Loop detection state -- tracks consecutive identical (name, args) tool calls.
    last_tool_signature: tuple | None = None
    tool_repeat_count: int = 0
    # The outcome-hashed loop detector for this run (see myriaxon/loop_detection.py). Per-context rather than a
    # module global, so two agents in one process -- a session turn and a Locus waking -- cannot pollute each
    # other's counts. None means the cheap signature check above is the only guard, which is the behaviour every
    # existing caller already has; a caller wanting the three stronger detectors sets one.
    loop_detector: object | None = None
    # The capability proving this run is still alive (see myriaxon/run_capability.py). Set by an agent at the
    # start of a turn and closed at the end, so anything holding the context -- a plugin handler, a background
    # thread, a sub-agent -- can be refused rather than acting for a run that has finished or been cancelled.
    run_capability: object | None = None
    # The turn's ceiling and its running spend (see myriaxon/run_budget.py). Carried on the context so a long
    # tool can read `remaining()` and narrow its own scope rather than running into a wall.
    run_budget: object | None = None
    # When True, run_bash executes inside a throwaway Docker container instead of directly on
    # the host. See tools._sandboxed_shell_argv.
    sandbox_enabled: bool = False
    # The directory a self-directed Locus is confined to, or "" for an ordinary turn.
    #
    # This single field is what makes "fully autonomous in an isolated environment" a fact rather than
    # a promise. It does two things, and it needs to do both:
    #
    #   * it turns the permission layer from ASK into ALLOW for mutating tools, because a headless
    #     run has nobody to answer a prompt. Measured: a Locus's `run_bash` printed "Proceed? [y/n]"
    #     into a console with no input, retried, and died on "invalid message format" after 235
    #     seconds having produced nothing. That is why an unattended Locus appeared able to do nothing
    #     but its one poster objective.
    #   * it makes the file tools REFUSE any path outside the directory, which is what keeps the
    #     autonomy from being a grant of the whole machine. Auto-approving without this would be the
    #     worst of both: no oversight and no boundary.
    #
    # An empty value means "not a Locus", so every existing caller keeps the interactive behaviour.
    locus_workspace: str = ""
    # When True, dispatch_tool prints every tool call's name/arguments and result inline --
    # toggled via /trace, for when a response feels wrong and you want to see exactly what the
    # model actually did without digging through logs.
    trace_enabled: bool = False
    # Opt-in (off by default -- this is a deliberately conservative heuristic, not a solved
    # problem) via /ground: after each turn, flag file paths the reply mentions that no tool call
    # this turn actually touched, as a soft "verify before trusting this" signal.
    grounding_enabled: bool = False
    # Reset at the start of every turn (see cli._run_turn_safely) -- paths any path-taking tool
    # (read_file, write_file, edit_file, grep_search, glob_search, git_diff) was called with.
    touched_paths_this_turn: set = field(default_factory=set)
    # Tool NAMES called this turn. Reset alongside `touched_paths_this_turn` at the start of a turn.
    #
    # Added because an agent can claim to have verified something it never ran, and nothing could
    # detect it: measured across seven benchmark runs, a Locus reported "verified by tracing the test
    # cases" and "the shell is blocked" while `run_bash` was present, permitted and working -- and was
    # never called once. The claim and the reality were indistinguishable from the outside.
    #
    # A set of names rather than a count: the question asked of it is "was this capability exercised
    # at all", not "how many times".
    tools_called_this_turn: set = field(default_factory=set)
    # len(agent.messages) at the moment the current turn started -- set by cli._run_turn_safely
    # before calling agent.run_turn(). Tagged onto each checkpoint (see checkpoints.record_checkpoint)
    # so /rewind can revert the conversation to "right before this turn" alongside the files.
    current_turn_message_count: int = 0
    # Optional IQB-14 trajectory recorder (see myriaxon/bench/state.py). None in normal CLI use --
    # when set, tool_registry.dispatch_tool reports every tool call (name, args, result, duration,
    # token delta) to it. Typed loosely on purpose: importing the bench package here would create a
    # cycle, and the protocol is a single `record(name, args, is_error, result)` method.
    bench_trace: object | None = None
    # Optional IQB-14 budget guard (see myriaxon/bench/guard.py). None in normal CLI use. When set,
    # dispatch_tool re-checks the declared step ceiling after every tool call -- the only place a
    # tool-only loop (one that never issues another model request) can be stopped.
    bench_guard: object | None = None
    # The active ModelProfile (myriaxon/model_profiles.py). Controls tool-result truncation, whether
    # delegation is offered, and the context thresholds. None means "no adaptation", which is only
    # correct for programmatic/benchmark drivers that set their own limits.
    # The agent whose tool list this context serves. A reference, not a search: the
    # deferral state lives on the agent (see tool_registry.DEFERRED_GROUPS), and the main
    # agent is registered nowhere else -- only spawned sub-agents are.
    owner: Any = None
    # The locus the terminal is currently talking to, set by `/loci chat <name>`. Empty means the
    # prompt goes to Myriaxon, which is the default. Lives here because the REPL's input path and the
    # prompt both already hold this context, and it is a property of the conversation rather than of
    # the agent.
    active_dot: str = ""
    profile: object | None = None
    # Latest context-budget reading (myriaxon/context_budget.py), refreshed by the agent before each
    # request and by the REPL for the status line. Kept here because it is per-conversation state
    # that both the agent and the UI need to read.
    budget_state: object | None = None
    # Set when a tool call was rejected for a malformed argument, so the agent can append the exact
    # expected shape to the result for weak models that otherwise repeat the same broken call.
    last_argument_error: str | None = None
    # Characters allowed for the *next* tool result. Written by the agent from the current
    # context-budget reading (tighter as the window fills) and read by dispatch_tool. None means
    # "use the profile's static default".
    result_char_limit: int | None = None
    # True once a result has been truncated this conversation, so the UI can say so once instead of
    # nagging on every call. This now means what it says: content the model has LOST. An oversized result is
    # spilled rather than cut, so the recoverable case sets `spilled_any_result` instead and leaves this False.
    truncated_any_result: bool = False
    # True once a result has been spilled to disk with a head+tail preview (see myriaxon/spill.py). Kept separate
    # from the flag above because the two mean opposite things about recoverability: a spill is readable by a
    # `read_file`, a truncation is gone. Collapsing them would make "the model lost this" indistinguishable from
    # "the model can go and read this".
    spilled_any_result: bool = False
    # The tool currently being dispatched. Read by the spill layer to name the file it writes, so a spilled result
    # is identifiable later without opening it.
    current_tool_name: str = ""
    # Identifies the session a spill belongs to. Spilled files live under a per-session directory hashed from
    # this, so one session cannot read or overwrite another's spilled output.
    session_id: str = ""
