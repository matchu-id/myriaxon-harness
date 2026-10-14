import copy
import functools
import json
import os
import sys
import time
from concurrent import futures

from rich.console import Console
from rich.live import Live
from rich.markup import escape
# `rich.markdown` is deferred (see theme.render_markdown): importing it here costs ~2.5s of
# markdown_it parsing on every startup, for a feature used only when printing a final answer.

from . import attachments, hooks, memory_store, model_adapt, session_store, subagents, tool_registry
from . import steering
from .usage import UsageTotals
from .compaction import (
    SUMMARY_SYSTEM_PROMPT,
    check_summary,
    prune_stale_tool_results,
    record_compaction,
    render_transcript,
    split_at_turn_boundary,
)
from .logging_setup import logger
from .constants import (
    APP_NAME,
    AUTO_COMPACT_THRESHOLD,
    COMPACT_KEEP_LAST_TURNS,
    CONTEXT_WARNING_THRESHOLD,
    DEFAULT_MAX_TOKENS,
    DEFAULT_MODE,
    MAX_EMPTY_RESPONSE_RETRIES,
    PROVIDER_OPENAI_COMPAT,
    PROVIDER_OPENROUTER,
    REASONING_TOKEN_HEADROOM,
    STALE_TOOL_RESULT_MAX_AGE_TURNS,
    STALE_TOOL_RESULT_MIN_CHARS,
    TRANSIENT_ERROR_BACKOFF_SECONDS,
    TRANSIENT_ERROR_RETRIES,
)
from .theme import ThreeColorSpinner, render_markdown
from .tool_context import ToolContext

def _stream_answer_to_console(console, text: str) -> None:
    """Report the accumulating answer to a console that streams, if it has that capability.

    Deliberately duck-typed rather than an isinstance check against the dashboard's console class:
    the CLI must not import the dashboard package at all, and any console without `stream_answer`
    (every terminal Console, including in tests) simply does nothing.
    """
    publisher = getattr(console, "stream_answer", None)
    if callable(publisher):
        publisher(text)



class _PendingToolCall:
    def __init__(self) -> None:
        self.id: str | None = None
        self.name: str | None = None
        self.arguments_fragments: list[str] = []

    def arguments_text(self) -> str:
        return "".join(self.arguments_fragments)


# Tools safe to run CONCURRENTLY when one response asks for several.
#
# Deliberately a small allowlist of reads, not a denylist of writes. A tool this module has never seen
# is treated as unsafe to parallelise, because the cost of serialising an unknown read is a little
# latency while the cost of parallelising an unknown write is a bug nobody can reproduce.
_PARALLEL_SAFE_TOOLS = frozenset({
    "read_file", "glob_search", "grep_search", "git_diff", "git_status",
})


def _parse_call_arguments(call) -> dict:
    """Best-effort argument parse, used by the parallel path. Malformed input becomes `{}` HERE only
    because the serial path already reported it -- see `_parse_arguments_or_error`."""
    try:
        parsed = json.loads(call.arguments_text() or "{}")
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _parse_arguments_or_error(call) -> tuple[dict, str]:
    """Parse a tool call's arguments, returning an error STRING instead of silently emptying them.

    The old behaviour was `except json.JSONDecodeError: parsed_input = {}`, which converted a broken
    call into a valid call with no arguments. The tool then ran with defaults or failed schema
    validation, and the model had no way to learn that its JSON was the problem -- so it retried the
    same broken shape. Measured in the audit as a real gap; the fix is to say what happened and show
    what arrived, which is the only thing that lets a model correct itself.
    """
    raw = call.arguments_text() or ""
    if not raw.strip():
        return {}, ""
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        preview = raw if len(raw) <= 200 else raw[:200] + "...(truncated)"
        return {}, (
            f"Your arguments for {call.name} were not valid JSON ({exc.msg} at position "
            f"{exc.pos}). Received: {preview!r}. Re-send the call with well-formed JSON."
        )
    if not isinstance(parsed, dict):
        return {}, (
            f"Your arguments for {call.name} must be a JSON object, not "
            f"{type(parsed).__name__}. Received: {raw[:200]!r}"
        )
    return parsed, ""


class _PendingReasoningDetail:
    """Accumulates one reasoning_details[] entry's streamed fragments by index -- confirmed via
    a live probe against OpenRouter that each delta carries {type, text, format, index}, the
    same index-keyed fragment pattern already used for tool_calls above."""

    def __init__(self) -> None:
        self.type: str | None = None
        self.format: str | None = None
        self.text_fragments: list[str] = []
        self.summary_fragments: list[str] = []

    def to_dict(self) -> dict:
        d: dict = {"type": self.type, "format": self.format}
        text = "".join(self.text_fragments)
        summary = "".join(self.summary_fragments)
        if text:
            d["text"] = text
        if summary:
            d["summary"] = summary
        return d


def _active_api_error_types() -> dict:
    """Exception classes to catch in the retry loop, for the client actually in use.

    Returns a dict of tuples so `except` can still be used with real classes (never a bare
    `except Exception`, which would swallow programming errors). When the direct HTTP client is
    active, the `openai` SDK is deliberately NOT imported to build these tuples -- naming the SDK's
    exception classes is not worth ~44s of import time, and the direct client raises its own
    equivalents which the CLI already classifies.
    """
    try:
        from .direct_http_client import DirectAPIError, DirectConnectionError

        if type(DirectAPIError).__name__ == "type":
            direct = {"status": (DirectAPIError,), "connection": (DirectConnectionError,),
                      "generic": (DirectAPIError, DirectConnectionError)}
        else:  # pragma: no cover
            direct = {"status": (), "connection": (), "generic": ()}
    except Exception:  # noqa: BLE001
        direct = {"status": (), "connection": (), "generic": ()}

    # Only consult the SDK when it is already loaded, or when the active client IS the SDK. This
    # keeps the fast path free of the import while preserving SDK behaviour when it is in use.
    sdk_loaded = "openai" in sys.modules
    if not sdk_loaded:
        return direct if direct["status"] else {"status": (), "connection": (), "generic": ()}

    import openai as openai_mod

    return {
        "status": (openai_mod.APIStatusError,) + direct["status"],
        "connection": (openai_mod.APIConnectionError,) + direct["connection"],
        "generic": (openai_mod.APIError,) + direct["generic"],
    }


def _is_bare_sdk_api_error(exc: BaseException) -> bool:
    """True only for the SDK's exact base APIError class.

    The SDK raises this bare class for a mid-stream SSE event carrying an embedded error, which is
    worth retrying. Subclasses are more specific failures already handled above, and the direct
    client's errors are never bare SDK APIErrors, so they are not retried here.
    """
    if "openai" not in sys.modules:
        return False
    import openai as openai_mod

    return type(exc) is openai_mod.APIError


class OpenAIAgent:
    """Mirrors Agent's external contract (run_turn/last_assistant_text) but speaks the
    OpenAI-compatible chat completions protocol instead of Anthropic's Messages API."""

    def __init__(
        self,
        client,
        model: str,
        console: Console,
        yolo: bool,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        tools: list[dict] | None = None,
        skills: dict | None = None,
        mcp_bridge=None,
        mcp_tool_defs: list[dict] | None = None,
        url_mcp_servers: list[dict] | None = None,
        effort: str | None = None,
        thinking: bool = False,
        max_input_tokens: int | None = None,
        mode: str = DEFAULT_MODE,
        initial_messages: list[dict] | None = None,
        subagent_types: dict | None = None,
        hooks: dict | None = None,
        project_instructions: str = "",
        scheduler=None,
        provider: str = PROVIDER_OPENAI_COMPAT,
        settings=None,
        fallback_model: str = "",
        profile=None,
    ) -> None:
        self.client = client
        self.model = model
        self.provider = provider
        self.settings = settings
        self.fallback_model = fallback_model
        self.console = console
        self.yolo = yolo
        self.max_tokens = max_tokens
        self.messages: list[dict] = list(initial_messages) if initial_messages else []
        self.todos: list[dict] = []
        self.skills = skills or {}
        self.mcp_bridge = mcp_bridge
        # url_mcp_servers accepted for a consistent constructor with Agent, but the remote MCP
        # connector is an Anthropic-only feature -- not meaningful for this provider.
        self.url_mcp_servers: list[dict] = []
        self.effort = effort
        self.thinking = thinking
        self.max_input_tokens = max_input_tokens
        self.mode = mode
        self.subagent_types = subagent_types or {}
        self.hooks = hooks or {}
        self.project_instructions = project_instructions
        self.scheduler = scheduler
        self.usage_totals = UsageTotals()
        # Mid-turn corrections, delivered at the next step boundary by `model_adapt.deliver_steers`.
        #
        # Present on all three providers so the mechanism cannot exist on one and be silently absent on another --
        # the failure mode `model_adapt` exists to prevent for runtime context and result formatting.
        self.steer_queue = steering.SteerQueue()
        # The sources of the steers delivered in the most recent step, for a status line and for tests.
        self.last_steer_sources: list[str] = []
        # Model profile (see model_profiles.py). None means "no adaptation" -- correct only for the
        # benchmark runner, which declares its own budgets and wants raw behaviour measured.
        self.profile = profile
        # Sampling knobs, set by the IQB-14 runner for reproducibility; ignored by the UI path.
        self.temperature: float | None = None
        self.seed: int | None = None

        if tools is not None:
            self.tools = tools
        else:
            self.tools = tool_registry.build_tool_defs_openai(mcp_tool_defs=mcp_tool_defs)
        # Withhold tools the profile disables *before* the tool context is built, so the model, the
        # dispatcher and the prompt all agree on one tool set.
        self.tools = model_adapt.filter_tools(self.tools, self.profile)
        # Then deferral: specialised tools are withheld until equest_tools\ unlocks their group.
        # See \	ool_registry.DEFERRED_GROUPS\ for the measurement behind this.
        self.unlocked_tool_groups: set[str] = set()
        # Kept BEFORE visibility is applied, so `request_tools` can recompute from a complete,
        # profile-correct list. It must be this list and not a fresh builder call: a builder knows
        # nothing about the active profile or this session's plugin and MCP tools, so rebuilding from
        # one could reintroduce a withheld tool or silently drop an MCP one the user configured.
        self.all_tools = list(self.tools)
        self.tools = tool_registry.apply_tool_visibility(self.tools, self.unlocked_tool_groups)

        self.tool_context = ToolContext(
            client=self.client,
            model=self.model,
            max_tokens=self.max_tokens,
            console=self.console,
            yolo=self.yolo,
            tools=self.tools,
            todos=self.todos,
            skills=self.skills,
            mcp_bridge=self.mcp_bridge,
            url_mcp_servers=self.url_mcp_servers,
            mode=self.mode,
            subagent_types=self.subagent_types,
            hooks=self.hooks,
            scheduler=self.scheduler,
            settings=self.settings,
            profile=self.profile,
        )
        # So a tool handler can reach the agent that owns this context -- `request_tools`
        # loads deferred tool groups onto it.
        self.tool_context.owner = self
        self.tool_context.agent_factory = functools.partial(
            OpenAIAgent,
            self.client,
            self.model,
            self.console,
            self.yolo,
            self.max_tokens,
            skills=self.skills,
            mcp_bridge=self.mcp_bridge,
            effort=self.effort,
            thinking=self.thinking,
            max_input_tokens=self.max_input_tokens,
            mode=self.mode,
            subagent_types=self.subagent_types,
            hooks=self.hooks,
            project_instructions=self.project_instructions,
            scheduler=self.scheduler,
            provider=self.provider,
            settings=self.settings,
            fallback_model=self.fallback_model,
        )

    def _system_prompt(self) -> str:
        """Profile-tiered prompt (see system_prompt.py): a small model gets the rules that prevent
        concrete failures, not eleven simultaneous instructions it will half-follow."""
        from .system_prompt import build_system_prompt

        return build_system_prompt(self)

    def last_assistant_text(self) -> str:
        for message in reversed(self.messages):
            if message.get("role") == "assistant":
                return message.get("content") or ""
        return ""

    def _prepare_context(self) -> None:
        """Check the window and take graduated action before issuing the next request.

        Failure this prevents: on a small-window model, exceeding the context produces a provider
        error mid-turn after the tokens are already billed, with no graceful path. Here the harness
        prunes, then compacts, then refuses with an explanation -- in that order, because pruning is
        free and compaction costs a model call.
        """
        if self.profile is None:
            return
        state = model_adapt.budget_for_agent(
            self,
            system_prompt=self._system_prompt(),
            tools_chars=len(json.dumps(self.tools, default=str)) if self.tools else 0,
        )
        if state is None:
            return
        notice = model_adapt.apply_context_action(
            self, state,
            # Honours the dashboard's "Automatic compaction" setting.
            # Hardcoded True here meant turning it off changed nothing for this provider,
            # while the setting was visible in the UI -- a control that does not control.
            allow_compaction=bool(getattr(self, "compact_enabled", True)),
            system_prompt=self._system_prompt(),
        )
        if notice and not getattr(self, "_context_notice_shown", False):
            self.console.print(f"[warning]{escape(notice)}[/]")
            # Announce once per condition, not once per request -- otherwise a long turn buries the
            # conversation under identical warnings.
            self._context_notice_shown = state.action != "ok"

    def run_turn(self, user_input: str) -> None:
        if not self.messages:
            self.messages.append({"role": "system", "content": self._system_prompt()})
        snapshot_len = len(self.messages)

        # A turn has a ceiling, and until this was added it had one ONLY on Anthropic.
        #
        # `run_budget` was wired into `agent.py` and nowhere else, so `openai_agent.py` and
        # `gemini_agent.py` -- which between them serve DeepSeek, Netra, Ollama, OpenRouter and every
        # generic OpenAI-compatible endpoint -- ran with NO step limit, NO wall-clock limit and NO
        # token ceiling. Found by an audit that grepped for `run_budget` in this file and got zero
        # hits, which contradicted the documented limits: the numbers were real, they just did not
        # apply to the providers in use.
        #
        # The consequence is specific rather than theoretical: an unattended Locus on a local or
        # gateway model could loop until the account or the machine gave out, with the documented
        # ceilings reading as protection that was not there.
        from . import loop_detection as _loop_detection
        from . import run_budget as _run_budget
        from . import run_capability as _run_capability

        unattended = bool(getattr(self, "unattended", False))
        # Snapshotted before the turn so the ledger records what THIS turn spent rather than the
        # agent's running total. See `usage.record_turn_delta`: recording the total each turn counts
        # the same tokens again, and the error grows with the square of the turn count -- a cost
        # report that is most wrong exactly when the spend is largest.
        _usage_before = copy.copy(self.usage_totals)
        turn_capability = _run_capability.new_run_capability("agent-turn", run_id=str(id(self)))
        turn_detector = _loop_detection.LoopDetector(run_id=str(id(self)))
        self._budget_warned = False
        self.last_stop_reason = None
        turn_budget = _run_budget.budget_for(
            unattended=unattended,
            interactive=not unattended,
            label="locus run" if unattended else "turn",
        )
        turn_run = _run_budget.RunBudget(turn_budget)
        if self.tool_context is not None:
            self.tool_context.run_capability = turn_capability
            self.tool_context.loop_detector = turn_detector
            self.tool_context.run_budget = turn_run

        cleaned_text, attachment_paths = attachments.parse_attachments(user_input)
        cleaned_text = hooks.run_user_prompt_submit(cleaned_text, self.tool_context)
        # In-process plugin hooks. A plugin's user_prompt handler may return a replacement prompt;
        # the last non-empty return wins, so a chain of plugins can each rewrite it.
        from .plugins import get_registry

        plugin_registry = get_registry()
        if plugin_registry.loaded:
            for replacement in plugin_registry.run_hooks('user_prompt', cleaned_text, self.tool_context):
                if isinstance(replacement, str) and replacement.strip():
                    cleaned_text = replacement
        if attachment_paths:
            content_blocks: list[dict] = [{"type": "text", "text": cleaned_text}]
            for path in attachment_paths:
                block, error = attachments.build_attachment_block(path, self.provider)
                if error:
                    self.console.print(f"[warning]{escape(error)}[/]")
                    continue
                content_blocks.append(block)
            self.messages.append({"role": "user", "content": content_blocks})
        else:
            # `cleaned_text`, matching the attachment branch and the Gemini agent: the
            # UserPromptSubmit hook above may have rewritten the prompt, and appending the raw
            # input discarded that rewrite in the no-attachment case.
            self.messages.append({"role": "user", "content": cleaned_text})
        # Volatile per-step facts are APPENDED rather than written into the system prompt: the prompt is the head
        # of the request, so one changed character invalidates the provider's cached prefix for the whole
        # conversation. `append_if_changed` keeps this to one small message per state change.
        model_adapt.append_runtime_context(self)

        try:
            while True:
                # Checked at the step boundary and nowhere else, for the reason recorded in
                # `agent.py`: interrupting mid-step leaves a `tool_use` with no matching
                # `tool_result`, and the provider rejects such a transcript on every later turn.
                # Reaching the ceiling is a normal end, not a failure -- the partial turn is KEPT.
                try:
                    turn_run.check()
                except _run_budget.BudgetExceeded as stop:
                    self.console.print(f"[warning]{escape(str(stop))}[/]")
                    self.last_stop_reason = getattr(stop, "reason", None)
                    return
                # Steers are delivered HERE, at the step boundary, before the next request. Anywhere else would
                # split a `tool_use` from its `tool_result` and corrupt the transcript -- the same reason the budget
                # is checked at the boundary. See `model_adapt.deliver_steers`.
                model_adapt.deliver_steers(self)
                self._prepare_context()
                content, tool_calls, finish_reason, reasoning_details, reasoning_content = self._stream_response_with_retry()

                # A completed stream (stop/absent finish_reason) with no text and no tool calls
                # is a degenerate provider completion, not a model choosing to say nothing --
                # documented DeepSeek behavior (their own harness maps this to a retryable
                # EMPTY_RESPONSE error) but not provider-specific in principle. Retry a bounded
                # number of times before giving up and returning the empty response as-is.
                empty_retries = 0
                while (
                    not content
                    and not tool_calls
                    and finish_reason in (None, "stop")
                    and empty_retries < MAX_EMPTY_RESPONSE_RETRIES
                ):
                    empty_retries += 1
                    self.console.print(
                        f"[warning]Empty response from the model -- retrying "
                        f"({empty_retries}/{MAX_EMPTY_RESPONSE_RETRIES})...[/]"
                    )
                    content, tool_calls, finish_reason, reasoning_details, reasoning_content = self._stream_response_with_retry()

                assistant_message: dict = {"role": "assistant", "content": content}
                if reasoning_details:
                    # Pass back unmodified next turn, per OpenRouter's documented pattern, so
                    # reasoning-capable models can continue from where they left off instead of
                    # losing their prior reasoning context on every new turn.
                    assistant_message["reasoning_details"] = reasoning_details
                if reasoning_content:
                    # DeepSeek's own contract (distinct from OpenRouter's reasoning_details
                    # shape above): a plain string field that must be serialized back verbatim
                    # on tool-call turns so the model can continue its reasoning chain.
                    assistant_message["reasoning_content"] = reasoning_content
                if tool_calls:
                    assistant_message["tool_calls"] = [
                        {
                            "id": call.id,
                            "type": "function",
                            "function": {"name": call.name, "arguments": call.arguments_text()},
                        }
                        for call in tool_calls
                    ]
                self.messages.append(assistant_message)

                if finish_reason == "length":
                    # A truncated turn is not a warning, it is a failed turn: the model was cut off
                    # mid-thought, so it produced no tool call and the loop ends having done nothing.
                    # Measured on `deepseek-v4.1-flash` at a 4096 ceiling -- the whole budget went on
                    # reasoning, `content` came back empty, no tool call followed, and the session
                    # simply stopped. Everything downstream saw a successful turn that accomplished
                    # no work.
                    #
                    # The ceiling is raised and the turn is retried once. Retrying rather than
                    # starting high is deliberate: a small prepaid account can be rejected outright
                    # when max_tokens exceeds its balance (the reason this default is low), so the
                    # large budget is only requested from a provider that has just proved it needs it.
                    if self.max_tokens < REASONING_TOKEN_HEADROOM:
                        raised = REASONING_TOKEN_HEADROOM
                        self.console.print(
                            f"[warning]Response hit the {self.max_tokens:,}-token output ceiling with "
                            f"no usable output -- retrying this turn with {raised:,}.[/]"
                        )
                        self.max_tokens = raised
                        # The truncated assistant message is dropped: keeping it would show the model
                        # its own cut-off sentence as history and invite it to continue mid-word.
                        self.messages.pop()
                        continue
                    self.console.print(
                        f"[warning]Response truncated at the model's max output length "
                        f"({self.max_tokens:,} tokens) even after raising the ceiling.[/]"
                    )

                if not tool_calls:
                    return

                # Read-only calls in one response run CONCURRENTLY; everything else stays serial.
                #
                # The audit found this loop dispatched every call in a plain `for`, so a model that
                # asked for three file reads paid three sequential round trips for work with no
                # ordering relationship at all. On a tool-using turn that is the difference between
                # one second and three.
                #
                # Only reads are parallelised, and the line is drawn conservatively: a write, a shell
                # command or anything that mutates shared state must keep the model's ordering,
                # because the model chose a sequence and reordering it silently would be a correctness
                # change disguised as a speed-up. `git_diff` and `git_status` are included because
                # they only read the repository.
                #
                # Results are appended in the ORIGINAL order regardless of completion order: the
                # provider matches tool messages to calls by `tool_call_id`, but a transcript whose
                # messages arrive shuffled makes a trace unreadable and makes a replayed request
                # differ from the one that was sent.
                readable = [
                    call for call in tool_calls
                    if call.name in _PARALLEL_SAFE_TOOLS
                ]
                parallel: dict[str, tuple[str, bool]] = {}
                if len(readable) > 1:
                    with futures.ThreadPoolExecutor(max_workers=min(len(readable), 8)) as pool:
                        pending = {
                            pool.submit(
                                tool_registry.dispatch_tool,
                                call.name,
                                _parse_call_arguments(call),
                                self.tool_context,
                            ): call
                            for call in readable
                        }
                        for future in futures.as_completed(pending):
                            call = pending[future]
                            try:
                                parallel[call.id] = future.result()
                            except Exception as exc:  # noqa: BLE001
                                parallel[call.id] = (f"Tool error: {exc}", True)

                for call in tool_calls:
                    if call.id in parallel:
                        content_str, is_error = parallel[call.id]
                    else:
                        # Malformed arguments are REPORTED, not silently emptied.
                        #
                        # The old `except json.JSONDecodeError: parsed_input = {}` turned a broken
                        # call into a well-formed call with no arguments -- so the tool ran with
                        # defaults, or failed validation, and the model had no way to know its JSON
                        # was the problem. It would retry the same broken shape. The error text now
                        # names the parse failure and shows what arrived, which is the one thing that
                        # lets the model fix it.
                        parsed_input, parse_error = _parse_arguments_or_error(call)
                        if parse_error:
                            content_str, is_error = parse_error, True
                        else:
                            content_str, is_error = tool_registry.dispatch_tool(
                                call.name, parsed_input, self.tool_context
                            )
                    # Profile-aware sizing: a weak model never receives a result it cannot hold.
                    # Applied here (not inside the tool) because it is the model's window that
                    # decides, and the full text is still what the IQB-14 trace records.
                    content_str, _truncated = model_adapt.format_result_for_model(content_str, self.tool_context)
                    if is_error:
                        # The OpenAI-style tool message has no structured is_error flag (unlike
                        # Anthropic's tool_result block) -- fold the signal into the text itself.
                        content_str = f"Error: {content_str}"
                    self.messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.id,
                            "content": content_str,
                        }
                    )
        except BaseException:
            # Roll back unconditionally, including on KeyboardInterrupt -- confirmed live this
            # matters: an interrupt (or any other failure) landing between "assistant calls a
            # tool" and "tool result appended" leaves a structurally invalid message list (a
            # tool_calls entry with no matching tool message), which the API then rejects on
            # every future turn or session resume (real observed error: "insufficient tool
            # messages following tool_calls message"). That's strictly worse than losing the
            # partial turn, so KeyboardInterrupt is no longer treated as a safe-to-preserve
            # special case -- and without the rollback, finally's _save_session() below would
            # persist the broken state to disk, so the same error would recur on every later turn
            # or session resume.
            del self.messages[snapshot_len:]
            raise
        finally:
            self._maybe_warn_context_usage()
            self._save_session()
            # Recorded in `finally`, not on the success path. A turn that hit the budget ceiling, was
            # cancelled, or failed mid-way has still SPENT -- and a cost report that omits exactly the
            # turns that went wrong is most wrong about the runs that cost most.
            try:
                from . import usage as _usage

                _usage.record_turn_delta(
                    self,
                    _usage_before,
                    provider=self.provider,
                    source=str(getattr(self.tool_context, "locus_run_source", "") or ""),
                    agent_name=str(getattr(self, "name", "") or ""),
                    run_id=str(id(self)),
                )
            except Exception:  # noqa: BLE001 - accounting must never fail a turn
                pass

    def _save_session(self) -> None:
        session_store.save_session(
            self.provider, self.model, self.mode, self.effort, self.thinking, self.messages
        )
        memory_store.record_session(
            os.getcwd(), self.provider, self.model, render_transcript(self.messages, self.provider)
        )

    def compact(self, auto: bool = False) -> None:
        # Rolling window: only the portion older than the last COMPACT_KEEP_LAST_TURNS turns
        # gets summarized -- the recent turns are kept verbatim so detail isn't lost on every
        # compaction, only once it's genuinely old.
        old, recent = split_at_turn_boundary(self.messages, COMPACT_KEEP_LAST_TURNS)
        transcript = render_transcript(old, self.provider)
        if not transcript.strip():
            if not auto:
                self.console.print("[dim]Nothing to compact.[/]")
            return

        # The start marker is written BEFORE the summariser call. Compaction spans an await, so a process that
        # dies during it leaves a state nobody can interpret from the transcript alone -- was the summary applied
        # or not? The unmatched start is what makes the interruption detectable on the next open.
        _recorder = _audit_recorder()
        if _recorder is not None:
            _recorder.record_compaction_start(messages=len(old), kept_turns=COMPACT_KEEP_LAST_TURNS)

        try:
            response = self.client.chat.completions.create(
                model=self.model,
                max_tokens=2048,
                messages=[
                    {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
                    {"role": "user", "content": transcript},
                ],
            )
            summary = response.choices[0].message.content or ""
        except Exception as exc:
            self.console.print(f"[error]Compaction failed: {escape(str(exc))}[/]")
            return

        # Record what is about to leave the conversation. `old` is unrecoverable after the next
        # line runs, and the summary alone is not a substitute: it is what the model chose to keep.
        # Refuse a summary that is not smaller than what it replaces. Applying one would make the next request
        # larger while destroying the history it was meant to compress -- and it compounds, because the next
        # compaction would be summarising this summary. Checked BEFORE `record_compaction` so a rejected summary
        # is not recorded as though it had been applied.
        verdict = check_summary(summary, transcript=transcript)
        if not verdict.ok:
            _mark_compaction_end(applied=False, reason="summary not smaller than the history replaced")
            self.console.print(f"[warning]Compaction refused: {escape(verdict.reason)}[/]")
            return

        _mark_compaction_end(applied=True, summary_chars=len(summary))
        record_compaction(old, summary, self.provider, kept_turns=COMPACT_KEEP_LAST_TURNS)
        self.messages = [
            {"role": "system", "content": self._system_prompt()},
            {"role": "user", "content": f"[Conversation compacted. Summary of earlier context:]\n{summary}"},
            {"role": "assistant", "content": "Understood — continuing from that summary."},
            *recent,
        ]
        prefix = "Auto-c" if auto else "C"
        # Arm the post-compaction loop guard. Compaction rewrites the transcript, so a loop that was in
        # progress resumes with its counters reset -- the agent goes straight back to the call it was
        # repeating. The guard snapshots the pre-compaction call tail and aborts if the same
        # call-and-result triple reappears, which is evidence that compressing did not break the loop.
        self._arm_post_compaction_guard()
        self.console.print(f"[success]{prefix}ompacted (kept last {COMPACT_KEEP_LAST_TURNS} turns verbatim).[/]")

    def _arm_post_compaction_guard(self) -> None:
        """Tell the loop detector that a compaction just happened. Safe to call with no detector."""
        detector = getattr(self.tool_context, "loop_detector", None) if self.tool_context else None
        if detector is None:
            return
        try:
            baseline = detector.arm_after_compaction()
        except Exception:
            logger.exception("could not arm the post-compaction loop guard")
            return
        if baseline:
            logger.debug("post-compaction guard armed with %d baseline call(s)", len(baseline))

    def _maybe_warn_context_usage(self) -> None:
        pruned = prune_stale_tool_results(
            self.messages, STALE_TOOL_RESULT_MAX_AGE_TURNS, STALE_TOOL_RESULT_MIN_CHARS
        )
        if pruned:
            self.console.print(f"[dim]Pruned {pruned} stale tool result(s) to save context.[/]")

        if not self.max_input_tokens:
            return
        # No universal tokenizer for arbitrary third-party models -- a rough heuristic
        # (roughly 4 characters per token) rather than an exact count.
        estimated_tokens = len(str(self.messages)) // 4
        fraction = estimated_tokens / self.max_input_tokens

        if fraction >= AUTO_COMPACT_THRESHOLD:
            self.console.print(
                f"[warning]~{round(fraction * 100)}% (estimated) of the context window used -- "
                "auto-compacting.[/]"
            )
            self.compact(auto=True)
        elif fraction >= CONTEXT_WARNING_THRESHOLD:
            pct = round(fraction * 100)
            self.console.print(
                f"[warning]~{pct}% (estimated) of this model's context window used "
                f"(~{estimated_tokens}/{self.max_input_tokens} tokens) — "
                "try /compact to summarize, or /clear to reset, if you hit a context-limit error.[/]"
            )

    def _stream_response_with_retry(
        self,
    ) -> tuple[str, list[_PendingToolCall], str | None, list[dict] | None, str | None]:
        """_stream_response(), retried a bounded number of times on transient upstream failures
        -- confirmed live (OpenRouter, google/gemini-3.8-flash) that the exact same request can
        fail non-deterministically three different ways: a real 5xx APIStatusError, a dropped
        APIConnectionError, or a mid-stream SSE event carrying an embedded error object with no
        HTTP status at all (the openai SDK raises that last one as a bare APIError, distinct
        from its own more specific subclasses). All three are the upstream backend being
        unstable, not a malformed request -- retrying the same request fresh often succeeds.
        Permanent errors (auth/rate-limit/bad-request/not-found -- all APIStatusError subclasses
        with a 4xx status) are deliberately left alone here and propagate immediately, same as
        before, so _run_turn_safely's existing classification/fallback logic in cli.py still
        sees them unchanged.

        Exception types come from whichever client is actually active. The unconditional
        `import openai` that used to sit here cost 25.6 SECONDS of this method's 28.8s total --
        the SDK was imported eagerly to name exception classes, even when the direct HTTP client
        (which needs no SDK) had already served the request successfully.
        """
        api_types = _active_api_error_types()

        attempt = 0
        while True:
            try:
                return self._stream_response()
            except api_types["status"] as exc:
                if getattr(exc, "status_code", 0) < 500 or attempt >= TRANSIENT_ERROR_RETRIES:
                    raise
            except api_types["connection"]:
                if attempt >= TRANSIENT_ERROR_RETRIES:
                    raise
            except api_types["generic"] as exc:
                # Retry only the SDK's exact base APIError (its mid-stream embedded-error case).
                # Any other generic failure, including the direct client's, is not retryable.
                if not _is_bare_sdk_api_error(exc) or attempt >= TRANSIENT_ERROR_RETRIES:
                    raise
            attempt += 1
            self.console.print(
                f"[warning]Transient API error -- retrying ({attempt}/{TRANSIENT_ERROR_RETRIES})...[/]"
            )
            time.sleep(TRANSIENT_ERROR_BACKOFF_SECONDS * attempt)

    def _stream_response(
        self,
    ) -> tuple[str, list[_PendingToolCall], str | None, list[dict] | None, str | None]:
        text_buffer = ""
        reasoning_content_buffer = ""
        thinking_active = False
        pending_calls: dict[int, _PendingToolCall] = {}
        pending_reasoning: dict[int, _PendingReasoningDetail] = {}
        finish_reason: str | None = None

        # OpenRouter's native web-search grounding -- billed through the same OpenRouter key,
        # no separate search API/credential needed. The model decides on its own whether a given
        # query needs it, same as how it decides whether to call any other tool. Confirmed live:
        # returns real, current results with structured url_citation annotations. OpenRouter-only
        # -- other OpenAI-compatible providers (e.g. DeepSeek, or a generic custom endpoint) don't
        # recognize this "plugins" field, so it's omitted for them rather than risking a rejected
        # request. Gated on the actual PROVIDER_OPENROUTER id now, not the generic
        # "openai_compat" bucket -- that bucket covers any OpenAI-compatible endpoint, not just
        # OpenRouter, and was sending this to non-OpenRouter custom endpoints too before.
        extra_body: dict = {"plugins": [{"id": "web"}]} if self.provider == PROVIDER_OPENROUTER else {}
        reasoning_config: dict = {}
        if self.effort:
            reasoning_config["effort"] = self.effort
        elif self.thinking:
            # No graded effort level requested, but --thinking means the user wants to see/use
            # reasoning -- some models only produce reasoning_details when explicitly enabled.
            reasoning_config["enabled"] = True
        if reasoning_config:
            extra_body["reasoning"] = reasoning_config

        # Sampling determinism, set only by the IQB-14 benchmark runner (see bench/guard.py). Left
        # as None in normal interactive use so Myriaxon keeps the provider's own defaults rather
        # than silently changing how the agent behaves for a human. Both fields are recorded in
        # the run manifest whether or not the provider honours them -- "we asked for temperature 0"
        # and "we got temperature 0" are different claims and must never be conflated.
        sampling: dict = {}
        temperature = getattr(self, "temperature", None)
        if temperature is not None:
            sampling["temperature"] = temperature
        seed = getattr(self, "seed", None)
        if seed is not None:
            sampling["seed"] = seed

        stream = self.client.chat.completions.create(
            model=self.model,
            max_tokens=self.max_tokens,
            messages=self.messages,
            tools=self.tools,
            stream=True,
            stream_options={"include_usage": True},
            extra_body=extra_body,
            **sampling,
        )

        # transient=True and a final print AFTER the block: the spinner must erase itself and the
        # finished answer must be written on a clean line, or the REPL prompt is drawn over leftover
        # live output and never becomes visible -- measured live on Gemini, where every message
        # needed an interrupt before it could be typed. The same pattern applies to every provider
        # that streams through a Live region.
        with Live(console=self.console, refresh_per_second=12, transient=True) as live:
            live.update(ThreeColorSpinner())
            for chunk in stream:
                # Checked once per chunk so cancelling stops the tokens rather than merely relabelling
                # the turn: raising leaves the `with` block, and leaving it closes the response.
                from . import cancellation

                token = getattr(self, "cancel_token", None)
                if token is not None:
                    token.raise_if_cancelled(text_buffer)
                # The usage-carrying final chunk has empty choices (per the real
                # ChatCompletionChunk docstring), so this must be checked before the
                # `if not chunk.choices: continue` skip below, or it's silently missed.
                if getattr(chunk, "usage", None) is not None:
                    from .usage import cache_fields

                    cache_read, cache_write = cache_fields(chunk.usage)
                    self.usage_totals.add(
                        chunk.usage.prompt_tokens, chunk.usage.completion_tokens,
                        cache_read, cache_write,
                    )

                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                delta = choice.delta

                # DeepSeek's own reasoning field -- a plain accumulated string, distinct from
                # OpenRouter's structured reasoning_details[] handled below. DeepSeek's first
                # thinking-mode chunk carries reasoning_content: "" (falsy), so no special-casing
                # is needed to avoid opening a spurious empty reasoning display -- `if fragment:`
                # already skips it.
                reasoning_fragment = getattr(delta, "reasoning_content", None)
                if reasoning_fragment:
                    if self.thinking:
                        thinking_active = True
                        self.console.print(f"[dim italic]{escape(reasoning_fragment)}[/]", end="")
                    reasoning_content_buffer += reasoning_fragment

                for entry in getattr(delta, "reasoning_details", None) or []:
                    index = entry.get("index", 0)
                    detail = pending_reasoning.setdefault(index, _PendingReasoningDetail())
                    if entry.get("type"):
                        detail.type = entry["type"]
                    if entry.get("format"):
                        detail.format = entry["format"]
                    entry_type = entry.get("type")
                    fragment = entry.get("text") if entry_type == "reasoning.text" else (
                        entry.get("summary") if entry_type == "reasoning.summary" else None
                    )
                    if fragment:
                        if entry_type == "reasoning.text":
                            detail.text_fragments.append(fragment)
                        else:
                            detail.summary_fragments.append(fragment)
                        if self.thinking:
                            thinking_active = True
                            self.console.print(f"[dim italic]{escape(fragment)}[/]", end="")

                if delta.content:
                    if thinking_active:
                        self.console.print()
                        thinking_active = False
                    text_buffer += delta.content
                    live.update(render_markdown(text_buffer))
                    _stream_answer_to_console(self.console, text_buffer)

                for tool_call_delta in delta.tool_calls or []:
                    call = pending_calls.setdefault(tool_call_delta.index, _PendingToolCall())
                    if tool_call_delta.id:
                        call.id = tool_call_delta.id
                    if tool_call_delta.function and tool_call_delta.function.name:
                        call.name = tool_call_delta.function.name
                    if tool_call_delta.function and tool_call_delta.function.arguments:
                        call.arguments_fragments.append(tool_call_delta.function.arguments)

                if choice.finish_reason:
                    finish_reason = choice.finish_reason

        ordered_calls = [pending_calls[i] for i in sorted(pending_calls)]
        reasoning_details = [pending_reasoning[i].to_dict() for i in sorted(pending_reasoning)] or None
        # Printed after the Live block so the answer lands on a clean line and the next prompt is
        # visible. Without this the streamed text was left inside the live region.
        if text_buffer:
            self.console.print(render_markdown(text_buffer))
        return text_buffer, ordered_calls, finish_reason, reasoning_details, (reasoning_content_buffer or None)


def _audit_recorder():
    """The active compaction recorder, or None. Lazily imported, for the same reason `compaction._audit` is."""
    try:
        from . import compaction_audit

        return compaction_audit.active()
    except Exception:
        return None


def _mark_compaction_end(*, applied: bool, reason: str = "", summary_chars: int = 0) -> None:
    """Close the compaction marker. Never raises: failing to record must not fail the compaction."""
    recorder = _audit_recorder()
    if recorder is None:
        return
    try:
        recorder.record_compaction_end(applied=applied, reason=reason, summary_chars=summary_chars)
    except Exception:
        pass
