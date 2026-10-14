"""Adaptation hooks shared by all three provider agents.

These functions are the single place where a raw tool result becomes what the model actually sees,
and where a bare validation error becomes actionable feedback. Keeping them here (rather than
duplicated in `agent.py` / `openai_agent.py` / `gemini_agent.py`) is deliberate: the entire point of
profiles is that a weak model is treated consistently regardless of which wire protocol is in use,
and three copies of this logic would drift within a release.
"""
from __future__ import annotations

from . import context_budget
from .logging_setup import logger
from .model_profiles import ModelProfile


def format_result_for_model(content: str, ctx) -> tuple[str, bool]:
    """Apply the profile's result budget to one tool result.

    Returns (text_for_model, was_truncated). The *recorded* trace keeps the full text (see
    tool_registry.dispatch_tool), so an evaluation can still score what the tool really returned
    while the model only ever sees what it could actually hold in its window.

    An oversized result is **spilled** before it is shrunk. The ordering is the point: shrinking destroys the
    middle of the text and gives the model no way to get it back, so an over-long result could only be recovered
    by repeating the tool call with a narrower scope -- which costs the call again and often returns the same
    oversized answer. Spilling writes the full text to disk, keeps a head+tail preview and names the file, so the
    same result is recoverable by a `read_file`.

    A spill failure returns the original content plus a warning, so the shrink below still runs and the behaviour
    degrades to exactly what it was before this was added.
    """
    profile: ModelProfile | None = getattr(ctx, "profile", None)
    if profile is None or content is None:
        return content, False

    limit = getattr(ctx, "result_char_limit", None)
    if limit is None:
        limit = profile.tool_result_chars
    # The hard cap applies even when the caller supplied a limit, so no code path can put a
    # multi-megabyte blob in front of the model.
    limit = min(int(limit), profile.tool_result_hard_cap)

    text = str(content)
    if len(text) > limit:
        from . import spill

        outcome = spill.spill(
            text,
            tool_name=str(getattr(ctx, "current_tool_name", "") or ""),
            session_id=str(getattr(ctx, "session_id", "") or ""),
            max_inline_tokens=max(1, limit // spill.CHARS_PER_TOKEN),
        )
        if outcome.spilled and outcome.record is not None:
            ctx.spilled_any_result = True
            text = outcome.content
        for warning in outcome.warnings:
            logger.warning("spill: %s", warning)

    text, truncated = context_budget.shrink_tool_result(text, limit)
    if truncated:
        ctx.truncated_any_result = True
    return text, truncated


def deliver_steers(agent) -> int:
    """Append any queued mid-turn steers to `agent.messages`. Returns how many were delivered.

    ## Why this lives here rather than in each agent

    All three providers need it, at the same point in their loops, and three copies would drift -- the same reasoning
    as `append_runtime_context` and `format_result_for_model`.

    ## Why the call belongs at the STEP BOUNDARY and nowhere else

    Interrupting mid-step leaves a `tool_use` block with no matching `tool_result`, and the provider then rejects the
    transcript on every later turn. That constraint is already why `run_budget.check()` runs at the boundary and
    nowhere else. A steer inserted anywhere else would corrupt the conversation the same way.

    So the guarantee is precise and worth stating: **a steer takes effect on the NEXT request, not the one in
    flight.** A steer cannot change a response that has already been sent.

    Never raises: a correction is additive, and failing a turn because one could not be delivered would be worse
    than the correction arriving late. Draining makes it idempotent, so a steer cannot be replayed into a later
    request and read as a person repeating themselves.
    """
    try:
        queue = getattr(agent, "steer_queue", None)
        if queue is None:
            return 0
        steers = queue.drain()
        if not steers:
            return 0

        from . import steering

        messages = getattr(agent, "messages", None)
        if messages is None:
            return 0
        for steer in steers:
            # Appended as an ordinary user turn, because that is the shape every provider accepts. The steer's
            # PROVENANCE stays in the `Steer` object and is recorded on the agent, not encoded into the text: a
            # marker the model is asked to parse is a second and weaker statement of a fact the harness already
            # knows, and a model can be talked out of reading a marker.
            messages.append(steering.as_user_turn(steer))

        agent.last_steer_sources = [s.source for s in steers]
        note = steering.provenance_note(steers)
        console = getattr(agent, "console", None)
        if note and console is not None:
            try:
                from rich.markup import escape as _escape

                console.print(f"[dim]{_escape(note)}[/]")
            except Exception:  # noqa: BLE001
                pass
        tool_context = getattr(agent, "tool_context", None)
        if tool_context is not None:
            try:
                tool_context.last_steer_provenance = list(agent.last_steer_sources)
            except Exception:  # noqa: BLE001
                pass
        return len(steers)
    except Exception:  # noqa: BLE001
        logger.debug("steer delivery skipped", exc_info=True)
        return 0


def append_runtime_context(agent, turn_run=None) -> bool:
    """Append a runtime-context snapshot to `agent.messages` when something that matters changed.

    Lives here rather than in each agent for the same reason `format_result_for_model` does: all three providers
    must behave identically, and three copies would drift within a release.

    Returns whether a snapshot was appended. Never raises -- a snapshot is an optimisation for the model's
    situational awareness, and failing a turn because a timestamp could not be rendered would be a poor trade.
    """
    try:
        from . import runtime_context

        messages = getattr(agent, "messages", None)
        if messages is None:
            return False

        tracker = getattr(agent, "_runtime_tracker", None)
        if tracker is None:
            tracker = runtime_context.RuntimeContextTracker()
            agent._runtime_tracker = tracker

        remaining = ""
        if turn_run is not None:
            try:
                steps = turn_run.remaining().get("steps")
                if steps is not None:
                    remaining = f"{steps} step(s) left of {turn_run.budget.max_steps}"
            except Exception:
                remaining = ""

        # A finished background job is announced HERE, through the runtime-context carrier, because that is the
        # mechanism that already exists for volatile per-step facts. Announcing it as a synthetic user message
        # instead would put words in the user'''s mouth; announcing it nowhere is the polling this service replaces.
        #
        # marks each job as reported, so a completion is announced EXACTLY ONCE. A job that
        # re-reported on every step would nag until the model learned to ignore the block -- the same
        # training-a-reader-to-ignore failure the diagnostics module was designed around.
        try:
            from . import jobs as _jobs

            for _report in _jobs.pending_notifications(getattr(agent, "tool_context", None)):
                messages.append({
                    "role": "user",
                    "content": f"[background job finished] {_report}",
                })
            _jobs.keep_bounded(getattr(agent, "tool_context", None))
        except Exception:
            logger.debug("background job notification skipped", exc_info=True)

        context = runtime_context.RuntimeContext(
            mode=str(getattr(agent, "mode", "") or ""),
            workspace=str(getattr(agent, "workspace", "") or ""),
            sandbox="container" if getattr(getattr(agent, "tool_context", None), "sandbox_enabled", False)
            else "host",
            approval="yolo" if getattr(getattr(agent, "tool_context", None), "yolo", False) else "ask",
            budget=remaining,
        )
        return runtime_context.append_if_changed(messages, context, tracker)
    except Exception:
        logger.debug("runtime context snapshot skipped", exc_info=True)
        return False


def coach_argument_error(error: str, tool_name: str) -> str:
    """Append the exact expected shape to a validation error.

    Measured failure mode this targets: a weak model omits a required argument, receives
    "missing required argument 'path'", and re-sends the *same* call -- because nothing told it what
    a valid call looks like. The schema below is the tool's own declared schema, so the feedback is
    exact rather than a generic hint to "try again".
    """
    from .tool_registry import _get_tool_schema_index

    schema = _get_tool_schema_index().get(tool_name)
    if not schema:
        return error
    properties = schema.get("properties", {})
    required = schema.get("required", [])
    lines = ["", "Correct the call and try again. This tool expects:"]
    for key, spec in properties.items():
        marker = " (required)" if key in required else " (optional)"
        kind = spec.get("type", "any")
        description = (spec.get("description") or "").split(".")[0][:90]
        entry = f'  - "{key}": {kind}{marker}'
        if description:
            entry += f" -- {description}"
        lines.append(entry)
    lines.append("Example: " + _example_call(tool_name, properties, required))
    return error + "\n".join(lines)


def _example_call(tool_name: str, properties: dict, required: list) -> str:
    import json

    sample = {}
    for key in required or list(properties)[:1]:
        spec = properties.get(key, {})
        kind = spec.get("type", "string")
        if kind in ("integer", "number"):
            sample[key] = 1
        elif kind == "boolean":
            sample[key] = True
        elif kind == "array":
            sample[key] = []
        elif kind == "object":
            sample[key] = {}
        elif key == "path":
            sample[key] = "path/to/file.py"
        elif key == "command":
            sample[key] = "python -c \"print(1)\""
        else:
            sample[key] = "..."
    return json.dumps({tool_name: sample}, default=str)


def filter_tools(tools: list[dict] | None, profile: ModelProfile | None) -> list[dict] | None:
    """Remove tools this profile withholds (see ModelProfile.disabled_tools).

    Tool schemas are charged on every request, and delegation tools are the worst offenders for a
    weak model: they carry long descriptions, they are the biggest context sink when used, and a
    model that cannot finish a two-step task in its own window will not do better in a sub-agent's
    smaller one. Withholding them is a context saving *and* a capability guard.
    """
    if not tools or profile is None:
        return tools
    blocked = set(profile.disabled_tools)
    if not profile.allow_delegation:
        blocked |= {"task", "parallel_tasks", "send_to_subagent"}
    if not blocked:
        return tools
    return [t for t in tools if _tool_name(t) not in blocked]


def _tool_name(tool: dict) -> str:
    """Both shapes are handled here because this runs for all three providers: the registry's own
    Anthropic-shaped defs use {name}, OpenAI's function-calling shape nests it under
    {"type": "function", "function": {"name": ...}}, and a filter that only understood one of them
    would silently withhold nothing on the other two providers."""
    if not isinstance(tool, dict):
        return ""
    if tool.get("name"):
        return str(tool["name"])
    function = tool.get("function")
    if isinstance(function, dict) and function.get("name"):
        return str(function["name"])
    return ""


def budget_for_agent(agent, *, system_prompt: str, tools_chars: int = 0):
    """Evaluate the context budget for an agent and push the result onto its tool context.

    Called immediately before every model request, which is the last moment at which the decision is
    still free -- after the request is sent, an over-window conversation is the provider's error to
    report and the user's tokens to waste.
    """
    profile: ModelProfile | None = getattr(agent, "profile", None)
    ctx = getattr(agent, "tool_context", None)
    if profile is None or ctx is None:
        return None

    state = context_budget.evaluate(
        agent.messages,
        profile,
        system_prompt=system_prompt,
        tools_chars=tools_chars,
        reserve_output_tokens=min(int(getattr(agent, "max_tokens", 2048) or 2048), 4096),
    )
    ctx.budget_state = state

    # Shrink the per-result allowance as the window fills, so the model is not handed one more
    # enormous result exactly when it has the least room to reason about it.
    if state.action in ("prune", "compact", "refuse"):
        ctx.result_char_limit = max(600, profile.tool_result_chars // 3)
    elif state.action == "warn":
        ctx.result_char_limit = max(1_200, profile.tool_result_chars // 2)
    else:
        ctx.result_char_limit = profile.tool_result_chars
    return state


# Tool results older than this many turns are the first thing to go: they are usually a file that
# has since been edited or a listing that has since been superseded, and they are the largest single
# bucket in a real session.
PRUNE_AGE_TURNS = 2
PRUNE_MIN_CHARS = 600


def apply_context_action(agent, state, *, allow_compaction: bool = True, system_prompt: str = "") -> str:
    """Graduated response to a full window. Returns a short human-readable description of what was
    done (empty when nothing was), so the REPL can print an honest notice instead of silently
    rewriting the conversation underneath the user.

    Ordering matters and is deliberate: pruning tool results is free and reversible in effect (the
    model can re-read the file), whereas compaction costs a model call and destroys detail. So prune
    first, and only compact when pruning was not enough.

    ``system_prompt`` is passed in rather than derived from ``agent.messages[0]``. That derivation
    was wrong for the Anthropic agent, whose ``messages[0]`` is the first *user* message -- a list of
    content blocks once attachments are present -- so the re-evaluation after pruning could raise
    TypeError on a list, or silently under-count the prompt and let compaction fire late.
    """
    from . import compaction

    profile: ModelProfile | None = getattr(agent, "profile", None)
    if profile is None or state is None:
        return ""

    if state.action == "warn":
        return f"context {state.percent}% full ({state.used_tokens:,}/{state.window:,} tokens)"

    if state.action == "refuse":
        # Refusing is the correct behaviour: sending this request would be rejected by the provider
        # after billing, and a partial failure inside a turn is worse for the user than a clear stop.
        raise ContextExhausted(
            f"Context window is {state.percent}% full ({state.used_tokens:,} of {state.window:,} tokens). "
            f"Use /compact to summarize the conversation, /clear to start over, or narrow the task."
        )

    pruned = compaction.prune_stale_tool_results(
        agent.messages, max_age_turns=PRUNE_AGE_TURNS, min_chars=PRUNE_MIN_CHARS
    )
    if pruned:
        after = context_budget.evaluate(
            agent.messages,
            profile,
            system_prompt=system_prompt,
            reserve_output_tokens=min(int(getattr(agent, "max_tokens", 2048) or 2048), 4096),
        )
        agent.tool_context.budget_state = after
        if after.action != "compact":
            return f"pruned {pruned} stale tool result(s); context now {after.percent}% full"
        state = after

    if state.action == "compact":
        if not allow_compaction:
            return f"context {state.percent}% full -- compaction disabled, results will be truncated"
        compactor = getattr(agent, "compact", None)
        if compactor is None:
            return f"context {state.percent}% full -- this provider cannot auto-compact"
        try:
            compactor()
        except Exception as exc:  # noqa: BLE001 -- failing to compact must not kill the user's turn
            return f"context {state.percent}% full -- auto-compact failed ({exc!r})"
        return f"auto-compacted at {state.percent}% context usage"
    return ""


class ContextExhausted(RuntimeError):
    """Raised instead of sending a request that the provider would reject for being over-window."""
