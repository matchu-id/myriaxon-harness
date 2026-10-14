from __future__ import annotations

import functools
import json
import os
from typing import TYPE_CHECKING

from rich.console import Console
from rich.live import Live
from rich.markup import escape
# `rich.markdown` is deferred (see theme.render_markdown): ~2.5s of markdown_it import time paid on
# every startup otherwise, for a feature only used when displaying a final answer.

from . import attachments, hooks, memory_store, model_adapt, server_tools, skills, session_store, subagents, tool_registry
from . import steering as _steering

if TYPE_CHECKING:
    # Only needed for the type hint below -- importing the real anthropic SDK unconditionally
    # here (even just for `import anthropic`, with no actual use) cost ~36s of the ~45s total
    # startup time in this environment (confirmed via `python -X importtime`), since agent.py is
    # imported by cli.py regardless of which provider is actually configured. `from __future__
    # import annotations` (PEP 563) means this annotation is never evaluated at runtime, so no
    # import is needed outside type-checking.
    import anthropic
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
    COMPACT_KEEP_LAST_TURNS,
    COMPACTION_BETA_HEADER,
    CONTEXT_WARNING_THRESHOLD,
    DEFAULT_MAX_TOKENS,
    DEFAULT_MODE,
    MAX_PAUSE_TURN_RESTARTS,
    MCP_BETA_HEADER,
    PROVIDER_ANTHROPIC,
    STALE_TOOL_RESULT_MAX_AGE_TURNS,
    STALE_TOOL_RESULT_MIN_CHARS,
)
from .theme import ThreeColorSpinner, render_markdown
from .tool_context import ToolContext

# See _maybe_warn_context_usage -- only pay for a real count_tokens API call once the cheap
# char-count estimate already suggests we're within striking distance of CONTEXT_WARNING_THRESHOLD.
_CHEAP_ESTIMATE_PRECHECK_THRESHOLD = 0.6


class Agent:
    def __init__(
        self,
        client: anthropic.Anthropic,
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
        compact: bool = True,
        initial_messages: list[dict] | None = None,
        subagent_types: dict | None = None,
        hooks: dict | None = None,
        project_instructions: str = "",
        scheduler=None,
        settings=None,
        fallback_model: str = "",
        profile=None,
    ) -> None:
        self.client = client
        self.model = model
        self.console = console
        self.yolo = yolo
        self.max_tokens = max_tokens
        self.messages: list[dict] = list(initial_messages) if initial_messages else []
        self.todos: list[dict] = []
        self.skills = skills or {}
        self.mcp_bridge = mcp_bridge
        self.url_mcp_servers = url_mcp_servers or []
        self.effort = effort
        self.thinking = thinking
        self.max_input_tokens = max_input_tokens
        self.mode = mode
        self.compact_enabled = compact
        self.subagent_types = subagent_types or {}
        self.hooks = hooks or {}
        self.project_instructions = project_instructions
        self.scheduler = scheduler
        self.settings = settings
        self.fallback_model = fallback_model
        self.usage_totals = UsageTotals()
        # Mid-turn corrections, delivered at the next step boundary by `_deliver_steers`.
        #
        # Created here rather than lazily so `offer()` is discoverable on the object a caller already holds, and so
        # a producer can queue a steer before the first turn has begun. See `myriaxon/steering.py` for why every
        # steer must carry its own source.
        self.steer_queue = _steering.SteerQueue()
        # The sources of the steers delivered in the most recent step, for a status line and for tests.
        self.last_steer_sources: list[str] = []
        # Model profile (see model_profiles.py). None disables adaptation entirely.
        self.profile = profile
        self.temperature: float | None = None
        self.seed: int | None = None

        if tools is not None:
            self.tools = tools
        else:
            self.tools = tool_registry.build_tool_defs(
                mcp_tool_defs=mcp_tool_defs,
                url_mcp_server_names=[s["name"] for s in self.url_mcp_servers],
            )
        # Profile-level tool withholding, applied before the tool context exists so the prompt, the
        # dispatcher and the model all see one consistent tool set.
        self.tools = model_adapt.filter_tools(self.tools, self.profile)
        # Then deferral: specialised tools are withheld until `request_tools` unlocks their group.
        # 60% of the tool payload, charged on every request, for capabilities a coding turn almost
        # never reaches for. Applied here rather than in the builder so the full set stays available
        # to the dispatcher for argument validation -- the tool must still *work* the moment it is
        # loaded, and it must be impossible to call one that was never sent.
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
            Agent,
            self.client,
            self.model,
            self.console,
            self.yolo,
            self.max_tokens,
            skills=self.skills,
            mcp_bridge=self.mcp_bridge,
            url_mcp_servers=self.url_mcp_servers,
            effort=self.effort,
            thinking=self.thinking,
            max_input_tokens=self.max_input_tokens,
            mode=self.mode,
            compact=self.compact_enabled,
            subagent_types=self.subagent_types,
            hooks=self.hooks,
            project_instructions=self.project_instructions,
            scheduler=self.scheduler,
            settings=self.settings,
            fallback_model=self.fallback_model,
        )

    def _deliver_steers(self) -> None:
        """Append any queued steers as user turns, at the step boundary.

        Delegates to `model_adapt.deliver_steers`, which all three providers call, so the three cannot drift. See
        that function for why the step boundary is the only safe place and for the precise guarantee it gives.
        """
        model_adapt.deliver_steers(self)

    def _append_runtime_context(self, turn_run=None) -> None:
        """Append a runtime-context snapshot when something that matters changed.

        Never raises: a snapshot is an optimisation for the model's situational awareness, and failing a turn
        because the timestamp could not be rendered would be a poor trade.
        """
        try:
            from . import runtime_context

            tracker = getattr(self, "_runtime_tracker", None)
            if tracker is None:
                tracker = runtime_context.RuntimeContextTracker()
                self._runtime_tracker = tracker

            remaining = ""
            if turn_run is not None:
                try:
                    left = turn_run.remaining()
                    steps = left.get("steps")
                    if steps is not None:
                        remaining = f"{steps} step(s) left of {turn_run.budget.max_steps}"
                except Exception:
                    remaining = ""

            context = runtime_context.RuntimeContext(
                mode=str(getattr(self, "mode", "") or ""),
                workspace=str(getattr(self, "workspace", "") or ""),
                sandbox="container" if getattr(self.tool_context, "sandbox_enabled", False) else "host",
                approval="yolo" if getattr(self.tool_context, "yolo", False) else "ask",
                budget=remaining,
            )
            runtime_context.append_if_changed(self.messages, context, tracker)
        except Exception:
            logger.debug("runtime context snapshot skipped", exc_info=True)

    def _system_prompt(self) -> str:
        """Profile-tiered prompt -- identical construction to the OpenAI-compatible agent, so a model
        gets the same instructions whichever protocol it is reached through."""
        from .system_prompt import build_system_prompt

        return build_system_prompt(self)

    def last_assistant_text(self) -> str:
        if not self.messages:
            return ""
        last = self.messages[-1]
        if last.get("role") != "assistant":
            return ""
        content = last["content"]
        if isinstance(content, str):
            return content
        return "\n".join(block.text for block in content if getattr(block, "type", None) == "text")

    def _prepare_context(self) -> None:
        """Same graduated context response as the OpenAI-compatible agent.

        Anthropic additionally has native server-side auto-compaction, but that only helps when the
        model is Anthropic -- and it fires server-side, after the request is already accepted. This
        local check runs first so behavior is identical across providers and the user gets a notice
        rather than discovering a summarized conversation later.
        """
        if self.profile is None:
            return
        # These two arguments feed the budget estimate, so getting them wrong makes the estimate
        # wrong in the direction that hurts: too low and the agent only notices the window is full
        # after the provider has already rejected the request.
        #
        # `self.messages[0]` looks like the system prompt but is not one -- for Anthropic it is the
        # first *user* message, and once attachments are attached its content is a list of blocks,
        # not a string. Passing that to the char-counting estimator raised TypeError, so the real
        # system prompt is built here (the same call the request itself makes, and it is cached).
        system_prompt = self._system_prompt()
        tools_chars = len(json.dumps(self.tools, default=str)) if self.tools else 0
        state = model_adapt.budget_for_agent(
            self,
            system_prompt=system_prompt,
            tools_chars=tools_chars,
        )
        if state is None:
            return
        notice = model_adapt.apply_context_action(
            self,
            state,
            allow_compaction=bool(getattr(self, "compact_enabled", True)),
            system_prompt=system_prompt,
        )
        if notice and not getattr(self, "_context_notice_shown", False):
            self.console.print(f"[warning]{escape(notice)}[/]")
            self._context_notice_shown = state.action != "ok"

    def run_turn(self, user_input: str) -> None:
        snapshot_len = len(self.messages)
        cleaned_text, attachment_paths = attachments.parse_attachments(user_input)
        cleaned_text = hooks.run_user_prompt_submit(cleaned_text, self.tool_context)
        if attachment_paths:
            content_blocks: list[dict] = [{"type": "text", "text": cleaned_text}]
            for path in attachment_paths:
                block, error = attachments.build_attachment_block(path, PROVIDER_ANTHROPIC)
                if error:
                    self.console.print(f"[warning]{escape(error)}[/]")
                    continue
                content_blocks.append(block)
            self.messages.append({"role": "user", "content": content_blocks})
        else:
            # `cleaned_text`, not `user_input`: the UserPromptSubmit hook (and any plugin hook)
            # returns a possibly rewritten prompt, and the branch above already honours it. Using
            # the raw input here meant every hook that rewrites a prompt silently did nothing in
            # the common no-attachment case -- the one case hooks are actually written for.
            self.messages.append({"role": "user", "content": cleaned_text})

        pause_restarts = 0

        # One capability per turn and one detector per turn, attached to the tool context for its duration.
        #
        # The capability closes in the existing `finally`, so it is closed whatever happens -- including the
        # rollback path above. Anything still holding the context afterwards (a plugin handler that kept a
        # reference, a background thread, a sub-agent) can then be refused rather than acting on behalf of a
        # turn that has ended. Without this the harness had no way to answer "is the work this belongs to still
        # alive?", which is the class of bug `run_capability` exists to close.
        #
        # The detector is fresh per turn because "repeated" should mean repeated *in this turn*: carrying counts
        # across turns would fire on a coincidence from unrelated earlier work.
        from . import loop_detection as _loop_detection
        from . import run_budget as _run_budget
        from . import run_capability as _run_capability

        turn_capability = _run_capability.new_run_capability("agent-turn", run_id=str(id(self)))
        turn_detector = _loop_detection.LoopDetector(run_id=str(id(self)))
        # Warned once per turn, not on every step: a warning that repeats is one the reader learns to skim.
        self._budget_warned = False
        self.last_stop_reason = None
        # A turn has a ceiling. Neither reference harness bounds its loop (one defaults to unlimited iterations,
        # the other has no budget member in its end-reason type at all), and both say so -- DeepSeek's own scan
        # calls the absence "right for an interactive GUI, wrong for a script". Myriaxon runs unattended Loci, so
        # it is a script at least half the time.
        turn_budget = _run_budget.budget_for(
            unattended=bool(getattr(self, "unattended", False)),
            interactive=not bool(getattr(self, "unattended", False)),
            label="locus run" if getattr(self, "unattended", False) else "turn",
        )
        turn_run = _run_budget.RunBudget(turn_budget)
        if self.tool_context is not None:
            self.tool_context.run_capability = turn_capability
            self.tool_context.loop_detector = turn_detector
            self.tool_context.run_budget = turn_run

        # Volatile per-step facts are APPENDED to the transcript rather than written into the system prompt.
        # Anything volatile in the prompt is expensive out of proportion to its size: the prompt is the head of
        # the request, so changing one character of it invalidates the provider's cached prefix for the whole
        # conversation -- a session that ran for an hour re-bills every token because the clock moved.
        #
        # Appending costs one small message. Rewriting the previous snapshot would cost the whole conversation at
        # full price, which is why `runtime_context` never edits a snapshot it has already emitted, and why the
        # timestamp it carries is quantised to the minute.
        self._append_runtime_context(turn_run)

        try:
            while True:
                # The budget is checked HERE, at the step boundary, and nowhere else. Interrupting mid-step would
                # leave a `tool_use` block with no matching `tool_result`, and the API rejects such a transcript on
                # every later turn -- the same failure the rollback below exists to prevent. A budget that
                # corrupts a session would be worse than no budget.
                #
                # Reaching the ceiling is a normal end, not a failure: the reason is named, the user is told, and
                # the partial turn is KEPT (unlike an exception, which rolls back). Five completed steps are real
                # work and discarding them because a sixth was refused would throw away the result of the budget.
                try:
                    turn_run.check()
                except _run_budget.BudgetExceeded as stop:
                    self.console.print(
                        f"[warning]{escape(str(stop))}[/]"
                    )
                    self.console.print(
                        "[dim]The turn ended at its budget with the work so far kept. "
                        "Ask again to continue, or raise the budget.[/]"
                    )
                    self.last_stop_reason = stop.reason
                    return

                turn_run.begin_step()

                # A steer is delivered HERE, at the step boundary, and the placement is the whole mechanism.
                #
                # The budget check above runs here and nowhere else for a reason already on record: interrupting
                # mid-step leaves a `tool_use` block with no matching `tool_result`, and the API rejects such a
                # transcript on every later turn. A steer inserted anywhere else would corrupt the conversation the
                # same way. Between steps it is just another user turn, and the next request carries it.
                #
                # This also means a steer takes effect on the NEXT request, not the one in flight -- which is the
                # honest guarantee. A steer cannot change a response that has already been sent.
                self._deliver_steers()

                self._prepare_context()
                response = self._stream_response()
                self.messages.append({"role": "assistant", "content": response.content})
                usage = getattr(response, "usage", None)
                if usage is not None:
                    from .usage import cache_fields

                    cache_read, cache_write = cache_fields(usage)
                    self.usage_totals.add(
                        usage.input_tokens, usage.output_tokens, cache_read, cache_write
                    )
                    # Counted from the response rather than estimated, so the token ceiling is about what was
                    # actually spent rather than about the size of the strings.
                    turn_run.add_tokens(
                        int(getattr(usage, "input_tokens", 0) or 0)
                        + int(getattr(usage, "output_tokens", 0) or 0)
                    )

                warning = turn_run.warn_at()
                if warning is not None and not self._budget_warned:
                    self._budget_warned = True
                    self.console.print(
                        f"[warning]{escape(turn_budget.label)} is close to its "
                        f"{escape(warning.value)} ceiling ({escape(turn_run.spend.render())}).[/]"
                    )

                if response.stop_reason == "pause_turn":
                    pause_restarts += 1
                    if pause_restarts > MAX_PAUSE_TURN_RESTARTS:
                        self.console.print("[warning]Too many pause_turn restarts in a row; stopping.[/]")
                        return
                    continue

                if response.stop_reason == "max_tokens":
                    self.console.print("[warning]Response truncated at max_tokens.[/]")

                tool_uses = [block for block in response.content if block.type == "tool_use"]
                if not tool_uses:
                    # The model produced a turn with no tool calls, which is the only clean completion signal
                    # there is. Deliberately NOT a self-reported "I am done" string: a loop whose exit condition
                    # is the model's own claim has no exit condition.
                    self.last_stop_reason = _run_budget.StopReason.COMPLETED
                    return

                results = []
                for block in tool_uses:
                    content, is_error = tool_registry.dispatch_tool(block.name, block.input, self.tool_context)
                    # Profile-aware sizing, applied at the boundary between the tool and the model:
                    # the tool produced the full text (and the IQB-14 trace recorded it), but the
                    # model only receives as much as its window can actually hold.
                    content, _truncated = model_adapt.format_result_for_model(content, self.tool_context)
                    results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": content,
                            "is_error": is_error,
                        }
                    )
                self.messages.append({"role": "user", "content": results})
        except BaseException:
            # Roll back unconditionally, including on KeyboardInterrupt -- confirmed live this
            # matters: an interrupt (or any other failure) landing between "assistant calls a
            # tool" and "tool result appended" leaves a structurally invalid message list (a
            # tool_use with no matching tool_result), which the API then rejects on every future
            # turn or session resume (real observed error: "insufficient tool messages following
            # tool_calls message"). That's strictly worse than losing the partial turn, so
            # KeyboardInterrupt is no longer treated as a safe-to-preserve special case -- and
            # without the rollback, finally's _save_session() below would persist the broken
            # state to disk, so the same error would recur on every later turn or session resume.
            del self.messages[snapshot_len:]
            raise
        finally:
            # Closed whatever happened above -- success, rollback, or interruption. Cleared from the context too,
            # so a holder that reads it after this point finds nothing rather than a closed capability it might
            # mistake for a live one.
            turn_capability.close("the turn ended")
            if self.tool_context is not None:
                self.tool_context.run_capability = None
                self.tool_context.loop_detector = None
                self.tool_context.run_budget = None
            self._maybe_warn_context_usage()
            self._save_session()

    def _save_session(self) -> None:
        session_store.save_session(
            PROVIDER_ANTHROPIC, self.model, self.mode, self.effort, self.thinking, self.messages
        )
        memory_store.record_session(
            os.getcwd(), PROVIDER_ANTHROPIC, self.model, render_transcript(self.messages, PROVIDER_ANTHROPIC)
        )

    def compact(self) -> None:
        # Rolling window: only the portion older than the last COMPACT_KEEP_LAST_TURNS turns
        # gets summarized -- the recent turns are kept verbatim. (No auto-compact trigger here
        # -- Anthropic already gets real server-side auto-compaction via context_management; this
        # is only reached via the manual /compact command.)
        old, recent = split_at_turn_boundary(self.messages, COMPACT_KEEP_LAST_TURNS)
        transcript = render_transcript(old, PROVIDER_ANTHROPIC)
        if not transcript.strip():
            self.console.print("[dim]Nothing to compact.[/]")
            return

        # The start marker is written BEFORE the summariser call. Compaction spans an await, so a process that
        # dies during it leaves a state nobody can interpret from the transcript alone -- was the summary applied
        # or not? The unmatched start is what makes the interruption detectable on the next open.
        _recorder = _audit_recorder()
        if _recorder is not None:
            _recorder.record_compaction_start(messages=len(old), kept_turns=COMPACT_KEEP_LAST_TURNS)

        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=2048,
                system=SUMMARY_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": transcript}],
            )
        except Exception as exc:
            self.console.print(f"[error]Compaction failed: {escape(str(exc))}[/]")
            return

        summary = "\n".join(block.text for block in response.content if getattr(block, "type", None) == "text")

        # Refuse a summary that is not smaller than what it replaces. Applying one would make the next request
        # larger while destroying the history it was meant to compress -- and it compounds, because the next
        # compaction would be summarising this summary. Checked BEFORE `record_compaction` so a rejected summary
        # is not recorded as though it had been applied.
        verdict = check_summary(summary, transcript=transcript)
        if not verdict.ok:
            _mark_compaction_end(applied=False, reason="summary not smaller than the history replaced")
            self.console.print(f"[warning]Compaction refused: {escape(verdict.reason)}[/]")
            return

        # Record what is about to leave the conversation. `old` is unrecoverable after the next
        # line runs, and the summary alone is not a substitute: it is what the model chose to keep.
        _mark_compaction_end(applied=True, summary_chars=len(summary))
        record_compaction(old, summary, PROVIDER_ANTHROPIC, kept_turns=COMPACT_KEEP_LAST_TURNS)
        self.messages = [
            {"role": "user", "content": f"[Conversation compacted. Summary of earlier context:]\n{summary}"},
            {"role": "assistant", "content": "Understood — continuing from that summary."},
            *recent,
        ]
        # Arm the post-compaction loop guard. Compaction rewrites the transcript, so any loop that was in
        # progress resumes with its history apparently erased and its counters reset -- the agent goes straight
        # back to the call it was repeating. The guard snapshots the pre-compaction call tail and aborts if the
        # same call-and-result triple appears again afterwards, which is evidence that compressing the history did
        # not break the loop.
        self._arm_post_compaction_guard()
        self.console.print(f"[success]Compacted (kept last {COMPACT_KEEP_LAST_TURNS} turns verbatim).[/]")

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

        # Cheap, no-network estimate first (same ~4-chars/token heuristic OpenAI/Gemini agents
        # already use) -- only pay for a real count_tokens API call (a full network round-trip)
        # once the rough estimate already suggests we're near the warning threshold. Confirmed
        # via a fresh profiling pass this was previously an UNCONDITIONAL network call on every
        # single turn (max_input_tokens is always populated at startup, so the old early-return
        # never actually triggered), adding real latency to every response even for short
        # conversations nowhere near the limit -- the one provider agent still doing this;
        # OpenAI/Gemini never made a network call here at all.
        estimated_tokens = len(str(self.messages)) // 4
        if estimated_tokens / self.max_input_tokens < _CHEAP_ESTIMATE_PRECHECK_THRESHOLD:
            return

        try:
            count = self.client.messages.count_tokens(
                model=self.model,
                system=self._system_prompt(),
                tools=self.tools,
                messages=self.messages,
            )
        except Exception:
            return

        fraction = count.input_tokens / self.max_input_tokens
        if fraction >= CONTEXT_WARNING_THRESHOLD:
            pct = round(fraction * 100)
            self.console.print(
                f"[warning]~{pct}% of this model's context window used "
                f"({count.input_tokens}/{self.max_input_tokens} tokens) — "
                "try /compact to summarize, or /clear to reset, if you hit a context-limit error.[/]"
            )

    def _stream_response(self):
        text_buffer = ""
        thinking_active = False

        # Prompt caching: the system prompt (skills/subagent listings, project instructions) and
        # tool defs are identical on every single request this session, so marking them as an
        # ephemeral cache breakpoint means only the actually-new conversation tail gets paid for
        # on every call after the first. Built fresh here (not stored back onto self.tools) --
        # self.tools' entries are the same shared dict objects as tool_registry's module-level
        # TOOL_DEFS, so mutating them in place would leak a cache_control key into every other
        # session/agent for the rest of the process.
        cached_system = [
            {"type": "text", "text": self._system_prompt(), "cache_control": {"type": "ephemeral"}}
        ]
        cached_tools = self.tools
        if cached_tools:
            cached_tools = [*cached_tools[:-1], {**cached_tools[-1], "cache_control": {"type": "ephemeral"}}]

        stream_kwargs = dict(
            model=self.model,
            max_tokens=self.max_tokens,
            system=cached_system,
            tools=cached_tools,
            messages=self.messages,
        )
        if self.effort:
            stream_kwargs["output_config"] = {"effort": self.effort}
        if self.thinking:
            stream_kwargs["thinking"] = {"type": "adaptive", "display": "summarized"}

        betas = []
        if self.url_mcp_servers:
            betas.append(MCP_BETA_HEADER)
        if self.compact_enabled:
            betas.append(COMPACTION_BETA_HEADER)

        if betas:
            beta_kwargs = dict(betas=betas, **stream_kwargs)
            if self.url_mcp_servers:
                beta_kwargs["mcp_servers"] = [
                    {"type": "url", "url": s["url"], "name": s["name"]} for s in self.url_mcp_servers
                ]
            if self.compact_enabled:
                beta_kwargs["context_management"] = {"edits": [{"type": "compact_20260112"}]}
            stream_cm = self.client.beta.messages.stream(**beta_kwargs)
        else:
            stream_cm = self.client.messages.stream(**stream_kwargs)

        with stream_cm as stream:
            # transient=True so the spinner erases itself when the block exits, leaving the terminal
            # clean for the REPL prompt. Without it, leftover live output is drawn over by the
            # prompt and the user cannot see what they are typing.
            #
            # The finished text is therefore reprinted AFTER the block, and that is not an optional
            # polish: transient teardown restores the cursor and erases the whole live frame, so the
            # streamed answer is destroyed by the very option that keeps the terminal clean. The
            # comment here used to claim "the finished text is printed by the caller from the
            # returned message" -- no caller did, so on this provider every CLI answer was erased the
            # instant it finished. The OpenAI agent has always reprinted (openai_agent.py, after its
            # Live block); this brings the two into line.
            with Live(console=self.console, refresh_per_second=12, transient=True) as live:
                live.update(ThreeColorSpinner())
                for event in stream:
                    # Checked once per chunk, which is what makes cancelling actually stop the
                    # tokens: raising here leaves the `with` block, and leaving it closes the
                    # provider's response. Before this the cancel flag was set and never read, so
                    # the turn ran to completion while the UI said it had been cancelled.
                    from . import cancellation

                    token = getattr(self, "cancel_token", None)
                    if token is not None:
                        token.raise_if_cancelled(text_buffer)
                    if event.type == "content_block_start":
                        if getattr(event.content_block, "type", None) == "compaction":
                            self.console.print("[dim]— conversation compacted —[/]")
                            continue
                        status = server_tools.status_line_for_block(event.content_block)
                        if status:
                            self.console.print(status)
                    elif event.type == "content_block_delta":
                        if event.delta.type == "text_delta":
                            if thinking_active:
                                self.console.print()
                                thinking_active = False
                            text_buffer += event.delta.text
                            live.update(render_markdown(text_buffer))
                        elif event.delta.type == "thinking_delta":
                            thinking_active = True
                            self.console.print(
                                f"[dim italic]{escape(event.delta.thinking)}[/]", end=""
                            )
            # After the Live block, so the answer lands on a clean line instead of being erased with
            # the live frame. Mirrors the OpenAI agent. The dashboard console accumulates this
            # through its own stream_answer path, which is unaffected.
            if text_buffer:
                self.console.print(render_markdown(text_buffer))
            return stream.get_final_message()


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
