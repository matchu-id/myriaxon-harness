"""Dashboard sessions: build an agent, run turns on a worker thread, publish what happens.

Each session owns:
  * its own agent (so model/mode can differ per session, and a session can be reused for follow-ups)
  * an EventBus the browser subscribes to
  * a TraceRecorder, which `tool_registry` already knows how to feed (`ctx.bench_trace`), giving the
    dashboard its trajectory view without any change to the agent code

Turns run on a worker thread because the agent is synchronous and streams for tens of seconds; the
HTTP handler returns immediately and the browser follows the SSE stream instead of holding a request
open.
"""
from __future__ import annotations

import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .console import EventConsole
from .events import EventBus
from . import store
from .. import cancellation


@dataclass
class SessionState:
    """What the UI needs to know about a session without running anything."""

    id: str
    title: str = "New session"
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    provider: str = ""
    model: str = ""
    mode: str = "auto_edit"
    busy: bool = False
    turn_count: int = 0
    error: str | None = None
    # Which persistent agent this session belongs to, if any. An explicit field rather than a
    # convention encoded in `title`: the Mission control view has to label a run as a locus's, and
    # parsing "<name> · locus" would break the moment someone renamed a session or a locus.
    locus: str = ""
    # The last usage snapshot, kept on the state so it survives a restart. The live totals belong to
    # the agent, which is rebuilt empty -- without this a session's spending read as zero the moment
    # the process ended, and the status line went blank for work that had already been paid for.
    usage: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "provider": self.provider,
            "model": self.model,
            "mode": self.mode,
            "busy": self.busy,
            "turn_count": self.turn_count,
            "error": self.error,
            "locus": self.locus,
            "usage": dict(self.usage or {}),
        }


class Session:
    """A live coding session the dashboard can talk to."""

    def __init__(self, session_id: str, workspace: Path, *, config: Any, mode: str = "auto_edit",
                 settings: Any = None, endpoints: Any = None) -> None:
        self.id = session_id
        self.workspace = workspace
        self.config = config
        self.mode = mode
        self.dashboard_settings = settings
        # User-defined endpoints, so /provider and /models can name them.
        self.endpoints = endpoints
        self.bus = EventBus()
        self.console = EventConsole(self.bus, session_id=session_id)
        self.state = SessionState(id=session_id, provider=config.provider, model=config.model, mode=mode)
        self.agent: Any = None
        self.manager: Any = None
        # The owning application, when there is one. Commands that read process-wide state use it.
        self.app: Any = None
        # Who this session speaks as. Empty for an ordinary session, in which case the agent asserts
        # the generic Myriaxon identity. A locus sets it to its own briefing, so the model knows it is
        # that locus -- see `system_prompt._sections`, where the defect that made a locus answer "I'm a
        # coding agent" is described.
        self.persona_identity: str = ""
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._cancelled = threading.Event()
        # The partial answer from a cancelled turn, kept so the `turn_end` payload can report what
        # the user actually saw before stopping.
        self._cancelled_note = ""
        # A locus run started through `DashboardApp.run_locus` holds its lease for the whole turn. Kept on
        # the session because the turn runs on a worker thread and the request that started it has
        # already returned; released from `_run_turn`'s completion.
        self._locus_lease: Any = None
        self._locus_lease_name: str = ""
        # Tool calls read back from disk, used until a live trace exists. Same shape as
        # `trajectory()` returns.
        self._restored_trajectory: list[dict] = []
        self._recorder: Any = None
        self._trace_state: Any = None
        # The conversation, kept here so it survives an agent rebuild. Switching provider or model
        # discards the agent (its client, endpoint and model are baked in), and without this the
        # transcript would go with it.
        self.history: list[dict] = []

    # -- agent lifecycle ----------------------------------------------------------
    def _ensure_agent(self, model: str | None = None) -> Any:
        """Build the agent once; rebuilt when the model changes."""
        target_model = model or self.config.model
        if self.agent is not None and getattr(self.agent, "model", None) == target_model:
            return self.agent

        from ..cli import _build_agent
        from ..hooks import load_hooks_config
        from ..project_instructions import load_project_instructions
        from ..scheduler import Scheduler
        from ..settings import load_settings
        from ..skills import discover_skills
        from ..subagents import discover_subagent_types

        settings = load_settings()
        # Dashboard preferences feed the same knobs the CLI exposes, so the two agree:
        #   workflow   -> agent_mode, which gates the delegation tools
        #   thinking   -> whether reasoning is displayed
        #   effort     -> the provider's own effort control
        #   max_tokens -> the reply ceiling (0 means "let the provider decide")
        #   yolo       -> always True here: the server has no terminal to answer a confirmation on,
        #                 so an approval prompt would hang the turn forever.
        prefs = self.dashboard_settings
        agent_mode = getattr(prefs, "workflow", "single") if prefs else "single"
        thinking = bool(getattr(prefs, "thinking", False)) if prefs else False
        effort = (getattr(prefs, "effort", "") or None) if prefs else None
        max_tokens = None
        if prefs is not None:
            configured = int(getattr(prefs, "max_tokens", 0) or 0)
            max_tokens = configured or None

        # MCP servers were previously not passed at all here (a literal `None` for the bridge), so a
        # server configured from this very UI contributed no tools to a web session -- it only ever
        # worked in the terminal. Both transports are started the same way the CLI starts them.
        mcp_bridge, mcp_tool_defs, mcp_notes = self._start_mcp()

        self.agent = _build_agent(
            self.config,
            target_model,
            self.console,
            True,  # yolo: the dashboard cannot answer interactive permission prompts
            mcp_bridge,
            mcp_tool_defs,
            [],    # url_mcp_servers: the Anthropic-only remote connector, not used here
            discover_skills(),
            discover_subagent_types(),
            load_hooks_config(),
            load_project_instructions(),
            Scheduler(),
            settings,
            max_tokens=max_tokens,
            effort=effort,
            thinking=thinking,
            mode=self.mode,
            no_compact=not (bool(getattr(prefs, "auto_compact", True)) if prefs else True),
            initial_messages=self._portable_history(),
            profile=None,
            agent_mode=agent_mode,
        )
        if self.agent is None:
            raise RuntimeError(
                f"Could not build an agent for provider {self.config.provider!r}. "
                "Check the provider SDK and credentials with `python scripts/set_provider.py --show`."
            )
        # Set AFTER construction, because the flag has to survive a rebuild -- the model or provider
        # can change mid-session, and a locus that forgot who it was on the next model switch would be
        # a worse bug than the one this fixes.
        self.agent.persona_identity = self.persona_identity
        self._attach_trace()
        return self.agent

    def _start_mcp(self) -> tuple[Any, list[dict], list[str]]:
        """Connect configured MCP servers for this agent. Returns (bridge, tool_defs, notes).

        A failure to start any single server is reported and skipped rather than aborting the agent
        build: a broken MCP entry must not make the dashboard unusable.
        """
        try:
            from ..mcp_client import McpBridge, load_mcp_config

            config = load_mcp_config()
            stdio = config.get("stdio_servers") or []
            urls = config.get("url_servers") or []
            if not stdio and not urls:
                return None, [], []
            bridge = McpBridge(self.console)
            tool_defs = bridge.start(stdio, urls)
            notes = []
            if tool_defs:
                notes.append(f"{len(tool_defs)} MCP tool(s) from {len(bridge.connected_server_names())} server(s)")
            return bridge, tool_defs, notes
        except Exception as exc:  # noqa: BLE001
            return None, [], [f"MCP unavailable: {type(exc).__name__}: {exc}"]

    def _attach_trace(self) -> None:
        """Give the agent's tool context a recorder so the trajectory tab has data.

        `tool_registry` already records every call through `ctx.bench_trace` when present, so this is
        the whole integration -- no agent changes and no parallel bookkeeping that could drift.
        """
        try:
            from ..bench.state import RunState, TraceRecorder
        except Exception:  # noqa: BLE001 -- trajectory is optional, never fatal
            return
        context = getattr(self.agent, "tool_context", None)
        if context is None:
            return
        self._trace_state = RunState(
            task_id=f"dashboard-{self.id}",
            attempt=1,
            model=getattr(self.agent, "model", "") or "",
            provider=self.config.provider,
            workspace=str(self.workspace),
            status="running",
        )
        inner = TraceRecorder(self._trace_state)
        # Wrapped so each call is streamed to the browser as it happens. Doing this here rather than
        # in tool_registry keeps the recording-of-record in one place and adds no agent coupling:
        # the wrapper behaves exactly like the real recorder from the registry's point of view.
        self._recorder = _StreamingRecorder(inner, self.bus)
        context.bench_trace = self._recorder

    def _portable_history(self) -> list[dict]:
        """The conversation in a form every provider agent accepts.

        Cross-provider transcripts are not interchangeable: the OpenAI-compatible agents store
        `{"role": "assistant", "tool_calls": [...]}`, while Gemini stores typed messages
        (`user_input`, `function_call`, `function_result`) and *requires* a thought_signature on
        replayed functionCall parts. Feeding one shape to the other either loses the tool exchange or
        is rejected outright.
        So a provider switch carries the conversational text -- user and assistant turns -- and drops
        the tool mechanics, which are re-derivable by the model and already visible in the
        Trajectory tab. Both `role`-based and typed messages normalize into that one shape.
        """
        portable = []
        for message in self.history:
            if not isinstance(message, dict):
                continue
            role = _role_of(message)
            if role not in ("user", "assistant"):
                continue
            text = _text_of(message)
            if text:
                portable.append({"role": role, "content": text})
        return portable

    def capture_history(self) -> None:
        """Snapshot the live agent's messages so they survive a rebuild."""
        agent = self.agent
        if agent is None:
            return
        messages = getattr(agent, "messages", None)
        if messages:
            self.history = list(messages)

    # -- running ------------------------------------------------------------------
    @property
    def busy(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def submit(self, text: str, *, model: str | None = None, mode: str | None = None) -> bool:
        """Start a turn. Returns False if one is already running."""
        with self._lock:
            if self.busy:
                return False
            if mode and mode != self.mode:
                self.mode = mode
                self.state.mode = mode
                # The mode is baked into the agent (it decides which tools are available), so a mode
                # change requires a rebuild rather than a flag flip.
                self.agent = None
            self._cancelled.clear()
            self._thread = threading.Thread(
                target=self._run_turn, args=(text, model), name=f"myriaxon-turn-{self.id}", daemon=True
            )
            self._thread.start()
            return True

    def _run_turn(self, text: str, model: str | None) -> None:
        self.state.busy = True
        self.state.error = None
        started = time.perf_counter()
        if self.state.title == "New session":
            self.state.title = text.strip().splitlines()[0][:60] or "New session"
            self.bus.publish("title", title=self.state.title)
        self.bus.publish("turn_start", text=text, mode=self.mode, model=model or self.config.model,
                         session=self.state.to_dict())

        try:
            agent = self._ensure_agent(model)
            self.state.provider = self.config.provider
            self.state.model = getattr(agent, "model", model or self.config.model)
            # The agent watches this between streamed chunks, so a cancel stops the tokens rather
            # than only relabelling the turn after it finishes.
            cancellation.attach(agent)
            self.bus.publish("status", text=f"{self.state.provider}:{self.state.model} ({self.mode})")
            # Clear per-turn streaming state so the previous turn's answer is not mistaken for this
            # one's (which would suppress this turn's final print entirely).
            self.console.begin_turn()
            agent.run_turn(text)
            self.state.turn_count += 1
        except cancellation.TurnCancelled as stop:
            # Cancellation is not a failure. It is recorded as its own outcome so the transcript
            # shows a stopped turn rather than an error, and the partial answer is kept -- the user
            # watched that text appear, and discarding it would be surprising.
            self._cancelled_note = stop.partial
            self.state.error = None
            self.bus.publish("notice", text="turn cancelled", severity="warning")
        except BaseException as exc:  # noqa: BLE001 -- reported to the UI, never swallowed
            self.state.error = f"{type(exc).__name__}: {exc}"
            # `exc_type`, not `kind`: `publish(kind, **payload)` already uses `kind` for the event
            # name, so passing `kind=` here raised
            #     TypeError: EventBus.publish() got multiple values for argument 'kind'
            # from inside the except block. The consequence was the opposite of the comment above --
            # the browser received no error event at all, so a failed turn left the spinner running
            # with no explanation, which is exactly what this handler exists to prevent.
            # The event's own `kind` field is "error", which is what the UI reads.
            self.bus.publish(
                "error",
                message=str(exc),
                exc_type=type(exc).__name__,
                detail=traceback.format_exc()[-2000:],
            )
        finally:
            try:
                self.console.flush_partial()
            except Exception:  # noqa: BLE001
                pass
            # If the provider produced no deltas at all, the browser never received the answer (its
            # single assistant bubble is only filled by deltas). Send the final text once so a
            # non-streaming provider still shows a reply -- and only once, which the UI guarantees by
            # reusing the same bubble for both the `answer` and `answer_delta` events.
            #
            # `self.state.error` is checked first, and that is the fix for a reported bug rather than
            # caution: on a failed turn `_final_assistant_text` walks the message list BACKWARDS for
            # the last assistant message, which is the PREVIOUS turn's reply. Measured in the browser
            # as an authentication failure rendered directly above "Hi! Anything else I can help you
            # with? 😊" -- a message the model had sent a minute earlier, presented as the answer to a
            # turn that never reached it. A turn that failed says so and says nothing else; nobody is
            # owed a prediction of what the model would have said.
            try:
                if self.state.error is None and not self.console.saw_answer:
                    final_text = _final_assistant_text(self.agent)
                    if final_text:
                        self.bus.publish("answer", text=final_text)
            except Exception:  # noqa: BLE001
                pass
            # Snapshot the transcript before anything can rebuild the agent, so a later provider or
            # model switch still has the conversation.
            try:
                self.capture_history()
            except Exception:  # noqa: BLE001
                pass
            elapsed = time.perf_counter() - started
            self.state.busy = False
            self.state.updated_at = time.time()
            # The usage snapshot is taken BEFORE the save, not after. `_usage_snapshot` is what writes
            # `state.usage`, so persisting first stored an all-zero block -- the file had the right
            # shape and no numbers, which is worse than having no block at all because it looks
            # authoritative. Measured: the live session reported 4,905 tokens while its own saved
            # file reported 0.
            usage = self._usage_snapshot()
            # Written to disk after the turn, not before: the transcript is only complete once the
            # turn has finished, and a save per chunk would write the same file dozens of times.
            # Failure is silent by design -- a disk problem is a lost transcript, not a failed turn.
            try:
                self._persist()
            except Exception:  # noqa: BLE001
                pass
            # A locus run started from the dashboard holds its lease for the duration of the turn, so
            # the release belongs here, on the one path every turn ends through. Released before the
            # event is published so a listener that re-reads the registry sees the locus as free.
            if getattr(self, "_dot_lease_name", ""):
                try:
                    app = getattr(self, "app", None)
                    if app is not None and hasattr(app, "release_locus_lease"):
                        app.release_locus_lease(self._locus_lease_name)
                except Exception:  # noqa: BLE001
                    pass
                self._locus_lease_name = ""
                self._locus_lease = None
            self.bus.publish(
                "turn_end",
                seconds=round(elapsed, 2),
                cancelled=self._cancelled.is_set(),
                session=self.state.to_dict(),
                usage=usage,
                trajectory=self.trajectory(),
            )

    def _persist(self) -> None:
        """Save this session through its manager, if it has one."""
        manager = getattr(self, "manager", None)
        if manager is not None:
            manager.save(self.id)
            return
        store.save(self.id, state=self.state, messages=self.messages(),
                   trajectory=self.trajectory())

    def _usage_snapshot(self) -> dict:
        totals = getattr(self.agent, "usage_totals", None)
        if totals is None:
            # No agent yet -- a restored session, or one that has only run slash commands. The
            # totals from before the restart are kept on the state, so the status line is not blank
            # for a conversation that plainly cost something.
            return dict(getattr(self.state, "usage", None) or {})
        # Every number the status line shows is computed here rather than in the browser, so the
        # page and any other reader of this event cannot disagree about what a session cost.
        cache_read = getattr(totals, "cache_read_tokens", 0)
        cache_write = getattr(totals, "cache_write_tokens", 0)
        cacheable = getattr(totals, "input_tokens", 0) + cache_read
        snapshot = {
            "input_tokens": getattr(totals, "input_tokens", 0),
            "output_tokens": getattr(totals, "output_tokens", 0),
            "request_count": getattr(totals, "request_count", 0),
            "total_tokens": getattr(totals, "total_tokens", 0)
            or (getattr(totals, "input_tokens", 0) + getattr(totals, "output_tokens", 0)),
            "cache_read_tokens": cache_read,
            "cache_write_tokens": cache_write,
            "cache_hit_rate": (cache_read / cacheable) if cacheable else 0.0,
            "steps": len(self.trajectory()),
            "turn_count": getattr(self.state, "turn_count", 0),
        }
        # Kept on the state so it survives a restart. The agent holding the live totals is rebuilt
        # empty, and a session's spending does not become zero because the process ended.
        self.state.usage = snapshot
        return snapshot

    def cancel(self) -> None:
        """Ask the running turn to stop.

        Reaches the agent's stream loop through its cancel token, so this stops tokens being
        consumed and closes the provider response -- it is not merely a flag the UI reads. The
        session-level event is still set, because it is what marks the turn as cancelled for the
        `turn_end` payload.
        """
        self._cancelled.set()
        agent = self.agent
        if agent is not None:
            token = getattr(agent, "cancel_token", None)
            if token is not None:
                token.request()

    # -- introspection ------------------------------------------------------------
    def trajectory(self) -> list[dict]:
        state = self._trace_state
        if state is None:
            # Restored from disk, so there is no live trace state: show what was saved rather than
            # an empty Trajectory tab for a session that visibly has history.
            return [dict(step) for step in getattr(self, "_restored_trajectory", [])]
        events = []
        for event in getattr(state, "events", []) or []:
            events.append(
                {
                    # Field names come straight from bench.state.TraceEvent -- `tool`/`args`, not
                    # `name`/`tool_input`; verified against the dataclass rather than assumed.
                    "tool": getattr(event, "tool", ""),
                    "args": getattr(event, "args", {}) or {},
                    "is_error": bool(getattr(event, "is_error", False)),
                    "result": _clip(getattr(event, "result", ""), 4000),
                    "duration_ms": getattr(event, "duration_ms", None),
                    "phase": getattr(event, "phase", ""),
                    "index": getattr(event, "index", None),
                }
            )
        return events

    def messages(self) -> list[dict]:
        """Conversation projected for the UI: [{role, text}, ...].

        Not `session_store.serialize_messages`: that preserves the on-disk provider-native shape,
        which differs per provider (OpenAI uses role/content dicts, Gemini uses typed parts like
        function_call/function_response). Verified against a live Gemini session, where the raw
        messages had no `role` key at all and the UI rendered nothing. Projecting here means the
        browser sees one stable shape regardless of provider.
        """
        agent = self.agent
        if agent is None:
            # No agent yet, which is the state right after a restore: the transcript read back from
            # disk is what the browser should see until a turn rebuilds an agent. Returning [] here
            # would show an empty conversation for a session whose history is sitting on disk.
            return [dict(m) for m in self.history]
        out: list[dict] = []
        for message in getattr(agent, "messages", []) or []:
            role = _role_of(message)
            text = _text_of(message)
            # The OpenAI-compatible agents keep the system prompt as the head of the message list.
            # That is transport, not conversation, and rendering it pushed the user's own first
            # message down the transcript. It remains visible in the Trajectory tab.
            if role == "system":
                continue
            # A message from the model that only calls a tool carries no prose -- measured: its
            # content is literally "\n\n". Rendering those produced blank assistant bubbles between
            # every tool call. Whitespace-only text is therefore treated as no text at all.
            if not text.strip():
                continue
            # Machine-written context is transport too. `model_adapt.append_runtime_context` adds a
            # one-off "<runtime_context>…</runtime_context>" message per state change, and a
            # background-job notice as another user-role message. Both are instructions TO the model,
            # not something the user said -- measured in a locus session's transcript, where the block
            # was rendered under a "You" label, telling the reader they had typed a list of settings.
            if role == "user" and _is_machine_context(text):
                continue
            out.append({"role": role, "text": text})
        return out

    def usage(self) -> dict:
        return self._usage_snapshot()

    def restore(self, saved: dict) -> None:
        """Reinstate a saved transcript, so a restart does not erase the conversation.

        Only the transcript and the descriptive state are restored. The agent is deliberately left
        unbuilt: it holds a provider client, a model and an MCP bridge, all of which must be
        constructed for *this* process, and an agent restored from disk would be a handle to
        connections that no longer exist.
        """
        state = saved.get("state") or {}
        restored: list[dict] = []
        for message in saved.get("messages") or []:
            if not isinstance(message, dict):
                continue
            role = str(message.get("role") or "")
            if role not in ("user", "assistant"):
                continue
            restored.append({"role": role, "text": str(message.get("text") or "")})
        self.history = restored

        if state.get("id"):
            self.state.id = str(state["id"])
        for key in ("title", "provider", "model", "mode", "locus"):
            value = state.get(key)
            if value:
                setattr(self.state, key, str(value))
        try:
            self.state.turn_count = int(state.get("turn_count") or 0)
        except (TypeError, ValueError):
            pass
        # The tool calls, so the Trajectory tab is not empty for a restored session. Kept in the
        # same shape `trajectory()` produces, and superseded the moment a new turn runs.
        self._restored_trajectory = [
            dict(step) for step in (saved.get("trajectory") or []) if isinstance(step, dict)
        ]


def _final_assistant_text(agent: Any) -> str:
    """The agent's last reply, or "" if it cannot be read. Never raises."""
    if agent is None:
        return ""
    for attribute in ("last_assistant_text",):
        getter = getattr(agent, attribute, None)
        if callable(getter):
            try:
                return getter() or ""
            except Exception:  # noqa: BLE001
                pass
    return ""


def _clip(value: Any, limit: int) -> Any:
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + f"\n... [truncated, {len(value)} chars total]"
    return value


# Gemini transcript part types that are mechanical rather than conversational.
_MECHANICAL_PARTS = {"function_call", "function_response", "thought", "executable_code", "code_execution_result"}

# Provider-native message types -> conversational role. Verified by dumping a live Gemini
# transcript: its messages are `{"type": "user_input", "content": "..."}` and
# `{"type": "model_output", "content": [{"type": "text", "text": "..."}]}`, with NO `role` key at
# all. Assuming an OpenAI-style `role` field made every message in the UI render as the assistant,
# including the user's own prompt.
_TYPE_TO_ROLE = {
    "user_input": "user",
    "user": "user",
    "model_output": "assistant",
    "assistant": "assistant",
    "model": "assistant",
    "function_call": "tool",
    "function_response": "tool",
    "tool": "tool",
    "tool_result": "tool",
    "tool_call": "tool",
}


def _is_machine_context(text: str) -> bool:
    """Whether a user-role message was written by the harness rather than by the user.

    Two shapes exist today, both added to the model's message list as `role: "user"` because that is
    the only role the wire protocol offers for "here is more information":

    * the volatile runtime snapshot from `model_adapt.append_runtime_context`, wrapped in
      `<runtime_context>` tags;
    * a `[background job finished]` notice from the jobs service.

    Both belong in the model's context and neither belongs in a transcript a person reads, where they
    appear as something the user typed. Recognised by shape rather than by identity: a real message
    that happened to contain these markers would be excluded too, which is the acceptable direction --
    this filter can only ever hide harness text, never user text, or the two are indistinguishable
    and the transcript lies about who said what.
    """
    stripped = text.strip()
    if not stripped:
        return True
    if stripped.startswith("<runtime_context") or stripped.startswith("[runtime context]"):
        return True
    return stripped.startswith("[background job finished]")


def _role_of(message: Any) -> str:
    """Normalize a provider-native message to user/assistant/tool."""
    if not isinstance(message, dict):
        return "assistant"
    # `type` is checked first: it is present and authoritative on the Gemini path, and on the OpenAI
    # path `role` is. Checking `role` first would misclassify nothing, but checking `type` first keeps
    # the two paths symmetric.
    message_type = str(message.get("type") or "").lower()
    if message_type in _TYPE_TO_ROLE:
        return _TYPE_TO_ROLE[message_type]
    role = message.get("role")
    if role:
        return {"model": "assistant", "tool": "tool"}.get(str(role).lower(), str(role).lower())
    return "assistant"


def _text_of(message: Any) -> str:
    """Extract the human-readable text from any provider's message shape."""
    if not isinstance(message, dict):
        return str(message)
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                block_type = block.get("type")
                if block_type in _MECHANICAL_PARTS:
                    continue
                if block_type == "text" or "text" in block:
                    parts.append(block.get("text") or "")
                elif block_type == "image_url":
                    parts.append("[image]")
        return "".join(parts)
    # Gemini Content objects, where `parts` holds typed Part objects.
    parts_attr = getattr(message, "parts", None)
    if parts_attr:
        collected = []
        for part in parts_attr:
            if getattr(part, "function_call", None) is not None:
                continue
            text = getattr(part, "text", None)
            if text:
                collected.append(text)
        return "".join(collected)
    message_text = message.get("text")
    if isinstance(message_text, str):
        return message_text
    return ""


class _StreamingRecorder:
    """Delegates to TraceRecorder and also publishes each call to the event bus.

    A thin proxy rather than a subclass, so the bench harness can keep changing its recorder without
    this needing to track its internals. `tool_registry` only calls `begin`, `abort` and `record`, so
    those are the three that matter; everything else is forwarded untouched.
    """

    def __init__(self, inner: Any, bus: EventBus) -> None:
        self._inner = inner
        self._bus = bus
        self._started: dict[int, float] = {}

    def begin(self) -> int:
        index = self._inner.begin()
        self._started[index] = time.perf_counter()
        return index

    def abort(self, index: int) -> None:
        self._started.pop(index, None)
        self._inner.abort(index)

    def record(self, name: str, tool_input: dict, is_error: bool, result: str) -> None:
        # `begin()` returns the index that `record()` will use, and the real recorder tracks it as
        # `_counter`; reading it here keeps the two in step without recomputing anything. If the
        # registry ever calls record() without begin(), there is simply no timing to report rather
        # than a wrong one.
        index = getattr(self._inner, "_counter", None)
        started = self._started.pop(index, None) if index is not None else None
        duration_ms = (time.perf_counter() - started) * 1000 if started else None
        try:
            self._inner.record(name, tool_input, is_error, result)
        finally:
            self._bus.publish(
                "tool_call",
                tool=name,
                args=tool_input,
                is_error=bool(is_error),
                result=_clip(result, 4000),
                duration_ms=round(duration_ms, 1) if duration_ms is not None else None,
            )

    def __getattr__(self, item: str) -> Any:
        return getattr(self._inner, item)


class SessionManager:
    """Owns sessions and their worker threads."""

    def __init__(self, workspace: Path, settings: Any = None, endpoints: Any = None,
                 application: Any = None) -> None:
        self.workspace = workspace
        self.settings = settings
        self.endpoints = endpoints
        # The owning application, handed to each session so a slash command can reach process-wide
        # state. `/mission` renders the same snapshot the dashboard view does, and that snapshot
        # reads the application -- not the session.
        self.application = application
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()

    def create(self, config: Any, *, mode: str = "auto_edit", session_id: str | None = None) -> Session:
        session_id = session_id or uuid.uuid4().hex[:12]
        session = Session(
            session_id, self.workspace, config=config, mode=mode, settings=self.settings,
            endpoints=self.endpoints,
        )
        # So the session can persist itself after a turn without the manager having to be threaded
        # through every call site that runs one.
        session.manager = self
        # The application, so a command can reach process-wide state (the mission snapshot reads it).
        session.app = self.application
        # A session created under an id that has a saved transcript continues that conversation
        # instead of starting an empty one. This is what makes a locus's memory survive a restart --
        # recreating the session kept the identity but not the history.
        saved = store.load(session_id)
        if saved:
            session.restore(saved)
        with self._lock:
            self._sessions[session_id] = session
        return session

    def restore_all(self, config: Any) -> int:
        """Bring back every readable session. Returns how many were restored.

        Called once at startup, so a browser reload after a restart finds the conversations it had
        rather than an empty list. Bounded by `store.load_all`, because an unbounded restore would
        make startup slower every day the tool is used.
        """
        restored = 0
        for saved in store.load_all():
            session_id = str((saved.get("state") or {}).get("id") or "")
            if not session_id or self.get(session_id) is not None:
                continue
            try:
                self.create(config, mode=str((saved.get("state") or {}).get("mode") or "auto_edit"),
                            session_id=session_id)
                restored += 1
            except Exception:  # noqa: BLE001 - one bad file must not stop the others
                continue
        return restored

    def save(self, session_id: str) -> None:
        """Persist one session's transcript. Never raises; a failed save is a lost transcript."""
        session = self.get(session_id)
        if session is None:
            return
        try:
            trajectory = session.trajectory()
        except Exception:  # noqa: BLE001
            trajectory = []
        store.save(session_id, state=session.state, messages=session.messages(),
                   trajectory=trajectory)

    def get(self, session_id: str) -> Session | None:
        with self._lock:
            return self._sessions.get(session_id)

    def list(self) -> list[dict]:
        with self._lock:
            sessions = list(self._sessions.values())
        return [s.state.to_dict() for s in sorted(sessions, key=lambda s: s.state.updated_at, reverse=True)]

    def delete(self, session_id: str) -> bool:
        with self._lock:
            existed = self._sessions.pop(session_id, None) is not None
        # The file goes too, or a deleted conversation reappears on the next restart -- which looks
        # like the delete button failing.
        store.delete(session_id)
        return existed

    def any_busy(self) -> bool:
        with self._lock:
            return any(s.busy for s in self._sessions.values())
