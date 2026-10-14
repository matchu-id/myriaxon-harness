import base64
import binascii
import functools
import json
import os
import time
import warnings

from rich.console import Console
from rich.live import Live
from rich.markup import escape
# NOTE: `rich.markdown` is deliberately NOT imported at module scope. It pulls in markdown_it,
# measured at ~2.5s of import time on this machine, and every provider agent (and therefore the
# whole CLI) imports this module at startup. Markdown rendering only happens when a final answer is
# displayed, so the import is deferred to that moment -- see render_markdown() in theme.py.

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
    PROVIDER_GEMINI,
    STALE_TOOL_RESULT_MAX_AGE_TURNS,
    STALE_TOOL_RESULT_MIN_CHARS,
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



class GeminiStreamError(Exception):
    """A mid-stream `error` event from the Interactions API (e.g. quota/auth failures arrive
    this way, not as a raised SDK exception, since the request itself was accepted with 200).
    Carries .code/.message so cli.py's existing _handle_gemini_error can treat it the same as
    genai_errors.APIError."""

    def __init__(self, code: str | None, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


class _PendingFunctionCall:
    def __init__(self) -> None:
        self.id: str | None = None
        self.name: str | None = None
        self.arguments_fragments: list[str] = []

    def arguments_text(self) -> str:
        return "".join(self.arguments_fragments)


class _GenerateResult:
    """One generateContent response, normalized: the parts the agent loop actually needs."""

    __slots__ = ("text", "calls", "usage")

    def __init__(self, text: str, calls: list, usage) -> None:
        self.text = text
        self.calls = calls
        self.usage = usage


def _resolve_protocol(model: str, requested: str) -> str:
    """Decide between `generate` and `interactions` for a model id.

    **generateContent is the default for every model.** Measured on Vertex with gemini-3.8-flash:

    | surface | result |
    |---|---|
    | `generateContent` (streaming) | time-to-first-token 2.26s, total 2.54s |
    | `interactions` | one call 18s, another hung for 252s and returned HTTP 500 |

    The earlier rule routed gemini-3.x to `interactions` because that is the newer API -- but newer
    is not the same as working, and the practical effect was that the requested model was unusable.
    generateContent is universally available across both API lines, and it is the one measured to be
    fast and reliable here.

    `interactions` remains available for a model that rejects generateContent (the error names
    itself clearly: "Unsupported model interaction"), either via the per-instance fallback in
    `_run_turn_generate` or explicitly with `--gemini-protocol interactions` / `MYRIAXON_GEMINI_PROTOCOL`.
    """
    if requested in ("generate", "interactions"):
        return requested
    return "generate"


def _parse_data_url(url: str) -> tuple[str, str] | None:
    """Split a `data:<mime>;base64,<payload>` URL into (mime_type, base64_payload).

    `attachments.build_attachment_block` emits this shape for non-Anthropic providers, and the
    Gemini SDK wants the mime type and the raw base64 separately in an inline_data part, so the URL
    has to be taken apart rather than passed through.
    """
    if not isinstance(url, str) or not url.startswith("data:"):
        return None
    header, _, payload = url.partition(",")
    if not payload:
        return None
    mime = header[5:].split(";", 1)[0] or "application/octet-stream"
    return mime, payload


def _attachment_parts(blocks: list, types) -> list:
    """Convert stored attachment blocks into Gemini parts.

    Handles the image / PDF / file variants that `build_attachment_block` can produce, so an
    attachment that works with the other providers also works here.
    """
    parts = []
    for block in blocks or []:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        url = None
        mime = None
        if kind == "image_url":
            url = (block.get("image_url") or {}).get("url")
        elif kind == "file":
            url = (block.get("file") or {}).get("file_data")
        elif kind == "document":
            source = block.get("source") or {}
            url = f"data:{source.get('media_type', 'application/pdf')};base64,{source.get('data', '')}"
        elif kind == "image":
            source = block.get("source") or {}
            url = f"data:{source.get('media_type', 'image/png')};base64,{source.get('data', '')}"

        parsed = _parse_data_url(url) if url else None
        if not parsed:
            continue
        mime, payload = parsed
        try:
            parts.append(types.Part.from_bytes(data=base64.b64decode(payload), mime_type=mime))
        except (binascii.Error, ValueError):
            continue
    return parts


def _to_gemini_contents(messages: list[dict]) -> list:
    """Translate the stored conversation into native `types.Content` for the generateContent API.

    The stored format is deliberately left as plain dicts (so `session_store` keeps working unchanged
    across all three providers); this is the one place that format is converted for transport.
    Function calls and their responses become their own Content turns, which is what the
    generateContent protocol requires -- unlike the Interactions API, where they are sibling steps.

    `user_input` messages may carry an `attachments` list of provider-neutral blocks (what
    `build_attachment_block` returns); those become inline_data parts, which is how images and PDFs
    reach Gemini.
    """
    from google.genai import types

    contents = []
    for message in messages or []:
        kind = message.get("type")
        if kind == "user_input":
            parts = [types.Part.from_text(text=message.get("content") or "")]
            parts.extend(_attachment_parts(message.get("attachments") or [], types))
            contents.append(types.Content(role="user", parts=parts))
        elif kind == "function_call":
            # Gemini 3.x requires the thought_signature on the model's own functionCall part when
            # the history is sent back. Measured: the API rejects the follow-up request with
            # "Function call is missing a thought_signature in functionCall parts ... position 2"
            # and names the functionCall, not the response -- so the signature belongs here.
            signature = message.get("thought_signature")
            part = types.Part(
                function_call=types.FunctionCall(name=message.get("name", ""), args=message.get("arguments") or {}),
                thought_signature=signature or None,
            )
            contents.append(types.Content(role="model", parts=[part]))
        elif kind == "function_result":
            payload = message.get("result", "")
            part = types.Part(
                function_response=types.FunctionResponse(name=message.get("name", ""), response={"result": payload})
            )
            contents.append(types.Content(role="user", parts=[part]))
        elif kind == "model_output":
            parts = []
            for block in message.get("content") or []:
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(types.Part.from_text(text=block.get("text") or ""))
            if parts:
                contents.append(types.Content(role="model", parts=parts))
        elif message.get("role") and message.get("content") is not None:
            # Tolerate a transcript resumed from the other providers' shape rather than dropping it.
            role = "user" if message["role"] == "user" else "model"
            parts = [types.Part.from_text(text=str(message["content"]))]
            if isinstance(message.get("content"), list):
                parts = [types.Part.from_text(text=block.get("text", "")) for block in message["content"]
                         if isinstance(block, dict) and block.get("type") == "text"] or parts
            parts.extend(_attachment_parts(message.get("attachments") or [], types))
            contents.append(types.Content(role=role, parts=parts))
    return contents


def _to_gemini_tools(tools: list[dict]):
    """Convert this project's tool defs into a generateContent `types.Tool`.

    The registry emits either the Anthropic shape ({name, description, input_schema}) or, for the
    gemini provider, an already-converted shape. Both are accepted here so callers cannot get it
    wrong by picking a different builder.
    """
    from google.genai import types

    declarations = []
    for tool in tools or []:
        name = tool.get("name") or (tool.get("function") or {}).get("name")
        if not name:
            continue
        schema = tool.get("input_schema") or tool.get("parameters") or (
            (tool.get("function") or {}).get("parameters")
        ) or {"type": "object", "properties": {}}
        description = tool.get("description") or (tool.get("function") or {}).get("description") or ""
        declarations.append(types.FunctionDeclaration(name=name, description=description, parameters=schema))
    return [types.Tool(function_declarations=declarations)] if declarations else []


class GeminiAgent:
    """Mirrors Agent's external contract but speaks one of two Google protocols.

    * ``generateContent`` (``protocol="generate"``) -- the long-standing, universally available
      Gemini API. This is what Vertex AI serves for `gemini-2.5-*`, and it is the only path that
      works against a corporate/Workforce-Identity GCP account.
    * ``interactions`` (``protocol="interactions"``) -- Google's newer step/event protocol, which is
      what the direct Gemini API serves for the `gemini-3.x` line.

    Both are supported rather than one replacing the other: the model a user has access to decides
    which is available, and forcing the newer protocol made the entire Vertex path unusable (measured:
    a Vertex `gemini-2.5-flash` request rejected with "Unsupported model interaction").
    """

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
        settings=None,
        fallback_model: str = "",
        profile=None,
        protocol: str = "auto",
        warmup: bool = True,
        vertex_project: str = "",
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
        # Remote MCP connector and web_search/web_fetch are Anthropic-only; not meaningful here.
        self.url_mcp_servers: list[dict] = []
        self.effort = effort
        self.thinking = thinking
        # Whether this agent is talking to Vertex, which decides if a `cached_content` reference is even
        # meaningful. The direct Gemini API has no project and no explicit cache, so a cache lookup there
        # would be a wasted network call per turn.
        self.vertex_project = str(vertex_project or "")
        if effort or thinking:
            self.console.print(
                "[warning]Note: --effort/--thinking aren't wired up for the gemini provider yet "
                "(unverified parameter shape) -- accepted but currently have no effect.[/]"
            )
        self.max_input_tokens = max_input_tokens
        self.mode = mode
        self.subagent_types = subagent_types or {}
        self.hooks = hooks or {}
        self.project_instructions = project_instructions
        self.scheduler = scheduler
        self.settings = settings
        self.fallback_model = fallback_model
        self.usage_totals = UsageTotals()
        # Mid-turn corrections, delivered at the next step boundary by `model_adapt.deliver_steers`.
        #
        # Present on all three providers so the mechanism cannot exist on one and be silently absent on another --
        # the failure mode `model_adapt` exists to prevent for runtime context and result formatting.
        self.steer_queue = steering.SteerQueue()
        # The sources of the steers delivered in the most recent step, for a status line and for tests.
        self.last_steer_sources: list[str] = []
        # Model profile (see model_profiles.py); None disables adaptation.
        self.profile = profile
        # Which Google protocol to speak. "auto" resolves from the model id: the gemini-3.x line
        # serves the newer Interactions API, everything else (notably every Vertex gemini-2.5 model)
        # serves generateContent. Getting this wrong is not a subtle degradation -- the request is
        # rejected outright -- so the resolution is recorded on the instance for the run manifest.
        self.protocol = _resolve_protocol(model, protocol)
        self._protocol_switch_used = False
        self.request_count = 0
        self._warmed = False

        if tools is not None:
            self.tools = tools
        else:
            self.tools = tool_registry.build_tool_defs_gemini(mcp_tool_defs=mcp_tool_defs)
        # Gemini's tool defs are already converted to its native shape, so the filter works on the
        # `name` key here rather than the nested function shape.
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
            GeminiAgent,
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
            settings=self.settings,
            fallback_model=self.fallback_model,
            profile=self.profile,
        )

    def _system_prompt(self) -> str:
        """Profile-tiered prompt, shared with the other two providers."""
        from .system_prompt import build_system_prompt

        return build_system_prompt(self)

    def warm_up(self) -> None:
        """Issue one throwaway request to pay the provider's cold start at a chosen moment.

        Measured on Vertex with gemini-3.8-flash: the first request in a fresh process costs ~30-36s
        and every subsequent one ~1.8s. It is provider-side model instance start-up, not our code.

        Measured effect of hiding it in a background thread: ~2s (33.0s vs 36.3s) -- inside the
        noise, because the provider serialises the two requests rather than serving them
        concurrently. So the async variant was removed as unearned complexity. The synchronous
        method is kept for callers that genuinely can schedule it usefully (a REPL that starts the
        warm-up and then waits for the user's first keystroke gains ~9s by a 5s head start,
        reproduced in scripts/measure_warmup.py).
        """
        try:
            for _chunk in self.client.models.generate_content_stream(model=self.model, contents="hi"):
                break
        except Exception:  # noqa: BLE001 -- warm-up is best-effort by definition
            pass

    def _prepare_context(self) -> None:
        """Same graduated context response as the other providers."""
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
            self._context_notice_shown = state.action != "ok"

    def _legacy_system_prompt(self) -> str:
        from . import skills as skills_module

        parts = [
            f"You are {APP_NAME}, a terminal-based coding agent.",
            f"Current working directory: {os.getcwd()}",
        ]
        if self.project_instructions:
            parts.append(f"Project/user instructions:\n{self.project_instructions}")
        parts += [
            "The run_bash tool executes commands via Windows PowerShell 5.1, not POSIX bash — "
            "write commands accordingly (PowerShell cmdlets, not GNU coreutils flags).",
            "write_file, edit_file, run_bash, and edit_notebook all require the user's "
            "interactive confirmation before they take effect; a decline is a normal outcome, "
            "not an error — acknowledge it and adjust your approach rather than repeating the "
            "same call. In Plan mode they are blocked outright -- describe your plan in text "
            "instead of calling them.",
            "Use todo_write to maintain a visible checklist for multi-step work, and use task to "
            "delegate a self-contained, open-ended sub-problem to a sub-agent when it would "
            "otherwise consume a lot of your own context.",
            "Live web search grounding (google_search) is available -- use it for current "
            "events, recent releases, or other facts that might be outside your training data, "
            "rather than asking the user to search manually.",
            "If a task needs a Python package/library you don't have, install it yourself via "
            "run_bash (e.g. pip install <package>) and use it properly, rather than working "
            "around the missing capability by degrading the task -- e.g. don't flatten a .docx "
            "into plain text if you lack a docx library; install python-docx and edit it properly.",
            "If you discover a genuinely reusable approach (not a one-off trivial action), "
            "consider calling save_skill to preserve it for future sessions -- most turns won't "
            "warrant this, and that's expected.",
            "Available skills (call use_skill with the exact name to load one's full "
            f"instructions):\n{skills_module.format_skill_listing(self.skills)}",
            "Available subagent types (pass as subagent_type to task):\n"
            f"{subagents.format_subagent_type_listing(self.subagent_types)}",
        ]
        return "\n".join(parts)

    def last_assistant_text(self) -> str:
        for message in reversed(self.messages):
            if message.get("type") == "model_output":
                texts = [
                    item.get("text")
                    for item in message.get("content") or []
                    if isinstance(item, dict) and item.get("type") == "text"
                ]
                return "\n".join(t for t in texts if t)
        return ""

    def run_turn(self, user_input: str) -> None:
        """One turn, with one automatic protocol switch if the API surface rejects the model.

        A wrong protocol guess is not a subtle degradation -- the request is refused outright with
        "Unsupported model interaction". Rather than leaving the user to diagnose that, the turn is
        retried exactly once on the other surface. Bounded to one retry and only on this specific
        error, so a genuine API failure is never masked by an endless protocol ping-pong.
        """
        try:
            return self._run_turn_dispatch(user_input)
        except Exception as exc:  # noqa: BLE001 -- inspected, then re-raised unless it is the switch case
            if self._protocol_switch_used or "unsupported model interaction" not in str(exc).lower():
                raise
            other = "interactions" if self.protocol == "generate" else "generate"
            self.console.print(
                f"[warning]The model rejected the '{self.protocol}' API "
                f"({escape(str(exc)[:80])}) -- retrying once over '{other}'.[/]"
            )
            self._protocol_switch_used = True
            self.protocol = other
            # The partial turn is discarded before retrying: the first attempt may already have
            # appended a user_input step, and replaying it would duplicate the request.
            return self._run_turn_dispatch(user_input)

    def _run_turn_dispatch(self, user_input: str) -> None:
        if self.protocol == "generate":
            return self._run_turn_generate(user_input)
        return self._run_turn_interactions(user_input)

    def _run_turn_generate(self, user_input: str) -> None:
        """One turn over the generateContent API.

        Stateless per request: the whole translated transcript is sent every time, which is what
        makes this path trivially resumable and keeps the stored format identical to the other two
        providers. Tool calls are executed in order and their results appended as function_response
        parts before the next request, mirroring the other agents' loop shape.
        """
        from google.genai import types

        snapshot_len = len(self.messages)
        cleaned_text, attachment_paths = attachments.parse_attachments(user_input)
        cleaned_text = hooks.run_user_prompt_submit(cleaned_text, self.tool_context)
        attachment_blocks = self._load_attachment_blocks(attachment_paths)
        self.messages.append(
            {"type": "user_input", "content": cleaned_text, "attachments": attachment_blocks}
        )
        # Volatile per-step facts are APPENDED rather than written into the system prompt: the prompt is the head
        # of the request, so one changed character invalidates the provider's cached prefix for the whole
        # conversation. `append_if_changed` keeps this to one small message per state change.
        model_adapt.append_runtime_context(self)

        tools = _to_gemini_tools(self.tools)
        config_kwargs: dict = {"max_output_tokens": self.max_tokens}
        if tools:
            config_kwargs["tools"] = tools
        if self.messages and self.messages[0].get("role") == "system":
            self.messages.pop(0)  # legacy shape guard; system goes in system_instruction
        config_kwargs["system_instruction"] = self._system_prompt()

        try:
            while True:
                # Steers are delivered HERE, at the step boundary, before the next request. Anywhere else would
                # split a `function_call` from its `function_response` and corrupt the transcript -- the same reason
                # the budget is checked at the boundary. See `model_adapt.deliver_steers`.
                model_adapt.deliver_steers(self)
                self._prepare_context()
                self.request_count += 1
                _req_start = time.perf_counter()
                result = self._generate_stream(types, config_kwargs)
                if os.environ.get("MYRIAXON_TIMING", "").strip() in ("1", "true", "yes"):
                    import sys as _sys

                    _sys.stderr.write(
                        f"    [gemini] request {self.request_count}: {time.perf_counter() - _req_start:6.2f}s "
                        f"text={len(result.text)}ch calls={len(result.calls)}\n"
                    )

                if result.usage is not None:
                    self.usage_totals.add(
                        getattr(result.usage, "prompt_token_count", None),
                        getattr(result.usage, "candidates_token_count", None),
                    )

                if result.text:
                    self.messages.append({"type": "model_output", "content": [{"type": "text", "text": result.text}]})

                if not result.calls:
                    return

                for call in result.calls:
                    name, args = call["name"], call["args"]
                    self.messages.append(
                        {
                            "type": "function_call",
                            "name": name,
                            "arguments": args,
                            # Required back on this part by Gemini 3.x (see _to_gemini_contents).
                            "thought_signature": call.get("thought_signature"),
                        }
                    )
                    content_str, is_error = tool_registry.dispatch_tool(name, args, self.tool_context)
                    content_str, _truncated = model_adapt.format_result_for_model(content_str, self.tool_context)
                    if is_error:
                        content_str = f"Error: {content_str}"
                    self.messages.append(
                        {
                            "type": "function_result",
                            "name": name,
                            "result": content_str,
                            "is_error": is_error,
                        }
                    )
        except BaseException:
            # Same rollback contract as the other providers: never leave a function_call without its
            # result, or every later request and session resume fails.
            del self.messages[snapshot_len:]
            raise
        finally:
            self._maybe_warn_context_usage()
            self._save_session()

    def _matching_cache(self) -> str:
        """The name of a live cache whose prefix matches this request, or "".

        Returns "" for every reason that is not "a usable cache exists": switched off, not Vertex, none
        created, expired, or the prefix changed. All of those must produce an ordinary uncached request
        rather than an error -- an optimization that can fail a turn is worse than no optimization.

        Any exception also yields "". The lookup is a network call to an optional service, and a turn must
        not break because a convenience feature could not reach it.
        """
        try:
            from . import vertex_cache

            if not vertex_cache.enabled() or not self._is_vertex:
                return ""
            declarations: list = []
            for tool in (_to_gemini_tools(self.tools) or []):
                for declaration in (getattr(tool, "function_declarations", None) or []):
                    declarations.append(
                        declaration.model_dump() if hasattr(declaration, "model_dump") else declaration
                    )
            signature = vertex_cache._signature(self._system_prompt(), declarations)
            match = vertex_cache.find_matching(self.model, signature)
            return match.name if match else ""
        except Exception:  # noqa: BLE001 - an optional optimization must never fail a turn
            return ""

    def _note_cache_usage(self, usage) -> None:
        """Report what the API said about the cache, so the panel shows measured hits, not intentions."""
        try:
            from . import vertex_cache
            from .usage import cache_fields

            if not vertex_cache.enabled():
                return
            read, _ = cache_fields(usage)
            vertex_cache.note_usage(read)
        except Exception:  # noqa: BLE001
            pass

    @property
    def _is_vertex(self) -> bool:
        """Whether this client talks to Vertex rather than the direct Gemini API.

        A project is the authoritative signal: the direct API has none, and a `cached_content` reference
        is only meaningful on Vertex. Cached because it is consulted once per turn and the answer cannot
        change for the life of the agent.
        """
        known = getattr(self, "_vertex_flag", None)
        if known is not None:
            return bool(known)
        flag = False
        try:
            flag = bool(getattr(self, "vertex_project", ""))
        except Exception:  # noqa: BLE001
            flag = False
        self._vertex_flag = flag
        return flag

    def _generate_stream(self, types, config_kwargs: dict):
        """Stream one response, showing text as it arrives and returning text + tool calls.

        generateContent yields *chunks*, so text and function calls are accumulated here rather than
        read off a single response object. The rendered text is shown incrementally (matching the
        other two providers' live display) and also returned, because the caller needs it to build
        the stored transcript.

        `thought_signature` is captured alongside each call. Gemini 3.x requires it to be sent back
        with the function response -- without it the next request fails with
        "Function call is missing a thought_signature in functionCall parts", which aborts a tool
        turn after the model has already acted. Captured here (not stored in the transcript) because
        it is request-scoped data tied to this turn's model output.
        """
        text_parts: list[str] = []
        calls: list[dict] = []
        usage = None
        first_token = True
        # transient=True so the spinner erases itself. The context manager is allowed to own the
        # display lifecycle and is exited exactly once -- an earlier version also called
        # live.stop() from inside the block on the first token, which left Rich's internal state
        # inconsistent and corrupted the terminal: the REPL prompt was then drawn underneath the
        # leftover spinner and never became visible, so the user had to press interrupt before every
        # single message (measured live). Anything that stops the spinner early must go through the
        # Live object's own update() instead.
        with Live(console=self.console, refresh_per_second=12, transient=True) as live:
            live.update(ThreeColorSpinner())
            # Explicit context caching, when it is switched on AND a cache matches what this request
            # would send.
            #
            # Both halves matter. Passing a `cached_content` name whose prefix no longer matches is
            # WORSE than passing nothing: the request misses, the whole prompt is re-sent at full price,
            # and there is an extra round trip on top. So the cache is chosen by comparing a fingerprint
            # of the exact system prompt and tool catalogue this turn will send -- not by remembering
            # that a cache was created at some point.
            cached_content = self._matching_cache()
            if cached_content:
                config_kwargs["cached_content"] = cached_content

            for chunk in self.client.models.generate_content_stream(
                model=self.model,
                contents=_to_gemini_contents(self.messages),
                config=types.GenerateContentConfig(**config_kwargs),
            ):
                # Same contract as the other providers: checked per chunk so a cancel stops the
                # tokens rather than only relabelling the turn afterwards.
                #
                # `"".join(text_parts)`, not `text_buffer`. This function accumulates into
                # `text_parts`; `text_buffer` is the name the *interactions* stream uses, and
                # referring to it here raised NameError on the first streamed chunk. The dashboard
                # attaches a cancel token, so every Gemini turn through the web UI died with a
                # message about an undefined variable, while the CLI looked fine because nothing
                # else attaches a token.
                from . import cancellation

                _token = getattr(self, "cancel_token", None)
                if _token is not None:
                    _token.raise_if_cancelled("".join(text_parts))
                if getattr(chunk, "usage_metadata", None) is not None:
                    usage = chunk.usage_metadata
                    # Reported as the stream reports it, so the cache panel counts what the API actually
                    # said rather than what this agent intended. A request that carried a cache name and
                    # matched nothing reports 0 here and is counted as a MISS -- which is the number that
                    # reveals a cache which stopped matching.
                    self._note_cache_usage(usage)
                for candidate in getattr(chunk, "candidates", None) or []:
                    for part in getattr(candidate.content, "parts", None) or []:
                        if getattr(part, "text", None):
                            if first_token:
                                # Blank the spinner so the incoming text does not interleave with it,
                                # without tearing the Live display down.
                                live.update("")
                                first_token = False
                            text_parts.append(part.text)
                            live.update(render_markdown("".join(text_parts)))
                            _stream_answer_to_console(self.console, "".join(text_parts))
                        call = getattr(part, "function_call", None)
                        if call is not None:
                            calls.append(
                                {
                                    "name": call.name,
                                    "args": dict(call.args or {}),
                                    "thought_signature": getattr(part, "thought_signature", None),
                                }
                            )
        # Print the finished text on a clean terminal. Doing this AFTER the Live block is what
        # guarantees the next prompt starts on its own line.
        if text_parts:
            self.console.print(render_markdown("".join(text_parts)))
        return _GenerateResult("".join(text_parts), calls, usage)

    def _load_attachment_blocks(self, paths: list) -> list:
        """Turn @-referenced paths into provider-neutral attachment blocks.

        Uses the shared `attachments.build_attachment_block` so the same file types work here as with
        the other providers. A file that cannot be attached is reported rather than silently dropped,
        because a model answering "I don't see any image" with no explanation is worse than an error.
        """
        blocks = []
        for path in paths or []:
            block, error = attachments.build_attachment_block(path, PROVIDER_GEMINI)
            if error:
                self.console.print(f"[warning]{escape(error)}[/]")
                continue
            if block is not None:
                blocks.append(block)
                self.console.print(f"[dim]attached {escape(path.name)}[/]")
        return blocks

    def _run_turn_interactions(self, user_input: str) -> None:
        snapshot_len = len(self.messages)
        cleaned_text, attachment_paths = attachments.parse_attachments(user_input)
        cleaned_text = hooks.run_user_prompt_submit(cleaned_text, self.tool_context)
        attachment_blocks = self._load_attachment_blocks(attachment_paths)
        self.messages.append(
            {"type": "user_input", "content": cleaned_text, "attachments": attachment_blocks}
        )

        try:
            while True:
                self._prepare_context()
                content_text, function_calls, incomplete = self._stream_response()
                if incomplete:
                    self.console.print("[warning]Response truncated at the model's max output length.[/]")

                # ModelOutputStep.content only accepts text/image/audio/document/video --
                # function calls are their own sibling FunctionCallStep entries in the input
                # list, not nested inside the model_output step's content.
                if content_text:
                    self.messages.append(
                        {"type": "model_output", "content": [{"type": "text", "text": content_text}]}
                    )

                parsed_calls: list[tuple[_PendingFunctionCall, dict]] = []
                for call in function_calls:
                    try:
                        parsed_input = json.loads(call.arguments_text() or "{}")
                    except json.JSONDecodeError:
                        parsed_input = {}
                    parsed_calls.append((call, parsed_input))
                    self.messages.append(
                        {"type": "function_call", "id": call.id, "name": call.name, "arguments": parsed_input}
                    )

                if not function_calls:
                    return

                for call, parsed_input in parsed_calls:
                    content_str, is_error = tool_registry.dispatch_tool(call.name, parsed_input, self.tool_context)
                    # Profile-aware sizing, same boundary as the other two providers.
                    content_str, _truncated = model_adapt.format_result_for_model(content_str, self.tool_context)
                    self.messages.append(
                        {
                            "type": "function_result",
                            "call_id": call.id,
                            "name": call.name,
                            "result": content_str,
                            "is_error": is_error,
                        }
                    )
        except BaseException:
            # Roll back unconditionally, including on KeyboardInterrupt -- confirmed live this
            # matters: an interrupt (or any other failure) landing between "assistant calls a
            # tool" and "tool result appended" leaves a structurally invalid message list (a
            # function_call with no matching function_result), which the API then rejects on
            # every future turn or session resume. That's strictly worse than losing the partial
            # turn, so KeyboardInterrupt is no longer treated as a safe-to-preserve special case
            # -- and without the rollback, finally's _save_session() below would persist the
            # broken state to disk, so the same error would recur on every later turn or session
            # resume (real observed failure mode, e.g. a Gemini 400 mid-turn).
            del self.messages[snapshot_len:]
            raise
        finally:
            self._maybe_warn_context_usage()
            self._save_session()

    def _save_session(self) -> None:
        session_store.save_session(
            PROVIDER_GEMINI, self.model, self.mode, self.effort, self.thinking, self.messages
        )
        memory_store.record_session(
            os.getcwd(), PROVIDER_GEMINI, self.model, render_transcript(self.messages, PROVIDER_GEMINI)
        )

    def compact(self, auto: bool = False) -> None:
        # Rolling window: only the portion older than the last COMPACT_KEEP_LAST_TURNS turns
        # gets summarized -- the recent turns are kept verbatim.
        old, recent = split_at_turn_boundary(self.messages, COMPACT_KEEP_LAST_TURNS)
        transcript = render_transcript(old, PROVIDER_GEMINI)
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
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                response = self.client.interactions.create(
                    model=self.model,
                    input=[{"type": "user_input", "content": f"{SUMMARY_SYSTEM_PROMPT}\n\n{transcript}"}],
                )
            summary = getattr(response, "output_text", None) or ""
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
        record_compaction(old, summary, PROVIDER_GEMINI, kept_turns=COMPACT_KEEP_LAST_TURNS)
        self.messages = [
            {"type": "user_input", "content": f"[Conversation compacted. Summary of earlier context:]\n{summary}"},
            {
                "type": "model_output",
                "content": [{"type": "text", "text": "Understood — continuing from that summary."}],
            },
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
        # No verified token-counting endpoint wired up yet -- same rough heuristic as OpenAIAgent.
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
                f"(~{estimated_tokens}/{self.max_input_tokens} tokens) — "
                "try /compact to summarize, or /clear to reset, if you hit a context-limit error.[/]"
            )

    def _stream_response(self) -> tuple[str, list[_PendingFunctionCall], bool]:
        text_buffer = ""
        pending_calls: dict[int, _PendingFunctionCall] = {}
        incomplete = False

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            stream = self.client.interactions.create(
                model=self.model,
                input=self.messages,
                tools=self.tools,
                system_instruction=self._system_prompt(),
                generation_config={"max_output_tokens": self.max_tokens},
                stream=True,
            )

        # transient=True and a final print AFTER the block: the spinner must erase itself and the
        # finished answer must be written on a clean line, or the REPL prompt is drawn over leftover
        # live output and never appears. See the note in _run_turn_generate for the measured symptom.
        with Live(console=self.console, refresh_per_second=12, transient=True) as live:
            live.update(ThreeColorSpinner())
            for event in stream:
                event_type = getattr(event, "event_type", None)

                if event_type == "step.start":
                    step = getattr(event, "step", None)
                    if getattr(step, "type", None) == "function_call":
                        index = getattr(event, "index", 0)
                        call = pending_calls.setdefault(index, _PendingFunctionCall())
                        call.id = getattr(step, "id", None)
                        call.name = getattr(step, "name", None)
                        if getattr(step, "arguments", None):
                            call.arguments_fragments.append(step.arguments)

                elif event_type == "step.delta":
                    delta = getattr(event, "delta", None)
                    delta_type = getattr(delta, "type", None)
                    if delta_type == "text":
                        text_buffer += delta.text
                        live.update(render_markdown(text_buffer))
                        _stream_answer_to_console(self.console, text_buffer)
                    elif delta_type == "arguments_delta":
                        index = getattr(event, "index", 0)
                        call = pending_calls.setdefault(index, _PendingFunctionCall())
                        call.arguments_fragments.append(delta.arguments)

                elif event_type == "step.stop":
                    # UNVERIFIED (Gemini quota exhausted at implementation time): both `usage`
                    # and `step_usage` exist on StepStop with identical docstrings; which is
                    # authoritative, and whether multiple step.stop events per turn are additive
                    # or each already cumulative for the whole turn, needs confirming against a
                    # real call. Summing across all step.stop events is the more conservative
                    # default if they turn out to be additive; if they're each already
                    # turn-cumulative this will overcount and should change to "take the last one".
                    usage = getattr(event, "usage", None) or getattr(event, "step_usage", None)
                    if usage is not None:
                        self.usage_totals.add(
                            getattr(usage, "total_input_tokens", None),
                            getattr(usage, "total_output_tokens", None),
                        )

                elif event_type == "error":
                    error = getattr(event, "error", None)
                    message = getattr(error, "message", None) or "Gemini stream error"
                    code = getattr(error, "code", None)
                    raise GeminiStreamError(code, message)

                elif event_type == "interaction.completed":
                    # Confirmed via the installed SDK's type definitions
                    # (interactionsseeventinteraction.py): status is one of in_progress /
                    # requires_action / completed / failed / cancelled / incomplete --
                    # "incomplete" is Gemini's truncation signal (hit max_output_tokens before
                    # finishing). Previously never checked at all, so a truncated Gemini
                    # response gave no warning whatsoever, unlike the other two providers.
                    interaction = getattr(event, "interaction", None)
                    if getattr(interaction, "status", None) == "incomplete":
                        incomplete = True

        ordered_calls = [pending_calls[i] for i in sorted(pending_calls) if pending_calls[i].name]
        if text_buffer:
            self.console.print(render_markdown(text_buffer))
        return text_buffer, ordered_calls, incomplete


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
