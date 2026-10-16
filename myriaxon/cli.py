import argparse
import difflib
import json
import os
import sys
import threading
import time as _time
from dataclasses import replace
from pathlib import Path

from rich.markup import escape
from rich.prompt import Prompt

from . import checkpoints, commands, model_adapt, run_state, todo, tool_registry, tools, usage
from .agent import Agent
from .compaction import analyze_context_usage, format_context_breakdown, render_transcript, sanitize_resumed_messages
from .banner import print_banner
from .config import (
    Config,
    PROVIDER_ENV_KEY_FALLBACK,
    load_config,
    resolve_config,
    run_auth_wizard,
    save_config,
    select_option,
    switch_vendor,
)
from . import boundaries as boundaries_module
from . import character as character_module
from .constants import (
    APP_NAME,
    AUTO_COMPACT_THRESHOLD,
    CONTEXT_WARNING_THRESHOLD,
    DEEPSEEK_MODELS,
    DEFAULT_BASE_URL,
    DEFAULT_DEEPSEEK_BASE_URL,
    DEFAULT_GEMINI_BASE_URL,
    DEFAULT_MAX_TOKENS,
    DEFAULT_MAX_TOKENS_OPENAI_COMPAT,
    DEFAULT_NETRA_BASE_URL,
    DEFAULT_OLLAMA_BASE_URL,
    DEFAULT_MODE,
    DEFAULT_MODEL,
    DEFAULT_OPENROUTER_BASE_URL,
    DEFAULT_VERTEX_GLOBAL_LOCATION,
    DEFAULT_VERTEX_LOCATION,
    EFFORT_LEVELS,
    MCP_SERVERS_FILE,
    MODES,
    OPENROUTER_FAVORITE_MODELS,
    PROVIDER_ANTHROPIC,
    PROVIDER_DEEPSEEK,
    PROVIDER_GEMINI,
    PROVIDER_NETRA,
    PROVIDER_OLLAMA,
    PROVIDER_OPENAI_COMPAT,
    PROVIDER_OPENROUTER,
    VERSION,
)
from .dotenv_support import load_dotenv_into_environ
from .gemini_agent import GeminiStreamError
from .grounding import find_unverified_paths
from .hooks import load_hooks_config
from .logging_setup import LOG_FILE, configure_logging, logger
from .mcp_client import (
    MCP_REGISTRY,
    McpBridge,
    add_stdio_server,
    add_url_server,
    find_registry_match,
    is_command_available,
    load_mcp_config,
    remove_server,
    resolve_registry_stdio_spec,
    resolve_registry_url_spec,
    search_registry,
)
from .memory_store import format_memory_results, search_memory
from .model_info import ModelLimits, fetch_model_limits, list_models
from .model_profiles import PROFILES, describe_profile, detect_profile
from .project_instructions import load_project_instructions
from .scheduler import Scheduler
from .job_object import job_objects_available
from .session_store import load_session
from .settings import format_settings_summary, load_settings, set_user_theme, set_user_vim_mode
from .skills import discover_skills
from .subagents import discover_subagent_types
from .theme import THEMES, get_console
from .tools import docker_available, render_diff, windows_sandbox_available

# openai/google.genai are optional, sizable SDKs -- imported lazily (only when the relevant
# provider is actually used, or an exception of that provider's type needs matching) rather
# than eagerly at module load, since importing all three provider SDKs on every launch is
# wasted cost for anyone only using one provider that session.
_anthropic_module = None
_openai_module = None
_openai_checked = False
_genai_module = None
_genai_errors_module = None
_genai_checked = False

# Returned by `session.prompt()` when the idle Locus wake-up timer exits it instead of the user pressing Enter.
#
# A sentinel rather than an empty string, because the two cases must not be confused: empty input is a user
# pressing Enter, which the REPL may legitimately act on, while this means "nobody typed anything, go and run
# the Locus that just came due". The leading NUL also makes an accidental collision with real input impossible --
# a terminal cannot produce it, so no typed line can ever equal this value. Verified that `app.exit(result=...)`
# returns it byte-for-byte and that `str.strip()` leaves it intact.
LOCUS_WAKE_SENTINEL = "\x00myriaxon-locus-due"


# The lazy-import caches below are guarded by a lock. Two threads now call them concurrently -- the
# preload thread started in main() and the main thread itself -- and the unguarded
# `if not checked: checked = True; import ...` pattern let the preload set the flag first while the
# main thread then read a still-empty cache, which surfaced to the user as the absurd
# "the 'google-genai' package isn't installed" for a package that was installed. The lock makes the
# second caller wait for the first rather than proceed with a half-populated cache.
_sdk_import_lock = threading.Lock()


def _get_anthropic():
    # anthropic is a required (not optional) dependency, but importing it unconditionally at module
    # load cost ~36s of the ~45s total startup time in this environment (confirmed via
    # `python -X importtime`) -- paid on every launch regardless of which provider is actually
    # configured (e.g. DeepSeek/Gemini/OpenRouter users were paying for an SDK they never use).
    # No try/except needed here since, unlike openai/google-genai, it's always installed.
    global _anthropic_module
    with _sdk_import_lock:
        if _anthropic_module is None:
            import anthropic

            _anthropic_module = anthropic
        return _anthropic_module


def _get_genai_client(genai_module, config):
    """Return a cached Vertex/Gemini client for this configuration.

    Constructing `genai.Client` measured 2.19s, of which 0.73s is loading the SSL trust store and
    1.10s is building the underlying httpx client -- pure setup, no network. Every agent rebuild
    (model switch, mode switch, a dashboard session changing model) paid it again. Clients are
    keyed by the settings that define them, so a genuinely different configuration still gets a
    fresh client and a stale one is never reused. `config.api_key` is deliberately part of the key.
    """
    global _genai_client_cache
    if getattr(config, "vertex_project", ""):
        key = ("vertex", config.vertex_project, config.vertex_location or "")
    else:
        key = ("apikey", config.base_url, (config.api_key or "")[-8:])

    with _sdk_import_lock:
        cached = _genai_client_cache.get(key)
        if cached is not None:
            return cached
        if getattr(config, "vertex_project", ""):
            # Credentials from the operator's own `gcloud auth login`, when there is no API key.
            #
            # The client otherwise resolves Application Default Credentials, which `gcloud auth login`
            # does NOT create -- making ADC is a separate interactive command
            # (`gcloud auth application-default login`). So Vertex was reachable in principle and
            # unreachable in practice for anyone who signed in the ordinary way, which is the owner's
            # exact situation.
            #
            # Verified live, end to end, before this was wired: with the shim,
            # `genai.Client(vertexai=True, credentials=..., project=..., location="global")` answered
            # generateContent on `gemini-3.8-flash` with no API key anywhere on the machine.
            #
            # Used ONLY when no api_key is configured, so an operator who set one keeps exactly the
            # behaviour they had and nothing silently changes underneath them.
            vertex_credentials = None
            if not (config.api_key or "").strip():
                try:
                    from . import vertex_auth

                    vertex_credentials = vertex_auth.GcloudCredentials()
                except Exception:  # noqa: BLE001
                    # Falling through to the client's own resolution is right: if the shim cannot even
                    # be imported, ADC is the only remaining chance and it raises a clearer error than
                    # anything raised from here would.
                    logger.debug("gcloud credentials unavailable; falling back to ADC")

            client = genai_module.Client(
                vertexai=True,
                project=config.vertex_project,
                location=config.vertex_location or DEFAULT_VERTEX_GLOBAL_LOCATION,
                **({"credentials": vertex_credentials} if vertex_credentials else {}),
            )
        else:
            client = genai_module.Client(
                api_key=config.api_key, http_options={"base_url": config.base_url}
            )
        _genai_client_cache[key] = client
        return client


def _get_openai():
    global _openai_module, _openai_checked
    with _sdk_import_lock:
        if not _openai_checked:
            try:
                import openai

                _openai_module = openai
            except ImportError:
                _openai_module = None
            _openai_checked = True
        return _openai_module


def _make_openai_compat_client(provider: str, api_key: str, base_url: str):
    """Build the chat client for an OpenAI-compatible provider, preferring direct HTTP.

    Measured on this machine, in a fresh process:

        import openai   43.9s   (803 modules)
        import httpx     1.0s   (~40 modules)

    For a CLI that starts a process per command, that import IS the startup time. The direct client
    speaks the same wire protocol and was verified behaviourally equivalent against `openai` on the
    live Netra endpoint (identical reply text, identical tool-call arguments, equivalent reasoning
    output).

    `MYRIAXON_OPENAI_SDK=1` restores the SDK path, because an escape hatch matters when a provider
    exercises a corner of the protocol this client has not been tested against.
    """
    use_sdk = os.environ.get("MYRIAXON_OPENAI_SDK", "").strip() in ("1", "true", "yes")
    if not use_sdk:
        try:
            from .direct_http_client import DirectOpenAI

            headers = _openai_compat_headers(provider)
            return DirectOpenAI(
                api_key=api_key,
                base_url=base_url,
                timeout=300.0,
                max_retries=0,
                default_headers=headers,
            )
        except Exception:  # noqa: BLE001 -- fall back to the SDK rather than failing to start
            logger.exception("Direct HTTP client unavailable; falling back to the openai SDK.")

    openai_mod = _get_openai()
    if openai_mod is None:
        return None
    return openai_mod.OpenAI(api_key=api_key, base_url=base_url)


def _openai_compat_headers(provider: str) -> dict:
    """Provider-specific request headers.

    OpenRouter uses these for attribution and to enable its web plugin; other gateways ignore
    unknown headers, but they are only sent where they are meaningful.
    """
    if provider == PROVIDER_OPENROUTER:
        return {
            "HTTP-Referer": "https://github.com/myriaxon",
            "X-Title": "Myriaxon",
        }
    return {}


def _get_genai():
    global _genai_module, _genai_errors_module, _genai_checked
    with _sdk_import_lock:
        if not _genai_checked:
            try:
                import google.genai as genai
                from google.genai import errors as genai_errors

                _genai_module = genai
                _genai_errors_module = genai_errors
            except ImportError:
                _genai_module = None
                _genai_errors_module = None
            _genai_checked = True
        return _genai_module, _genai_errors_module


_genai_compat_errors_module = None
_genai_compat_errors_checked = False


def _get_genai_compat_errors():
    # The Interactions API surface (client.interactions.create, what gemini_agent.py actually
    # calls) raises from a *different*, newer internal error hierarchy than the classic
    # google.genai.errors module handled by _gemini_api_errors() below -- confirmed live: a real
    # 401 raised google.genai._gaos.lib.compat_errors.AuthenticationError, which is NOT a
    # subclass of genai_errors.APIError, so it fell through to the generic exception handler
    # instead of a clean "Authentication failed" message. This hierarchy conveniently mirrors
    # anthropic/openai's own APIStatusError shape (.status_code, .message), confirmed via
    # constructing a real instance -- so its members slot directly into the existing
    # _auth_errors/_rate_limit_errors/_connection_errors/_status_errors tuples below instead of
    # needing Gemini-specific handling.
    global _genai_compat_errors_module, _genai_compat_errors_checked
    if not _genai_compat_errors_checked:
        _genai_compat_errors_checked = True
        try:
            import google.genai._gaos.lib.compat_errors as compat_errors

            _genai_compat_errors_module = compat_errors
        except ImportError:
            _genai_compat_errors_module = None
    return _genai_compat_errors_module


_httpx_module = None
_httpx_checked = False
_httpx2_module = None
_httpx2_checked = False


def _get_httpx():
    # Confirmed live 2026-08-31: a mid-stream connection drop ("peer closed connection without
    # sending complete message body") raises the RAW httpx transport exception, not the SDK's
    # own wrapped APIConnectionError -- anthropic/openai only wrap errors raised while *opening*
    # a request (inside their own request() method's try/except), not ones raised later while a
    # caller iterates an already-established stream's chunks (that generator body runs outside
    # any SDK-level try/except). httpx backs anthropic/google-genai's HTTP calls.
    global _httpx_module, _httpx_checked
    if not _httpx_checked:
        _httpx_checked = True
        try:
            import httpx

            _httpx_module = httpx
        except ImportError:
            _httpx_module = None
    return _httpx_module


def _get_httpx2():
    # httpx2 (a genuinely separate, newer major-version package, not a typo/duplicate --
    # confirmed via `pip show`) is what the openai SDK's 2.x line uses instead of plain httpx.
    global _httpx2_module, _httpx2_checked
    if not _httpx2_checked:
        _httpx2_checked = True
        try:
            import httpx2

            _httpx2_module = httpx2
        except ImportError:
            _httpx2_module = None
    return _httpx2_module


MODEL_LIMITS_HARD_TIMEOUT_SECONDS = 3.0


def _fetch_model_limits_bounded(provider: str, client, model: str) -> ModelLimits:
    """Wraps fetch_model_limits with a hard wall-clock ceiling, independent of whatever
    timeout/retry options the underlying SDK claims to honor. Confirmed live this was
    genuinely necessary: fetch_model_limits already passes a 3s per-call timeout with retries
    disabled, and a raw httpx request to the same endpoint completed in under 3s, yet the SDK
    call still took 12-15s regardless -- the extra latency isn't coming from the network request
    itself, so no SDK-level timeout parameter can bound it. Runs the call in a daemon thread and
    gives up (falling back to the caller's own hardcoded defaults, same as any other
    fetch_model_limits failure) if it hasn't finished within the ceiling; the orphaned thread is
    harmless if it eventually completes since its result is simply discarded."""
    result: dict[str, ModelLimits] = {}

    def worker() -> None:
        result["value"] = fetch_model_limits(provider, client, model)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    thread.join(MODEL_LIMITS_HARD_TIMEOUT_SECONDS)
    return result.get("value", ModelLimits(None, None))


EXIT_WORDS = {"exit", "quit", "/exit", "/quit"}


# Exception-type tuples are built fresh on each call rather than once at import time -- `except
# EXPR:` only evaluates EXPR when an exception is actually propagating and needs matching, so in
# the common no-error case these never trigger the lazy imports above at all.
_active_provider_module = ""


def preload_provider_sdk(provider: str) -> None:
    """Start loading the active provider's SDK in a background thread.

    Measured on this machine: importing a provider SDK costs 45-55s (openai), 40s (anthropic) or
    ~12s (google.genai), dominated by on-access file scanning rather than CPU. That cost is paid
    once per process, and the CLI does real work before the client is needed -- loading config,
    settings, hooks, MCP config, skills, checking for a resumable session. Overlapping the import
    with that work removes it from the critical path entirely, and the import lock makes it safe:
    the main thread simply blocks on the same module if it arrives first.

    Only the provider actually in use is preloaded. Preloading all three was the original behaviour
    and is the single worst startup decision in the program -- a Gemini user paid for the anthropic
    AND openai SDKs before their first token.
    """
    global _active_provider_module
    _active_provider_module = provider
    loader = {
        PROVIDER_GEMINI: _get_genai,
        PROVIDER_DEEPSEEK: _get_openai,
        PROVIDER_OPENROUTER: _get_openai,
        PROVIDER_NETRA: _get_openai,
        PROVIDER_OPENAI_COMPAT: _get_openai,
        PROVIDER_OLLAMA: _get_openai,
    }.get(provider, _get_anthropic)
    thread = threading.Thread(target=loader, name="sdk-preload", daemon=True)
    thread.start()
    _preload_threads.append(thread)


_preload_threads: list = []

# Vertex/Gemini clients are expensive to build (2.19s, mostly SSL and httpx setup) and safe to
# reuse, so they are cached per configuration. Guarded by the same SDK lock as the imports above.
_genai_client_cache: dict = {}


def wait_for_preload(timeout: float = 180.0) -> None:
    """Let the preload finish before a failure path tries to introspect the same modules.

    Called only when something has already gone wrong, so the cost is irrelevant and the benefit is
    that error classification sees a fully-imported module instead of racing it.
    """
    for thread in _preload_threads:
        thread.join(timeout=timeout)


def _active_error_modules() -> list:
    """SDK modules worth consulting when classifying an exception, for the active provider only.

    Replacing the previous build-all-three behaviour: constructing exception tuples used to import
    anthropic, openai and google.genai on every error, which on this machine is ~100s of import work
    triggered by a single failed request.
    """
    modules = []
    if _active_provider_module == PROVIDER_GEMINI:
        modules.extend(m for m in _get_genai() if m is not None)
        compat = _get_genai_compat_errors()
        if compat is not None:
            modules.append(compat)
    else:
        openai_module = _get_openai()
        if openai_module is not None:
            modules.append(openai_module)
        if _active_provider_module not in (PROVIDER_DEEPSEEK, PROVIDER_OPENROUTER, PROVIDER_NETRA, PROVIDER_OPENAI_COMPAT,
                                           PROVIDER_OLLAMA):
            anthropic_module = _get_anthropic()
            if anthropic_module is not None:
                modules.append(anthropic_module)
    return modules


def _direct_errors() -> dict:
    """The errors the built-in HTTP client raises, which is the client every OpenAI-compatible provider uses unless the SDK is asked for.

    They were never part of the classification below: with the `openai` SDK not installed (the normal case) a refused
    key, a rate limit, a 500 and a server that is not running all fell through to "Unexpected error -- details logged",
    which says nothing about what to do. Cheap to import (no network library), so it is imported when an error happens.
    """
    from . import direct_http_client as direct

    return {"auth": (direct.DirectAuthError,), "rate_limit": (direct.DirectRateLimitError,),
            "connection": (direct.DirectConnectionError,), "status": (direct.DirectAPIError,)}


def _auth_errors():
    # Exception-type tuples are built fresh on each call rather than once at import time -- `except
    # EXPR:` only evaluates EXPR when an exception is actually propagating, so in the common no-error
    # case the lazy imports above never trigger at all.
    return tuple(
        getattr(module, "AuthenticationError")
        for module in _active_error_modules()
        if hasattr(module, "AuthenticationError")
    ) + _direct_errors()["auth"]


def _rate_limit_errors():
    return tuple(
        getattr(module, "RateLimitError")
        for module in _active_error_modules()
        if hasattr(module, "RateLimitError")
    ) + _direct_errors()["rate_limit"]


def _connection_errors():
    modules = _active_error_modules()
    httpx_mod = _get_httpx()
    httpx2_mod = _get_httpx2()
    return (
        tuple(
            getattr(module, "APIConnectionError")
            for module in modules
            if hasattr(module, "APIConnectionError")
        )
        + tuple(
            getattr(module, "APITimeoutError")
            for module in modules
            if hasattr(module, "APITimeoutError")
        )
        # See _get_httpx/_get_httpx2 -- a dropped connection mid-stream raises the raw
        # transport exception, never wrapped into the SDK's own APIConnectionError.
        + ((httpx_mod.RemoteProtocolError,) if httpx_mod else ())
        + ((httpx2_mod.RemoteProtocolError,) if httpx2_mod else ())
        + _direct_errors()["connection"]
    )


def _status_errors():
    return tuple(
        getattr(module, "APIStatusError")
        for module in _active_error_modules()
        if hasattr(module, "APIStatusError")
    ) + _direct_errors()["status"]


# Gemini's errors.APIError (ClientError/ServerError) uses .code/.message, not .status_code --
# handled in its own except clause rather than folded into _status_errors(). GeminiStreamError
# carries the same .code/.message shape for errors delivered as a mid-stream SSE event instead
# of a raised SDK exception (quota/auth failures can arrive either way).
def _gemini_api_errors():
    _, genai_errors_mod = _get_genai()
    return ((genai_errors_mod.APIError,) if genai_errors_mod else ()) + (GeminiStreamError,)


def _pick(console, message, options, default=None):
    """An arrow-key picker the user can back out of. Returns the chosen value, or None after saying "Cancelled."

    Callers go back to the prompt on None. The setup wizard keeps using `select_option` directly, because an answer is
    required there.
    """
    chosen = select_option(console, message, options, default=default, cancellable=True)
    if chosen is None:
        console.print("[dim]Cancelled.[/]")
    return chosen


def is_unknown_slash_command(text: str) -> bool:
    """True when `text` is meant as a slash command that does not exist, rather than as a message.

    A first word that is `/` followed only by letters, digits, `-` or `_` is a command name: `/hepl` is a typo for `/help`
    and must say so, not spend a model turn answering it. A first word with a second `/`, a `.`, a backslash or anything
    else in it is a path (`/etc/hosts what is this?`) and goes to the model as the message it is.

    The caller has already tried every built-in command and every custom one, so what reaches here is not either.
    """
    token = text.strip().split(None, 1)[0] if text.strip() else ""
    name = token[1:]
    return token.startswith("/") and bool(name) and all(ch.isalnum() or ch in "-_" for ch in name)


def _ollama_hint_suffix(message: str, *, connection: bool = False, provider: str = "") -> str:
    """For an error from a local model, a sentence that says the one thing to try (with a leading space); "" for every other provider."""
    if (provider or _active_provider_module) != PROVIDER_OLLAMA:
        return ""
    from . import ollama as ollama_module

    hint = ollama_module.hint_for_error(message, connection=connection)
    return f" {hint}" if hint else ""


def _print_ollama_hint(agent, console, message: str, *, connection: bool = False) -> None:
    """After an error from a local model, say the one thing to try. Silent for every other provider."""
    hint = _ollama_hint_suffix(message, connection=connection, provider=getattr(agent, "provider", ""))
    if hint:
        console.print(f"[dim]{escape(hint.strip())}[/]")


def _run_turn_safely(agent, text: str, console) -> None:
    """Shared by the REPL's normal input path and scheduled-prompt firing -- same exception
    handling either way, so a fired scheduled prompt behaves identically to the user having
    typed it themselves."""
    snapshot_len = len(agent.messages)
    agent.tool_context.current_turn_message_count = snapshot_len
    if agent.tool_context.grounding_enabled:
        agent.tool_context.touched_paths_this_turn = set()
    # Reset unconditionally, unlike the grounding set above: this one is read by the Locus runner to
    # decide whether an objective that demanded execution actually got any, and a stale set from a
    # previous turn would make an idle turn look like a working one.
    try:
        agent.tool_context.tools_called_this_turn = set()
    except AttributeError:
        pass
    # The character. Created HERE rather than passed in, because this is the one function every turn goes
    # through -- a character wired anywhere else is a character that some turns never reach.
    character = character_module.for_agent(agent)
    character.before_turn()
    failure: BaseException | None = None
    try:
        agent.run_turn(text)
    except KeyboardInterrupt:
        console.print("\n[warning]Interrupted.[/]")
    except _auth_errors() as exc:
        logger.error("Authentication failed: %s", exc)
        console.print("[error]Authentication failed. Run 'myriaxon --reauth' to update your API key.[/]")
    except _rate_limit_errors() as exc:
        if _try_fallback(agent, console, snapshot_len):
            _run_turn_safely(agent, text, console)
        else:
            logger.warning("Rate limited: %s", exc)
            console.print("[error]Rate limited by the API. Try again shortly.[/]")
    except _connection_errors() as exc:
        logger.error("Connection error: %s", exc)
        console.print(f"[error]Could not reach the API endpoint: {escape(str(exc))}[/]")
        _print_ollama_hint(agent, console, str(exc), connection=True)
    except _status_errors() as exc:
        logger.error("API error (%s): %s", exc.status_code, exc.message)
        console.print(f"[error]API error ({exc.status_code}): {escape(exc.message)}[/]")
        _print_ollama_hint(agent, console, exc.message)
    except _gemini_api_errors() as exc:
        if getattr(exc, "code", None) == 429 and _try_fallback(agent, console, snapshot_len):
            _run_turn_safely(agent, text, console)
        else:
            _handle_gemini_error(exc, console)
    except model_adapt.ContextExhausted as exc:
        # The context budget's deliberate refusal to send an over-window request. It carries a
        # written explanation and the fix ("run /compact"), so it is reported as guidance rather
        # than as an unexpected error -- falling through to the generic handler below replaced
        # that message with "Unexpected error", which told the user nothing they could act on.
        console.print(f"[warning]{escape(str(exc))}[/]")
    except Exception as exc:
        failure = exc
        logger.exception("Unhandled error during run_turn")
        console.print(f"[error]Unexpected error -- details logged to {escape(str(LOG_FILE))}[/]")
    finally:
        # In a `finally`, so a turn that RAISED still moves the face. That is the whole point: a failed turn
        # must not leave a happy face behind, and every other placement of this call skips the error path.
        character.after_turn(agent, error=failure)
    # The grounding warning runs after the turn, whether it succeeded or not. It used to sit in an `else:`, which
    # Python does not allow beside a `finally` -- and `finally` is worth more here, because the face matters on
    # every path while the warning only matters when the turn produced a reply to check.
    if failure is None:
        if agent.tool_context.grounding_enabled:
            unverified = find_unverified_paths(agent.last_assistant_text(), agent.tool_context.touched_paths_this_turn)
            if unverified:
                console.print(
                    f"[warning]/ground: this reply mentions {', '.join(unverified)} but no tool call "
                    "this turn touched them -- worth verifying before trusting that claim.[/]"
                )

        # Section 8.5, checked HERE because this is the one function every turn goes through. It runs AFTER the
        # reply has streamed, so it cannot unprint it -- what it does is tell the user the reply crossed a line,
        # which is the most this position can honestly offer. `boundaries.apply_boundaries` does the replacing, for
        # a caller that has the text before showing it.
        try:
            reply_text = agent.last_assistant_text() or ""
            found = boundaries_module.review(reply_text, user_message=text)
            if not found.clean:
                logger.warning("Section 8.5 boundary crossed: %s", found.render())
                console.print(
                    f"[warning]Boundary check: this reply may cross one of the lines Loci holds "
                    f"({escape(', '.join(sorted(found.boundaries)))}). Worth reading with that in mind.[/]"
                )
                # The face follows the content, not only the turn's outcome: a reply that crossed a line is not a
                # cheerful moment, and the character is the one part of the interface that can say so quietly.
                if boundaries_module.DIAGNOSIS in found.boundaries or boundaries_module.MEDICATION in found.boundaries:
                    # character_module.expression is the module character.py already imported, and _time is how this file
                    # imports time -- neither `expression_module` nor a bare `time` exists here, which the first
                    # version of this wiring assumed and which compiling caught.
                    character.state.apply(character_module.expression.SIGNAL_MOOD_LOW, now=_time.time())
        except Exception:  # noqa: BLE001 - a check must never take down a turn that already succeeded
            logger.debug("Boundary check skipped", exc_info=True)


def _show_session_diff(console, session_start_time: float) -> None:
    """Unified diff, per file, between the earliest checkpoint recorded since this process
    started and the file's current on-disk content -- i.e. "everything changed this session".
    Checkpoints persist across sessions in .myriaxon/checkpoints/index.json, so filtering by
    timestamp >= session_start_time is what scopes this to just the current run rather than the
    project's entire edit history."""
    entries = [e for e in checkpoints.list_checkpoints() if e.get("timestamp", 0) >= session_start_time]
    if not entries:
        console.print("[dim]No file changes recorded this session.[/]")
        return

    earliest_by_path: dict[str, dict] = {}
    for entry in entries:
        path = entry["original_path"]
        if path not in earliest_by_path or entry["timestamp"] < earliest_by_path[path]["timestamp"]:
            earliest_by_path[path] = entry

    any_diff = False
    for path, entry in sorted(earliest_by_path.items()):
        backup_file = Path(entry["backup_file"])
        if not backup_file.exists():
            continue
        before = backup_file.read_text(encoding="utf-8", errors="replace")
        target = Path(path)
        after = target.read_text(encoding="utf-8", errors="replace") if target.exists() else ""
        if before == after:
            continue
        any_diff = True
        render_diff(console, target, before, after)

    if not any_diff:
        console.print("[dim]No net file changes this session (edits were made but reverted, or unchanged).[/]")


def _print_rewind_points(console) -> None:
    points = checkpoints.list_rewind_points(limit=15)
    if not points:
        if not checkpoints.shadow_git_available():
            console.print(
                "[warning]No rewind points available -- git isn't installed/on PATH. Shadow-git "
                "checkpointing (whole-project + conversation revert) requires it; /undo (single-"
                "file only) still works without it.[/]"
            )
        else:
            console.print("[dim]No rewind points yet -- make an edit first, then /rewind <id>.[/]")
        return

    for entry in points:
        when = _time.strftime("%Y-%m-%d %H:%M:%S", _time.localtime(entry["timestamp"]))
        console.print(
            escape(f"  {entry['id']}  {when}  {entry['tool_name']}  {entry['original_path']}")
        )
    console.print("[dim]/rewind <id> reverts every tracked file AND the conversation to just before that turn.[/]")


def _run_doctor(console, config: Config, agent, settings) -> None:
    """One-shot health check -- everything that could make Myriaxon feel unreliable in one place,
    instead of the user having to dig through logs after something feels off."""
    console.print("[bold]Myriaxon doctor[/]")

    console.print(f"Provider: {config.provider}  Model: {agent.model}")
    console.print(f"API key configured: {'yes' if config.api_key else 'no'}")
    console.print(f"Endpoint: {config.base_url or '(provider default)'}")

    # Only probe the SDK the active provider actually needs -- _get_openai()/_get_genai() are
    # lazy specifically so an unused, sizable SDK (confirmed ~27-38s cold-import cost in this
    # environment) is never imported. A health check that eagerly imports every provider's SDK
    # "just to report on it" would silently reintroduce that exact cost on every /doctor call.
    if config.provider == PROVIDER_ANTHROPIC:
        console.print("SDK 'anthropic': [success]OK[/] (required dependency, always installed)")
    elif config.provider in (PROVIDER_OPENAI_COMPAT, PROVIDER_DEEPSEEK, PROVIDER_OPENROUTER, PROVIDER_NETRA):
        ok = _get_openai() is not None
        console.print(f"SDK 'openai': {'[success]OK[/]' if ok else '[error]NOT INSTALLED[/]'}")
    elif config.provider == PROVIDER_GEMINI:
        genai_mod, _ = _get_genai()
        ok = genai_mod is not None
        console.print(f"SDK 'google-genai': {'[success]OK[/]' if ok else '[error]NOT INSTALLED[/]'}")

    console.print(
        f"Sandbox: enabled={agent.tool_context.sandbox_enabled}  "
        f"docker={'available' if docker_available() else 'not found'}  "
        f"windows_sandbox={'available' if windows_sandbox_available() else 'not found'}  "
        f"job_objects={'available' if job_objects_available() else 'not available (non-Windows)'}"
    )

    console.print(f"MCP: {'connected' if agent.tool_context.mcp_bridge is not None else 'not configured'}")
    console.print(f"Scheduler: {'active' if agent.scheduler is not None else 'not started'}")

    if settings.sources:
        console.print(f"Settings layers active: {', '.join(settings.sources)}")
    else:
        console.print("Settings layers active: none (no settings.json at any layer)")
    if settings.enforced_provider:
        console.print(f"  enforced_provider: {settings.enforced_provider}")
    if settings.enforced_mode:
        console.print(f"  enforced_mode: {settings.enforced_mode}")
    if settings.enforced_sandbox:
        console.print("  enforced_sandbox: True")

    checkpoints_dir = Path.cwd() / ".myriaxon" / "checkpoints"
    console.print(f"Checkpoints directory writable: {'yes' if _dir_is_writable(checkpoints_dir) else 'no'}")

    buckets = analyze_context_usage(agent.messages)
    system_prompt_len = len(agent._system_prompt())
    tools_len = len(str(agent.tools))
    estimated_tokens = (system_prompt_len + tools_len + sum(buckets.values())) // 4
    max_input_tokens = getattr(agent, "max_input_tokens", None)
    if max_input_tokens:
        pct = round(estimated_tokens / max_input_tokens * 100)
        if config.provider == PROVIDER_ANTHROPIC:
            console.print(
                f"Context: ~{estimated_tokens:,}/{max_input_tokens:,} tokens ({pct}%) -- Anthropic "
                f"auto-compacts server-side; a local warning fires at "
                f"{round(CONTEXT_WARNING_THRESHOLD * 100)}%, no local auto-compact trigger."
            )
        else:
            trigger_pct = round(AUTO_COMPACT_THRESHOLD * 100)
            headroom = max(0, trigger_pct - pct)
            console.print(
                f"Context: ~{estimated_tokens:,}/{max_input_tokens:,} tokens ({pct}%) -- "
                f"auto-compact triggers at {trigger_pct}% (~{headroom} percentage points of "
                "headroom left before it fires)."
            )
    elif config.provider == PROVIDER_DEEPSEEK:
        console.print(
            "Context: unknown -- confirmed live that DeepSeek's API never publishes a model's "
            "context-window size (no context_length field on its /models list, and /models/{id} "
            "404s), not a resolution failure on our side."
        )
    else:
        console.print("Context: unknown -- this model's context window size wasn't resolved.")

    console.print(f"Messages in memory: {len(agent.messages)}  Mode: {agent.mode}  Yolo: {agent.yolo}")
    console.print("[dim]Log file:[/] " + str(LOG_FILE))


def _print_model_list(console, config: Config, agent) -> None:
    """Shared by /models and /model list. OpenRouter (its own provider id, not just a base_url
    guess within the generic "openai_compat" bucket) gets a small curated favorites shortlist
    ahead of the full live catalog -- never a restriction, its catalog stays freely-typed same
    as always.

    Ollama is listed from the machine, not from a web endpoint. `/models` asked every provider for an
    OpenAI-shaped `/models` catalogue, and Ollama's does not describe the models a user can switch to
    -- so the answer was "Could not list models for this provider (unsupported or request failed)"
    while the models sat on the disk. The local report is also the only place the RUNNING window is
    visible, which is the number that decides whether a model can drive the agent at all.
    """
    if config.provider == PROVIDER_OLLAMA:
        from . import ollama as ollama_module

        probe = ollama_module.detect(with_details=True)
        if not probe.available:
            console.print(f"[warning]{escape(probe.explain())}[/]")
            console.print("[dim]Start Ollama (`ollama serve`, or open the Ollama app), then try again.[/]")
            return
        current = getattr(agent, "model", "") or config.model
        for line in ollama_module.report(with_details=True):
            # The active model is marked, because a list of four models that does not say which one
            # is answering makes the user check twice.
            if line.strip().startswith(current):
                console.print(f"[success]{escape(line)}   <- in use[/]")
            else:
                console.print(escape(line) if line.strip() else "")
        console.print("[dim]Switch with:  /model <name>    Build a full-window model with:  "
                      "myriaxon ollama --create[/]")
        return

    if config.provider == PROVIDER_OPENROUTER:
        console.print("[bold]Favorites:[/]")
        for model_id in OPENROUTER_FAVORITE_MODELS:
            console.print(f"  {model_id}")
        console.print("[bold]Full catalog:[/]")

    available = list_models(config.provider, agent.client)
    if not available:
        console.print("[warning]Could not list models for this provider (unsupported or request failed).[/]")
    else:
        lines = [
            f"{m['id']}" + (f"  ({m['display_name']})" if m["display_name"] else "")
            for m in available
        ]
        console.print("\n".join(lines))


def _print_mcp_list(console, agent) -> None:
    # Re-reads from disk rather than trusting whatever was loaded at startup -- reflects a
    # /mcp install or /mcp remove run earlier in this same session, or a manual edit of the file.
    config = load_mcp_config()
    connected = agent.tool_context.mcp_bridge.connected_server_names() if agent.tool_context.mcp_bridge else set()

    if not config["stdio_servers"] and not config["url_servers"]:
        console.print(
            "[dim]No MCP servers configured. Run /mcp list-available to see what's installable, "
            f"or edit {MCP_SERVERS_FILE} directly for anything else.[/]"
        )
        return

    for s in config["stdio_servers"]:
        status = "connected" if s["name"] in connected else "configured -- restart to connect"
        command_line = " ".join([s["command"], *s.get("args", [])])
        # markup=False -- a literal "(stdio)"-style tag next to the name would otherwise risk
        # the same fate as /help's bracketed placeholders if it ever used square brackets; kept
        # as plain parentheses and escaped defensively since server names/command lines are
        # user-supplied and could themselves contain brackets.
        console.print(escape(f"  {s['name']}  (stdio)  {status}\n    {command_line}"))
    for s in config["url_servers"]:
        console.print(escape(f"  {s['name']}  (url)  {s['url']}"))


def _print_available_mcp_servers(console) -> None:
    """/mcp list-available -- distinct from /mcp list (currently configured servers): this shows
    what's actually installable right now, i.e. the built-in curated registry, each annotated
    with whether the runtime it needs (npx/uvx) is actually on PATH on this machine."""
    console.print("[bold]Built-in (curated, install with /mcp install <name>):[/]")
    for name in sorted(MCP_REGISTRY):
        entry = MCP_REGISTRY[name]
        required_bin = entry.get("requires")
        if required_bin:
            ready = "ready" if is_command_available(required_bin) else f"needs {required_bin} on PATH"
        else:
            ready = "ready"
        console.print(escape(f"  {name}  ({ready})  -- {entry.get('description', '')}"))
    console.print(
        "[dim]For anything else, /mcp search <query> checks the live official MCP registry "
        "(registry.modelcontextprotocol.io) -- covers far more than this curated list.[/]"
    )


def _install_mcp_server(console, name: str) -> None:
    name = name.strip()
    if not name:
        console.print("[warning]Usage: /mcp install <name>. See /mcp list-available, or /mcp search <query>.[/]")
        return

    entry = MCP_REGISTRY.get(name)
    if entry is not None:
        _install_from_builtin_registry(console, name, entry)
        return

    # Not one of the 5 curated entries -- fall back to the live official MCP registry, which
    # covers a vastly larger set (confirmed live: registry.modelcontextprotocol.io/v0/servers).
    matches = search_registry(name, limit=10)
    match = find_registry_match(name, matches)
    if match is None:
        safe_name = escape(name)
        if matches:
            listing = "\n".join(
                escape(f"  {s['name']} -- {(s.get('description') or '')[:80]}") for s in matches[:10]
            )
            console.print(
                f"[warning]No exact match for {safe_name!r}. Closest results from the live MCP "
                f"registry:\n{listing}\nRe-run /mcp install <exact name from above>.[/]"
            )
        else:
            console.print(
                f"[warning]Unknown MCP server {safe_name!r} -- not in the built-in list "
                f"({', '.join(sorted(MCP_REGISTRY))}) and no match on the live registry. Try "
                f"/mcp search {safe_name} or edit {MCP_SERVERS_FILE} directly.[/]"
            )
        return
    _install_from_registry_match(console, name, match)


def _install_from_builtin_registry(console, name: str, entry: dict) -> None:
    required_bin = entry.get("requires")
    if required_bin and not is_command_available(required_bin):
        console.print(
            f"[warning]{required_bin!r} isn't on PATH -- {name} needs it to actually run. Writing the "
            f"config entry anyway; install {required_bin} before restarting Myriaxon.[/]"
        )

    args = list(entry.get("args", []))
    for placeholder, prompt_text in entry.get("prompts", []):
        value = Prompt.ask(prompt_text, console=console)
        args = [a.replace(f"{{{placeholder}}}", value) for a in args]

    env: dict[str, str] = {}
    for env_var, prompt_text, is_secret in entry.get("env_prompts", []):
        env[env_var] = Prompt.ask(prompt_text, password=is_secret, console=console)

    error = add_stdio_server(name, entry["command"], args, env or None)
    if error:
        console.print(f"[error]{escape(error)}[/]")
    else:
        console.print(f"[success]Added {name!r} to {MCP_SERVERS_FILE}. Restart Myriaxon for it to connect.[/]")


def _install_from_registry_match(console, name: str, match: dict) -> None:
    # escape() -- match["name"] comes from the live, third-party-controlled MCP registry; every
    # message below interpolates full_name inside a [style]...[/] f-string, so it's escaped once
    # here rather than at each use site.
    full_name = escape(match.get("name", name))

    stdio_spec = resolve_registry_stdio_spec(match)
    if stdio_spec is not None:
        if not is_command_available(stdio_spec["command"]):
            console.print(
                f"[warning]{stdio_spec['command']!r} isn't on PATH -- {full_name} needs it to actually "
                f"run. Writing the config entry anyway; install {stdio_spec['command']} before "
                "restarting Myriaxon.[/]"
            )
        env: dict[str, str] = {}
        for env_var in stdio_spec["env_vars"]:
            var_name = env_var.get("name")
            if not var_name:
                continue
            # escape() -- env_var['description'] comes from the live, third-party-controlled MCP
            # registry, not code we wrote; Prompt.ask parses its label as Rich markup (confirmed:
            # PromptBase.__init__ calls Text.from_markup on it), so an untrusted description
            # containing "[...]" could otherwise inject markup into the prompt display.
            label = var_name + (f" ({escape(env_var['description'])})" if env_var.get("description") else "")
            if not env_var.get("isRequired"):
                label += " (optional -- Enter to skip)"
            value = Prompt.ask(label, password=bool(env_var.get("isSecret")), default="", console=console)
            if value:
                env[var_name] = value
        error = add_stdio_server(name, stdio_spec["command"], stdio_spec["args"], env or None)
        if error:
            console.print(f"[error]{escape(error)}[/]")
        else:
            console.print(
                f"[success]Added {name!r} ({full_name}) to {MCP_SERVERS_FILE} from the live "
                "registry. Restart Myriaxon for it to connect.[/]"
            )
        return

    url_spec = resolve_registry_url_spec(match)
    if url_spec is not None:
        error = add_url_server(name, url_spec["url"])
        if error:
            console.print(f"[error]{escape(error)}[/]")
        else:
            console.print(
                f"[success]Added {name!r} ({full_name}) as a remote MCP server to {MCP_SERVERS_FILE}. "
                "Restart Myriaxon for it to connect.[/]"
            )
        return

    console.print(
        f"[warning]Found {full_name!r} on the registry but it has no npm/pypi package or remote URL "
        f"Myriaxon can install automatically. See {MCP_SERVERS_FILE} to configure it by hand.[/]"
    )


def _search_mcp_registry(console, query: str) -> None:
    query = query.strip()
    if not query:
        console.print("[warning]Usage: /mcp search <query>[/]")
        return
    results = search_registry(query, limit=10)
    if not results:
        console.print(f"[dim]No results for {query!r} on the live MCP registry.[/]")
        return
    for s in results:
        console.print(escape(f"  {s['name']}  -- {(s.get('description') or '')[:100]}"))
    console.print("[dim]Install with: /mcp install <name>[/]")


def _dir_is_writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".myriaxon_doctor_probe"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def _try_fallback(agent, console, snapshot_len: int) -> bool:
    """On a rate-limit/quota error, swap to the configured fallback model and retry the turn from
    a clean slate -- the failed attempt's partial messages (the just-appended user turn, and any
    partial assistant/tool messages from a turn that failed mid-stream) are discarded first, since
    replaying run_turn would otherwise re-append a duplicate user message. Returns True if a
    fallback was actually available and applied (caller should retry); False if the caller should
    report the error as usual. Naturally bounded to one hop: once agent.model == fallback_model,
    a second failure has nothing left to fall back to."""
    fallback = getattr(agent, "fallback_model", "") or ""
    if not fallback or fallback == agent.model:
        return False
    console.print(
        f"[warning]Rate limited on {escape(agent.model)} -- falling back to "
        f"{escape(fallback)} and retrying.[/]"
    )
    del agent.messages[snapshot_len:]
    agent.model = fallback
    agent.tool_context.model = fallback
    return True


_MODE_ALIASES = {
    "manual": "manual",
    "auto-edit": "auto_edit",
    "auto_edit": "auto_edit",
    "plan": "plan",
    "auto": "auto",
}

_PROVIDER_ALIASES = {
    "anthropic": PROVIDER_ANTHROPIC,
    "openai_compat": PROVIDER_OPENAI_COMPAT,
    "openai-compat": PROVIDER_OPENAI_COMPAT,
    "openai": PROVIDER_OPENAI_COMPAT,
    "gemini": PROVIDER_GEMINI,
    "deepseek": PROVIDER_DEEPSEEK,
    "openrouter": PROVIDER_OPENROUTER,
    "netra": PROVIDER_NETRA,
    "netraruntime": PROVIDER_NETRA,
    "ollama": PROVIDER_OLLAMA,
}

_PROVIDER_DEFAULT_ENDPOINTS = {
    PROVIDER_ANTHROPIC: DEFAULT_BASE_URL,
    PROVIDER_GEMINI: DEFAULT_GEMINI_BASE_URL,
    PROVIDER_DEEPSEEK: DEFAULT_DEEPSEEK_BASE_URL,
    PROVIDER_OPENROUTER: DEFAULT_OPENROUTER_BASE_URL,
    PROVIDER_NETRA: DEFAULT_NETRA_BASE_URL,
    PROVIDER_OLLAMA: DEFAULT_OLLAMA_BASE_URL,
}


def _handle_gemini_error(exc, console) -> None:
    if exc.code in (401, 403):
        logger.error("Gemini authentication failed: %s", exc.message)
        console.print("[error]Authentication failed. Run 'myriaxon --reauth' to update your API key.[/]")
    elif exc.code == 429:
        logger.warning("Gemini rate limited: %s", exc.message)
        console.print("[error]Rate limited by the API. Try again shortly.[/]")
    else:
        logger.error("Gemini API error (%s): %s", exc.code, exc.message)
        console.print(f"[error]API error ({exc.code}): {escape(str(exc.message))}[/]")


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    """Thin alias for the single shared definition in argparse_cli.

    The real parser lives in `argparse_cli` so that `--help`/`--version` never import this module
    (which costs several seconds and drags in every agent, checkpoints and MCP). Keeping the name
    here means existing callers and tests are unaffected.
    """
    from .argparse_cli import parse_args as _parse_args

    return _parse_args(argv)



def _context_exhausted_errors() -> tuple:
    """The exception type raised when a conversation cannot fit the model's window.

    Imported here rather than at module scope for the same reason as the provider error tuples: the
    type is only consulted after a failure, and importing the budget machinery on every successful
    run to build a tuple nobody reads is cost for nothing.

    Returns an empty tuple when the module cannot be imported. Every caller guards with
    `if types and isinstance(...)`, so an empty tuple means "cannot classify", which falls through to
    the catch-all rather than raising from inside error handling.
    """
    try:
        from .model_adapt import ContextExhausted

        return (ContextExhausted,)
    except Exception:  # noqa: BLE001
        return ()


def _classify_run_error(exc: Exception, *, json_mode: bool, console, logger) -> str | None:
    """Turn an exception from one-shot `run_turn` into a message, or report it directly.

    Returns None when the error was already displayed (the non-JSON Gemini path prints its own
    message), which the caller treats as "exit 1, nothing further to report".

    Ordering mirrors the original except-clause chain: auth, then HTTP status, then Gemini's own
    error shape, then the catch-all. The difference is that the provider SDK modules are inspected
    only here, after a failure, instead of being imported up front on every run.
    """
    auth_types = _auth_errors()
    if auth_types and isinstance(exc, auth_types):
        logger.error("Authentication failed: %s", exc)
        return "Authentication failed. Run 'myriaxon --reauth' to update your API key."

    rate_limit_types = _rate_limit_errors()
    if rate_limit_types and isinstance(exc, rate_limit_types):
        logger.warning("Rate limited: %s", exc)
        return "Rate limited by the API. Try again shortly."

    connection_types = _connection_errors()
    if connection_types and isinstance(exc, connection_types):
        logger.error("Connection error: %s", exc)
        return f"Could not reach the API endpoint: {exc}" + _ollama_hint_suffix(str(exc), connection=True)

    status_types = _status_errors()
    if status_types and isinstance(exc, status_types):
        logger.error("API error (%s): %s", exc.status_code, exc.message)
        return f"API error ({exc.status_code}): {exc.message}" + _ollama_hint_suffix(exc.message)

    gemini_types = _gemini_api_errors()
    if gemini_types and isinstance(exc, gemini_types):
        if json_mode:
            return f"API error ({getattr(exc, 'code', None)}): {getattr(exc, 'message', exc)}"
        _handle_gemini_error(exc, console)
        return None

    # A conversation that cannot fit the model's window is a condition, not a defect, and the harness
    # already knows exactly what it is -- `apply_context_action` raises with the numbers and the
    # options. Left to the catch-all below, the user got "Unexpected error -- details logged to
    # <path>" and a log file, which names neither the cause nor the fix. Measured on a 4,096-token
    # Ollama model: the prompt alone is 4,758 tokens, so every one-shot `-p` run said that.
    #
    # The local-model case is called out separately because its fix is different: "narrow the task"
    # is useless advice when the system prompt ALONE is over the window, and `myriaxon ollama --create`
    # is the thing that actually helps.
    context_types = _context_exhausted_errors()
    if context_types and isinstance(exc, context_types):
        logger.warning("Context exhausted: %s", exc)
        message = str(exc)
        hint = _ollama_hint_suffix(message)
        if not hint and "of 4,096 tokens" in message:
            hint = (" This model's window is smaller than Myriaxon's own prompt; no task will fit. "
                    "Build one with the full window:  myriaxon ollama --create")
        return message + hint

    logger.exception("Unhandled error during one-shot run_turn")
    return f"Unexpected error -- details logged to {LOG_FILE}"


_TIMING_ENABLED = os.environ.get("MYRIAXON_TIMING", "").strip() in ("1", "true", "yes")
_timing_start = _time.perf_counter()
_timing_marks: list = []


def _mark(label: str) -> None:
    """Record a startup phase boundary. Off unless MYRIAXON_TIMING is set.

    Permanent instrumentation rather than a temporary probe: "why is this slow" is a question that
    recurs on every new machine and credential type, and a harness that cannot answer it about itself
    forces the user to guess. Costs one dict append when enabled and one environment lookup when not.
    """
    if _TIMING_ENABLED:
        _timing_marks.append((label, _time.perf_counter() - _timing_start))


def _print_timing() -> None:
    if not _TIMING_ENABLED or not _timing_marks:
        return
    import sys as _sys

    _sys.stderr.write("\n--- Myriaxon timing (seconds since process start) ---\n")
    previous = 0.0
    for label, value in _timing_marks:
        _sys.stderr.write(f"  {label:<34} {value:8.3f}  (+{value - previous:.3f})\n")
        previous = value
    _sys.stderr.write(f"  {'TOTAL':<34} {previous:8.3f}\n")


def _build_agent(
    config: Config,
    model: str,
    console,
    yolo: bool,
    mcp_bridge,
    mcp_tool_defs,
    url_mcp_servers,
    discovered_skills,
    discovered_subagent_types,
    loaded_hooks,
    project_instructions,
    scheduler,
    settings,
    max_tokens: int | None = None,
    effort: str | None = None,
    thinking: bool = False,
    mode: str = DEFAULT_MODE,
    no_compact: bool = False,
    initial_messages: list[dict] | None = None,
    profile: str | None = None,
    agent_mode: str = "single",
):
    if settings.enforced_mode and mode != settings.enforced_mode:
        console.print(
            f"[warning]Mode locked to '{settings.enforced_mode}' by organization policy "
            f"(settings.json) -- ignoring requested mode '{mode}'.[/]"
        )
        mode = settings.enforced_mode

    # Resolve the model profile before any client is built: it decides the prompt, the tool set, the
    # result budget and the context thresholds, so it has to be known before the agent exists.
    # `max_input_tokens` is filled in per provider from the live model lookup below and cannot be
    # known yet, so the profile is resolved from the model id first and refined after the lookup.
    #
    # A local Ollama model is asked about itself here: the window Ollama will really run it with (the full window for a
    # model made by `myriaxon ollama --create`, 4,096 for a model nobody configured) and its size, because a model
    # called `myriaxon` does not say that it is a 9B and the rules about small models go by size.
    ollama_window, ollama_size = 0, ""
    if config.provider == PROVIDER_OLLAMA:
        from . import ollama as ollama_module

        ollama_window, ollama_size = ollama_module.agent_facts(config.base_url, model)
    resolved_profile = detect_profile(f"{model} {ollama_size}".strip(), explicit=profile, agent_mode=agent_mode)

    if config.provider == PROVIDER_GEMINI:
        genai, _ = _get_genai()
        if genai is None:
            console.print(
                "[error]Provider is 'gemini' but the 'google-genai' package isn't installed. "
                f"Install with: {escape('pip install -e .[gemini]')}[/]"
            )
            return None
        from .gemini_agent import GeminiAgent

        if config.vertex_project:
            # Vertex mode -- same google-genai Client, constructed against a GCP project/location
            # instead of an API-key + generativelanguage.googleapis.com endpoint. The SDK falls back
            # to Application Default Credentials (gcloud ADC / workforce federation /
            # GOOGLE_APPLICATION_CREDENTIALS / GCE metadata), which is how a corporate account signs
            # in without an API key.
            #
            # api_key is deliberately NOT passed. Measured live against a Workforce-Identity-federated
            # account: passing a key here produced
            #   401 "API keys are not supported by this API. Expected OAuth2 access token..."
            # because the SDK then preferred the key over the OAuth credentials. A stored key from a
            # *different* provider (e.g. a DeepSeek key still in ~/.myriaxon/config.json) was enough
            # to trigger it, so this is a realistic configuration, not a contrived one.
            if config.api_key:
                console.print(
                    "[dim]Vertex mode: using your Google Cloud credentials; the stored API key is "
                    "ignored (Vertex authenticates with OAuth, not API keys).[/]"
                )
            client = _get_genai_client(genai, config)
        else:
            client = _get_genai_client(genai, config)
        # Vertex's model catalog/resource-name shape may differ from the direct Gemini API's --
        # unverified whether client.models.get/list behave identically there. Both
        # fetch_model_limits and list_models never raise, so this degrades to safe defaults
        # (None limits / empty list) rather than crashing if Vertex's shape doesn't match.
        limits = _fetch_model_limits_bounded(PROVIDER_GEMINI, client, model)
        effective_max_tokens = next(
            (v for v in (max_tokens, limits.max_output_tokens, DEFAULT_MAX_TOKENS) if v),
            DEFAULT_MAX_TOKENS,
        )
        resolved_profile = _refine_profile(resolved_profile, limits, profile, agent_mode)
        return GeminiAgent(
            client,
            model,
            console,
            yolo,
            max_tokens=effective_max_tokens,
            mcp_tool_defs=mcp_tool_defs,
            skills=discovered_skills,
            mcp_bridge=mcp_bridge,
            effort=effort,
            # Says whether a `cached_content` reference is meaningful at all: explicit context caching
            # exists only on Vertex, so a direct-API run must not spend a lookup per turn discovering
            # that. Read from the config rather than the environment so it agrees with the client the
            # config produced -- the two cannot disagree about which backend is in use.
            vertex_project=getattr(config, "vertex_project", "") or "",
            thinking=thinking,
            max_input_tokens=limits.max_input_tokens,
            mode=mode,
            initial_messages=initial_messages,
            subagent_types=discovered_subagent_types,
            hooks=loaded_hooks,
            project_instructions=project_instructions,
            scheduler=scheduler,
            settings=settings,
            fallback_model=config.fallback_model,
            profile=resolved_profile,
            protocol=getattr(config, "gemini_protocol", "auto"),
        )

    if config.provider in (
        PROVIDER_OPENAI_COMPAT,
        PROVIDER_DEEPSEEK,
        PROVIDER_OPENROUTER,
        PROVIDER_NETRA,
        # Ollama speaks the same OpenAI-compatible protocol at `<host>/v1`, so it reuses this agent
        # rather than needing a fourth implementation. Leaving it out of this tuple was a real bug:
        # the request fell through to the Anthropic branch, which posted to the base URL without the
        # `/v1` prefix, and Ollama answered `404 page not found` -- an error that names neither the
        # provider nor the URL and reads as the model being missing.
        PROVIDER_OLLAMA,
    ):
        from .openai_agent import OpenAIAgent

        # Direct HTTP by default; see _make_openai_compat_client for the measured reasoning and the
        # MYRIAXON_OPENAI_SDK escape hatch.
        client = _make_openai_compat_client(config.provider, config.api_key, config.base_url)
        if client is None:
            console.print(
                f"[error]Provider is '{config.provider}' but neither the built-in HTTP client nor "
                f"the 'openai' package is available. Install with: {escape('pip install -e .[openai]')}[/]"
            )
            return None
        limits = (ModelLimits(ollama_window, None) if ollama_window
                  else _fetch_model_limits_bounded(config.provider, client, model))
        # The low 4096 default exists only for OpenRouter, which pre-checks max_tokens against your
        # credit balance and rejects the whole request if the worst case isn't affordable. Netra,
        # DeepSeek and a local Ollama do no such check, so capping them at 4096 is not caution -- it
        # is a broken turn. Measured: a Terminal-Bench task on `deepseek-v4.1-flash` spent the whole
        # 4096 on reasoning, emitted no content and no tool call, and the task scored zero with no
        # error reported anywhere.
        #
        # `DEFAULT_MAX_TOKENS` for providers without a credit pre-check, and the floor only where the
        # floor has a purpose.
        if config.provider == PROVIDER_OLLAMA:
            fallback_max_tokens = limits.max_output_tokens or DEFAULT_MAX_TOKENS
        elif config.provider in (PROVIDER_DEEPSEEK, PROVIDER_NETRA, PROVIDER_OPENAI_COMPAT):
            fallback_max_tokens = DEFAULT_MAX_TOKENS
        else:
            fallback_max_tokens = DEFAULT_MAX_TOKENS_OPENAI_COMPAT
        effective_max_tokens = next(
            (v for v in (max_tokens, limits.max_output_tokens, fallback_max_tokens) if v),
            fallback_max_tokens,
        )
        resolved_profile = _refine_profile(resolved_profile, limits, profile, agent_mode)
        # url_servers (Anthropic's remote MCP connector) has no equivalent here -- deliberately
        # not passed through; web_search/web_fetch are likewise Anthropic-only and simply absent
        # from OpenAIAgent's tool set. No auto-compaction either -- manual /compact only.
        return OpenAIAgent(
            client,
            model,
            console,
            yolo,
            max_tokens=effective_max_tokens,
            mcp_tool_defs=mcp_tool_defs,
            skills=discovered_skills,
            mcp_bridge=mcp_bridge,
            effort=effort,
            thinking=thinking,
            max_input_tokens=limits.max_input_tokens,
            mode=mode,
            initial_messages=initial_messages,
            subagent_types=discovered_subagent_types,
            hooks=loaded_hooks,
            project_instructions=project_instructions,
            scheduler=scheduler,
            provider=config.provider,
            settings=settings,
            fallback_model=config.fallback_model,
            profile=resolved_profile,
        )

    client = _get_anthropic().Anthropic(api_key=config.api_key, base_url=config.base_url)
    limits = _fetch_model_limits_bounded(PROVIDER_ANTHROPIC, client, model)
    effective_max_tokens = next(
        (v for v in (max_tokens, limits.max_output_tokens, DEFAULT_MAX_TOKENS) if v),
        DEFAULT_MAX_TOKENS,
    )
    resolved_profile = _refine_profile(resolved_profile, limits, profile, agent_mode)
    return Agent(
        client,
        model,
        console,
        yolo,
        max_tokens=effective_max_tokens,
        mcp_tool_defs=mcp_tool_defs,
        url_mcp_servers=url_mcp_servers,
        skills=discovered_skills,
        mcp_bridge=mcp_bridge,
        effort=effort,
        thinking=thinking,
        max_input_tokens=limits.max_input_tokens,
        mode=mode,
        compact=not no_compact,
        initial_messages=initial_messages,
        subagent_types=discovered_subagent_types,
        hooks=loaded_hooks,
        project_instructions=project_instructions,
        scheduler=scheduler,
        settings=settings,
        fallback_model=config.fallback_model,
        profile=resolved_profile,
    )


def _refine_profile(profile, limits, explicit: str | None, agent_mode: str):
    """Fold the provider's real context window into a profile chosen from the model id alone.

    The live lookup is the only authoritative source for the window, and it is what actually protects
    the user: a model id that matched nothing still gets a correct ceiling once `limits` resolves.
    An explicit `--profile` keeps its thresholds and only takes the window and derived result sizes,
    because a user who asked for `small` wants small-model behavior even on a big-window model.
    """
    window = getattr(limits, "max_input_tokens", None)
    if not window:
        return profile
    return detect_profile(
        "",
        explicit=explicit,
        base=getattr(profile, "name", None),
        max_input_tokens=window,
        agent_mode=agent_mode,
        detection="known",
    )


def _status_line(agent, config: Config):
    """The per-turn bottom toolbar: a list of (style, text) fragments.

    Styling is carried as prompt_toolkit style class names rather than as Rich markup, and that is a
    fix rather than a preference. The text used to embed markup -- `ctx [success]7,657/128,000 (9%)[/]`
    -- which Rich interprets in a `console.print` and prompt_toolkit does not interpret at all, so the
    toolbar displayed the tags themselves. Measured in a real session: the bottom of the terminal read
    `ctx [success]7,657/128,000 (9%)[/] [success]â–ˆâ–ˆâ–ˆâ–ˆâ–‘â–‘â–‘â–‘[/]`. The same class of bug as the prompt's
    ANSI escapes: a string styled for one renderer, handed to another.

    Changed deliberately from a lifetime-token counter to a *context* meter. `input+output` over a
    whole session is a spend figure; what a user needs to see while working is how much of the
    window is still available, because that is the number that decides whether their next request
    succeeds. When the window is unknown the meter is omitted rather than faked -- a made-up
    percentage is worse than no percentage.
    """
    parts: list[tuple[str, str]] = [
        ("class:status", f"{config.provider} | {agent.model}")
    ]
    profile = getattr(agent, "profile", None)
    if profile is not None:
        parts.append(("class:status", f" | {profile.name}"))
    tail = f" | mode={agent.mode}"
    if agent.yolo:
        tail += " | yolo"

    state = getattr(getattr(agent, "tool_context", None), "budget_state", None)
    if state is not None:
        # Colour thresholds mirror the action thresholds so the bar turns at the same moment the
        # harness starts pruning -- the UI and the behaviour agree by construction.
        cls = "status.ok" if state.used_fraction < 0.6 else (
            "status.warn" if state.used_fraction < 0.85 else "status.bad")
        tail += f" | ctx {state.used_tokens:,}/{state.window:,} ({state.percent}%) "
        parts.append(("class:status", tail))
        parts.append((f"class:{cls}", f"{state.meter()}"))
        tail = ""
    parts.append(("class:status", tail))
    totals = agent.usage_totals
    if totals.input_tokens or totals.output_tokens:
        parts.append(("class:status", f" | used {totals.input_tokens + totals.output_tokens:,} tok"))
    # The character's mood rides the status line as TEXT, which is the channel that survives a
    # screen reader, a log file and a colourblind reader alike.
    try:
        mood = character_module.for_agent(agent).status_fragment()
        if mood:
            parts.append(("class:status", f" | {mood}"))
    except Exception:  # noqa: BLE001 - a status line is not worth an exception
        pass
    return parts


_HELP_TEXT = """\
Myriaxon is a terminal coding agent -- type a message to chat with it, or a
/command for direct control. Most work happens through plain conversation;
the model reads/writes files, runs commands, and uses skills on its own.

Tool confirmation: write_file, edit_file, run_bash, and edit_notebook ask
before taking effect, unless auto-approved by mode/yolo/settings.json policy.

Bash-mode: prefix a line with ! to run it directly as a shell command (still
gated by mode/yolo/settings.json, same as run_bash) without spending a model
turn on something you already know is just a shell command.

CONVERSATION & CONTEXT
  /clear                    Clear conversation history and the todo list.
  /compact                  Summarize everything except the last few turns
                             to free up context (also happens automatically
                             near the context limit, except on Anthropic).
  /context                  Show a breakdown of what's consuming context.
  /todos                    Show the current task checklist.
  /export [path]            Export the transcript to a Markdown file.

MODEL & PROVIDER
  /model [name|list]        Show or switch the active model; "list" shows
                             what's available for the active provider.
  /models                   List models available for the active provider.
                            On Ollama this lists the models on this machine,
                            with the window each one really runs with.
  /vendor [deepseek|netra|ollama]
                            Switch between the three gateways this build
                            targets, reusing a key already in .env or already
                            saved -- no pasting it again. Saved for next run.
                            No argument shows which vendor is active.
  /provider [name]          Switch provider for this session (anthropic,
                             openai_compat, openrouter, gemini, deepseek).
                             No argument opens an arrow-key picker.
  /reauth                   Re-run API key/endpoint setup for this session
                             only (not saved to config.json).
  /fallback-model [name|none]
                             Set/clear the model to auto-retry on rate limits.
  /cost                     Show token usage for this session.

PERMISSIONS & GOVERNANCE
  /mode [manual|auto-edit|plan|auto]
                             Show or set the permission mode.
  /yolo                     Toggle auto-accepting all confirmations.
  /profile [name]            Apply a named mode+yolo bundle (built-in:
                             fast, careful -- more definable in settings.json).
  /policy                   Show the active layered settings.json policy
                             (tool permissions, enforced provider/mode/sandbox).
  /sandbox [on|off]          Toggle Docker/Windows-Sandbox-isolated run_bash.

CUSTOMIZATION
  /theme [default|high-contrast|mono]
                             Switch color theme (saved for future sessions).
  /effort [low|medium|high|xhigh|max|none]
                             Show or set the reasoning/response effort level.
  /thinking                  Toggle showing the model's reasoning trace.
  /max-tokens [n]            Show or set the response token ceiling.

MEMORY & DISCOVERY
  /memory search <query>     Full-text search past sessions, any project.
  /commands                  List discovered custom slash commands.

RELIABILITY & TRUST
  /undo                      Restore the most recent file checkpoint directly
                             -- no need to convince the model to fix its own
                             mistake.
  /rewind [id]               List whole-project checkpoints (shadow git), or
                             revert every tracked file AND the conversation to
                             just before a given checkpoint's turn. Needs git
                             on PATH; doesn't delete files created since.
  /diff                      Show a unified diff of every file changed since
                             this process started.
  /retry                     Re-send your last message as a fresh turn (e.g.
                             after a weak model visibly botched a tool call).
  /trace                     Toggle showing every tool call's raw arguments
                             and result inline, for verifying what actually
                             happened.
  /doctor                    One-shot health check: provider/model, SDK
                             install status, sandbox availability, active
                             settings.json layers, checkpoints directory.
  /mcp [list|list-available|search <query>|install <name>|remove <name>]
                             Manage MCP servers (~/.myriaxon/mcp_servers.json).
                             list shows what's configured; list-available
                             shows the built-in installable servers; search/
                             install also fall back to the live official MCP
                             registry beyond that list. Changes need a
                             restart to take effect.
  /vim                       Toggle vi-style input-line editing (persisted).
  /shells                    List background processes started by run_bash.
  /plugins                   Show which plugins loaded, what tools and hooks
                             they contributed, and why any failed to load.
  /loci [list]               Persistent agents: identities that keep their role,
                             instructions and objectives across sessions.
    /loci create <name> [--role R] [--goal "..."] [--instructions "..."]
                        [--proactive] [--every SECONDS]
    /loci show <name>        Full state: objectives, notes, workspace, activity.
    /loci chat <name>        Talk to it continuously -- everything you type goes
                             to that locus until /loci off. Keeps one conversation.
    /loci off                Back to Myriaxon.
    /loci run <name> [msg]   Run one turn as that agent now (a fresh agent each
                             time; use chat for a continuing discussion).
    /loci goal <name> "..."  Add an objective.
    /loci goal <name> <id> --done|--active|--blocked [--note "why"]
    /loci enable|disable <name>
    /loci proactive <name> on|off
    /loci rm <name>          Delete it (confirmed).
                             A proactive locus advances its own objectives while a
                             session is open; its writes still obey your mode.
  /ground                    Toggle a soft warning when a reply mentions
                             files no tool call this turn actually touched.
                             Off by default -- a conservative heuristic, not
                             a guarantee.

SESSION
  exit, quit                 Leave Myriaxon.

Anything not listed here (running commands, editing files, exploring the
codebase, delegating to sub-agents) happens by just asking in plain text."""


def repl(agent, console, config: Config, rebuild_fn, custom_commands, settings) -> None:
    # prompt_toolkit costs real, measured startup time (~6.5s in this environment, confirmed via
    # `python -X importtime`) and is only ever needed once the interactive REPL actually starts
    # -- --version/--reauth/-p one-shot mode never reach this function, so importing it at
    # cli.py's module load unconditionally penalized every invocation, not just interactive ones.
    from prompt_toolkit.enums import EditingMode
    from prompt_toolkit.patch_stdout import patch_stdout

    from .completion import build_prompt_session

    session = build_prompt_session(custom_commands)
    if session is not None and settings.vim_mode:
        session.editing_mode = EditingMode.VI
    session_start_time = _time.time()
    last_user_text: str | None = None

    # Install the compaction recorder for this session. Compaction is invoked from five places -- the three
    # agent backends, model_adapt, and this file -- so it is installed once here rather than threaded through
    # each call site, and every one of them picks it up through `compaction_audit.active()`.
    #
    # Without a recorder, compaction silently discards tool output and nobody can establish what the agent saw.
    # `MYRIAXON_COMPACTION_AUDIT=0` turns it off for anyone who wants no file writes; it writes only under the
    # config directory, never into the workspace.
    if os.environ.get("MYRIAXON_COMPACTION_AUDIT", "1") not in ("0", "false", "no"):
        try:
            from . import compaction_audit as _compaction_audit

            _session_name = _time.strftime("%Y%m%dT%H%M%S", _time.localtime(session_start_time))
            _compaction_audit.set_active(
                _compaction_audit.Recorder(session=_session_name, enabled=True)
            )
        except Exception:
            # Auditing must never stop a session from starting. A recorder that cannot be installed is a
            # missing log, not a broken REPL.
            logger.exception("Could not install the compaction recorder; continuing without one.")

    while True:
        if agent.scheduler is not None:
            while True:
                due_prompt = agent.scheduler.pop_due_prompt()
                if due_prompt is None:
                    break
                console.print(f"[dim]-- firing scheduled prompt: {due_prompt!r}[/]")
                last_user_text = commands.resolve_input(due_prompt, custom_commands)
                _run_turn_safely(agent, last_user_text, console)

        # Proactive loci. Checked at the top of every prompt, exactly like a scheduled prompt:
        # a due locus becomes an ordinary turn, so it passes through the same permission layer,
        # the same budget checks and the same error handling as anything the user types. A locus
        # that interrupted mid-turn would need a second concurrency model; this does not, and
        # "it works while you are using the tool" is the honest description of the feature.
        #
        # The turn itself goes through `locus_run`, so the run slot, the cross-process lease, the
        # preflight refusal and the retry policy all apply here without being repeated. The only
        # thing this loop adds is the *next* step: a locus with `max_steps_per_run` above 1 keeps
        # going until its budget is spent, its objective is done, or a step fails.
        if agent.tool_context is not None:
            try:
                from . import loci as _loci

                for _dot in _loci.get_registry().due():
                    # `take_run` claims the goal and returns it with its prompt together. The previous
                    # `next_goal()` + `proactive_prompt()` pair could disagree, and since the prompt call is
                    # what claims the goal, a caller that inspected the prompt first silently consumed the goal.
                    _taken = _dot.take_run()
                    if _taken is None:
                        continue
                    _goal, _first_prompt = _taken
                    console.print(
                        f"[dim]-- {escape(_dot.name)} is working on: {escape(_goal.title)}[/]"
                    )
                    for _step in range(1, _dot.step_budget() + 1):
                        _prompt = _first_prompt if _step == 1 else _dot.continuation_prompt(_step)
                        if not _prompt:
                            break
                        try:
                            _text, _failed = tool_registry.dispatch_tool(
                                "locus_run",
                                {"locus": _dot.name, "message": _prompt, "_proactive": True},
                                agent.tool_context,
                            )
                        except Exception as exc:  # noqa: BLE001
                            logger.exception("Proactive locus %s failed", _dot.name)
                            _dot.log("error", str(exc))
                            break
                        console.print(_text if not _failed else f"[error]{escape(_text)}[/]")
                        if _failed:
                            # A failed step is already recorded by locus_run with its real reason.
                            # Continuing would spend more tokens on the same obstacle.
                            break
                    _loci.get_registry().touch(_dot)
            except Exception:
                # Autonomy must never be able to stop the user's own session. A broken locus is
                # logged and skipped; the prompt still appears.
                logger.exception("Proactive locus pass failed; continuing.")

        # The prompt names who is going to answer, and nothing else. It used to prepend the mini
        # character, which came back as a string with its own ANSI escapes -- shown literally by
        # prompt_toolkit as a cyan `^[[34m(_â€¿_)[â—]^[[0m` before every line typed, because raw escapes
        # are display text to a prompt that renders styling through its own Style. A name is also the
        # more useful answer to "who is speaking": the face's mood still rides the status line below.
        # A locus conversation still names the locus, so the two are never confused.
        active = getattr(getattr(agent, "tool_context", None), "active_dot", "")
        prompt_text = f"{active} > " if active else "Myria > "
        prompt_style = "class:prompt.locus" if active else "class:prompt"

        # The character is still ticked, because the tick is what decays a held expression between
        # prompts -- the face is drawn in the status line, not here.
        try:
            character_state = character_module.for_agent(agent)
            character_state.tick()
        except Exception:  # noqa: BLE001
            logger.exception("The character tick failed; continuing with the plain prompt.")

        try:
            if session is not None:
                try:
                    with patch_stdout():
                        line = session.prompt(
                            [(prompt_style, prompt_text)],
                            bottom_toolbar=lambda: _status_line(agent, config),
                            # Idle wake-up. Without this, a due Locus fired only when the user pressed Enter,
                            # so "autonomous" meant "autonomous once you interact" -- the exact failure the
                            # Loci design set out to remove. `pre_run` arms a one-shot timer that exits the
                            # prompt when the next Locus comes due, which returns control to the top of this loop
                            # *on the main thread*. That matters: the agent and its ToolContext are only ever
                            # touched from here, so a background thread that ran turns would violate the
                            # invariant the scheduler already documents.
                            pre_run=lambda: _arm_locus_wakeup(session, agent),
                        )
                except (EOFError, KeyboardInterrupt):
                    raise
                except Exception:
                    # The interactive prompt broke mid-session (e.g. terminal state changed
                    # underneath it) -- degrade permanently to plain input rather than
                    # retrying a broken session on every subsequent line.
                    logger.exception("Interactive prompt session failed; falling back to plain input.")
                    session = None
                    line = console.input(f"[prompt.locus]{escape(prompt_text)}[/]" if active
                                         else "[prompt]>[/] ")
            else:
                line = console.input(f"[prompt.locus]{escape(prompt_text)}[/]" if active
                                     else "[prompt]>[/] ")
        except (EOFError, KeyboardInterrupt):
            console.print()
            return

        # The idle wake-up timer exited the prompt with this sentinel, so there is no user input to process.
        # Looping straight back runs the proactive-Locus pass at the top. Returning here rather than falling
        # through is what keeps "no input" from being mistaken for "empty input", which would otherwise be
        # submitted as a blank turn and answered.
        if line == LOCUS_WAKE_SENTINEL:
            continue

        # Strip a leading UTF-8 BOM. `str.strip()` does not remove it, and some terminals and
        # piped input sources prepend one; an unstripped BOM defeats every "/"-prefixed check
        # below, because the line no longer starts with "/", so a slash command is silently sent
        # to the model as chat text instead.
        #
        # Written as the \ufeff escape rather than the literal character. The literal has now been
        # destroyed twice by file-encoding round-trips (it came back as the mojibake "ÃƒÂ¯Ã‚Â»Ã‚Â¿"),
        # which is exactly the failure this escape exists to prevent -- a non-printing character
        # cannot survive being read as Latin-1 and written back as UTF-8, but five ASCII
        # characters can.
        stripped = line.lstrip("\ufeff").strip()
        if not stripped:
            continue
        lowered = stripped.lower()
        if lowered in EXIT_WORDS:
            return

        if stripped.startswith("!") and stripped != "!":
            # Bash-mode shortcut -- runs directly via the same run_bash tool/confirm_action gate
            # the model itself uses (so mode/yolo/settings.json policy still applies), without
            # spending a model turn on something you already know is just a shell command.
            command = stripped[1:].strip()
            if command:
                content, is_error = tool_registry.dispatch_tool(
                    "run_bash", {"command": command}, agent.tool_context
                )
                if is_error:
                    console.print(f"[error]{escape(content)}[/]")
                else:
                    # _run_bash already streamed the command's own output live line-by-line --
                    # only the trailing "Exit code: N" (or, for a backgrounded command, the
                    # whole one-line message) hasn't been shown yet.
                    console.print(f"[dim]{escape(content.rsplit(chr(10), 1)[-1])}[/]")
            continue

        if lowered in ("/help", "/?"):
            # markup=False -- the help text is full of literal [bracketed] placeholders
            # ([path], [on|off], etc.) that Rich's markup parser would otherwise silently
            # swallow as unrecognized style tags (confirmed live: every bracketed usage hint
            # vanished, leaving bare whitespace, until this was added).
            console.print(_HELP_TEXT, markup=False)
            continue

        if lowered == "/clear":
            agent.messages = []
            agent.tool_context.todos.clear()
            console.print("[success]Conversation cleared.[/]")
            continue

        if lowered == "/compact":
            agent.compact()
            continue

        if lowered == "/todos":
            if not agent.tool_context.todos:
                console.print("[dim]No todos yet.[/]")
            else:
                todo.render_todos(agent.tool_context.todos, console)
            continue

        if lowered == "/commands":
            console.print(commands.format_command_listing(custom_commands))
            continue

        if lowered == "/yolo":
            if settings.enforced_mode and not agent.yolo:
                console.print(
                    f"[warning]yolo is disabled by organization policy (mode locked to "
                    f"'{settings.enforced_mode}').[/]"
                )
                continue
            agent.yolo = not agent.yolo
            agent.tool_context.yolo = agent.yolo
            console.print(f"[success]Auto-accept (yolo) is now {'ON' if agent.yolo else 'OFF'}.[/]")
            continue

        if lowered == "/thinking":
            agent.thinking = not agent.thinking
            console.print(f"[success]Thinking display is now {'ON' if agent.thinking else 'OFF'}.[/]")
            continue

        if lowered == "/vim":
            if session is None:
                console.print("[warning]No interactive prompt session available (fell back to plain input) -- nothing to toggle.[/]")
            else:
                is_vi = session.editing_mode == EditingMode.VI
                session.editing_mode = EditingMode.EMACS if is_vi else EditingMode.VI
                new_mode = "EMACS" if is_vi else "VI"
                set_user_vim_mode(not is_vi)
                console.print(f"[success]Input editing mode is now {new_mode} (saved for future sessions).[/]")
            continue

        if lowered == "/ground":
            agent.tool_context.grounding_enabled = not agent.tool_context.grounding_enabled
            state = "ON" if agent.tool_context.grounding_enabled else "OFF"
            console.print(
                f"[success]Grounding check is now {state}.[/] [dim]Conservative heuristic -- flags "
                "file paths the reply mentions that no tool call this turn actually touched. "
                "Expect occasional misses, not false alarms.[/]"
            )
            continue

        if lowered == "/loci" or lowered.startswith("/loci "):
            _run_loci_repl_command(console, stripped.split()[1:], agent)
            continue

        if lowered == "/mission":
            # The dashboard's overview, as text. Wired here because `render_lines()` existed with no
            # caller, so the terminal view the changelog described could not actually be reached.
            # The dashboard's own app object is used when this session came from one; a terminal
            # session gets a shim over the same workspace and config.
            app = getattr(agent.tool_context, "app", None)
            if app is None:
                app = _mission_app(getattr(agent, "config", None))
            _mission_command(app, console)
            continue

        if lowered == "/shells":
            procs = agent.tool_context.background_procs
            if not procs:
                console.print("[dim]No background processes.[/]")
            else:
                for bg_id, bg in procs.items():
                    console.print(escape(f"  {bg_id}  [{bg.status_text()}]  {bg.command}"))
            continue

        if lowered == "/mode" or lowered.startswith("/mode "):
            if settings.enforced_mode:
                console.print(
                    f"[warning]Mode is locked to '{settings.enforced_mode}' by organization "
                    "policy (settings.json) and cannot be changed here.[/]"
                )
                continue
            requested = lowered.removeprefix("/mode").strip()
            if not requested:
                new_mode = _pick(
                    console,
                    "Select a mode",
                    [("manual", "manual"), ("auto_edit", "auto-edit"), ("plan", "plan"), ("auto", "auto")],
                    default=agent.mode,
                )
                if new_mode is None:
                    continue
            else:
                new_mode = _MODE_ALIASES.get(requested)
            if new_mode is None:
                console.print(f"[warning]Usage: /mode manual|auto-edit|plan|auto[/] (current: {agent.mode})")
            else:
                agent.mode = new_mode
                agent.tool_context.mode = new_mode
                console.print(f"[success]Mode is now {new_mode}.[/]")
            continue

        if lowered == "/effort" or lowered.startswith("/effort "):
            requested = lowered.removeprefix("/effort").strip()
            if not requested:
                requested = _pick(
                    console,
                    "Select an effort level",
                    [(e, e) for e in EFFORT_LEVELS] + [("none", "none (provider default)")],
                    default=agent.effort or "none",
                )
                if requested is None:
                    continue
            if requested in ("none", "clear", "default"):
                agent.effort = None
                console.print("[success]Effort reset to provider default.[/]")
            elif requested in EFFORT_LEVELS:
                agent.effort = requested
                console.print(f"[success]Effort is now {requested}.[/]")
            else:
                console.print(
                    f"[warning]Usage: /effort {'|'.join(EFFORT_LEVELS)}|none[/] "
                    f"(current: {agent.effort or '(provider default)'})"
                )
            continue

        if lowered == "/max-tokens" or lowered.startswith("/max-tokens "):
            requested = stripped[len("/max-tokens") :].strip()
            if not requested:
                console.print(f"[dim]Current max-tokens: {agent.max_tokens}[/]")
            elif requested.isdigit() and int(requested) > 0:
                agent.max_tokens = int(requested)
                agent.tool_context.max_tokens = agent.max_tokens
                console.print(f"[success]max-tokens is now {agent.max_tokens}.[/]")
            else:
                console.print(f"[warning]Usage: /max-tokens <positive integer>[/] (current: {agent.max_tokens})")
            continue

        if lowered == "/model" or lowered.startswith("/model "):
            requested = stripped[len("/model") :].strip()
            picked = False
            if not requested:
                # No argument -- arrow-key picker instead of just showing the current model,
                # so choosing a model never requires typing one out by hand.
                picked = True
                if config.provider == PROVIDER_DEEPSEEK:
                    requested = _pick(
                        console, "Select a model", [(m, m) for m in DEEPSEEK_MODELS], default=agent.model
                    )
                    if requested is None:
                        continue
                elif config.provider == PROVIDER_OPENROUTER:
                    model_choice = _pick(
                        console,
                        "Select a model (favorites shown -- OpenRouter has hundreds more, see /models)",
                        [(m, m) for m in OPENROUTER_FAVORITE_MODELS] + [("__other__", "Other (type manually)")],
                        default=agent.model
                        if agent.model in OPENROUTER_FAVORITE_MODELS
                        else OPENROUTER_FAVORITE_MODELS[0],
                    )
                    if model_choice is None:
                        continue
                    if model_choice == "__other__":
                        requested = Prompt.ask("Model", default=agent.model, console=console)
                        picked = False
                    else:
                        requested = model_choice
                else:
                    # No small fixed list for these providers -- offer the real live catalog
                    # instead (same data /models itself shows), falling back to just displaying
                    # the current model if the live fetch is unavailable/empty.
                    available = list_models(config.provider, agent.client)
                    if not available:
                        console.print(f"[dim]Current model: {agent.model}[/]")
                        continue
                    requested = _pick(
                        console,
                        "Select a model",
                        [
                            (m["id"], f"{m['id']}  ({m['display_name']})" if m["display_name"] else m["id"])
                            for m in available
                        ],
                        default=agent.model,
                    )
                    if requested is None:
                        continue

            if requested.lower() in ("list", "ls"):
                # "/model list" reads naturally as "list the models" -- alias it to /models
                # instead of trying (and failing validation) to switch to a model named "list".
                _print_model_list(console, config, agent)
            elif config.provider == PROVIDER_DEEPSEEK and requested not in DEEPSEEK_MODELS:
                # DeepSeek's model set is small and fixed -- the auth wizard and /provider both
                # already restrict it to this same list via select_option; /model was the one
                # place that bypassed validation entirely and accepted any literal string.
                console.print(
                    f"[warning]Unknown DeepSeek model {requested!r}. Supported: "
                    f"{', '.join(DEEPSEEK_MODELS)}[/]"
                )
            else:
                if config.provider != PROVIDER_DEEPSEEK and not picked:
                    # No small fixed list for these providers -- best-effort check against the
                    # real /models endpoint (same call /models itself uses). Never blocks on a
                    # failed/incomplete lookup, since the catalog could be stale or the request
                    # could time out -- only warns, doesn't refuse, if it disagrees. Skipped when
                    # the value came from the picker above -- it's already a real, live-fetched
                    # id, so re-checking it would just be a second, redundant API call.
                    available_ids = {m["id"] for m in list_models(config.provider, agent.client)}
                    if available_ids and requested not in available_ids:
                        console.print(
                            f"[warning]{requested!r} doesn't match any model from /models -- "
                            "setting it anyway, but double-check the name (run /models to see "
                            "valid options).[/]"
                        )
                agent.model = requested
                agent.tool_context.model = requested
                limits = _fetch_model_limits_bounded(config.provider, agent.client, requested)
                if limits.max_input_tokens:
                    agent.max_input_tokens = limits.max_input_tokens
                console.print(f"[success]Model is now {requested}.[/]")
            continue

        if lowered == "/models":
            _print_model_list(console, config, agent)
            continue

        if lowered == "/cost":
            console.print(usage.format_usage_summary(agent.usage_totals, config.provider, agent.model))
            continue

        if lowered == "/context":
            buckets = analyze_context_usage(agent.messages)
            system_prompt_len = len(agent._system_prompt())
            tools_len = len(str(agent.tools))
            # The profile-aware reading is printed first because it is the one that decides
            # behaviour: it is what the harness compares against its thresholds, and it counts the
            # system prompt and tool schemas that the plain message breakdown omits entirely.
            profile = getattr(agent, "profile", None)
            if profile is not None:
                state = getattr(agent.tool_context, "budget_state", None) or model_adapt.budget_for_agent(
                    agent, system_prompt=agent._system_prompt(), tools_chars=tools_len
                )
                console.print(f"[bold]Profile[/]  {escape(describe_profile(profile))}")
                if state is not None:
                    console.print(
                        f"[bold]Context[/]  {state.used_tokens:,}/{state.window:,} tokens "
                        f"({state.percent}%) {state.meter()}  action=[bold]{state.action}[/]  "
                        f"estimate={state.confidence}"
                    )
                    console.print(
                        f"[bold]Headroom[/] {state.headroom_tokens:,} tokens "
                        f"({state.used_fraction:.1%} of the window is in use, reply reserve included)"
                    )
                    if state.breakdown:
                        ordered = sorted(state.breakdown.items(), key=lambda kv: kv[1], reverse=True)
                        console.print(
                            "           "
                            + "  ".join(f"{k}={v:,}" for k, v in ordered if v)
                        )
                    if profile.disabled_tools:
                        console.print(f"[bold]Withheld[/] {', '.join(profile.disabled_tools)} [dim](context saving)[/]")
                console.print()
            console.print(format_context_breakdown(buckets, system_prompt_len, tools_len, agent.max_input_tokens))
            continue

        if lowered == "/policy":
            console.print(format_settings_summary(settings))
            continue

        if lowered == "/sandbox" or lowered.startswith("/sandbox "):
            requested = lowered.removeprefix("/sandbox").strip()
            if not requested:
                requested = _pick(
                    console,
                    "Sandbox",
                    [("on", "on"), ("off", "off")],
                    default="on" if agent.tool_context.sandbox_enabled else "off",
                )
                if requested is None:
                    continue
            if requested not in ("on", "off"):
                console.print(
                    f"[dim]Sandbox is {'ON' if agent.tool_context.sandbox_enabled else 'OFF'}. "
                    "Usage: /sandbox on|off[/]"
                )
            elif requested == "off" and settings.enforced_sandbox:
                console.print("[warning]Sandbox is mandatory by organization policy -- cannot turn it off.[/]")
            else:
                agent.tool_context.sandbox_enabled = requested == "on"
                console.print(f"[success]Sandbox is now {requested.upper()}.[/]")
            continue

        if lowered == "/mcp" or lowered.startswith("/mcp "):
            requested = stripped[len("/mcp") :].strip()
            parts = requested.split(maxsplit=1)
            subcmd = parts[0].lower() if parts else "list"
            rest = parts[1] if len(parts) > 1 else ""

            if subcmd in ("", "list"):
                _print_mcp_list(console, agent)
            elif subcmd in ("list-available", "available"):
                _print_available_mcp_servers(console)
            elif subcmd == "search":
                _search_mcp_registry(console, rest)
            elif subcmd == "install":
                _install_mcp_server(console, rest)
            elif subcmd in ("remove", "rm"):
                if not rest.strip():
                    console.print("[warning]Usage: /mcp remove <name>[/]")
                elif remove_server(rest.strip()):
                    console.print(
                        f"[success]Removed {rest.strip()!r} from {MCP_SERVERS_FILE}. Restart Myriaxon "
                        "for this to take effect.[/]"
                    )
                else:
                    console.print(f"[warning]No MCP server named {rest.strip()!r} found in config.[/]")
            else:
                console.print(
                    "[warning]Usage: /mcp [list|list-available|search <query>|install <name>|remove <name>][/]"
                )
            continue

        if lowered == "/memory" or lowered.startswith("/memory "):
            requested = stripped[len("/memory") :].strip()
            if requested.startswith("search "):
                query = requested[len("search ") :].strip()
                console.print(format_memory_results(search_memory(query)))
            else:
                console.print("[warning]Usage: /memory search <query>[/]")
            continue

        if lowered == "/profile" or lowered.startswith("/profile "):
            requested = stripped[len("/profile") :].strip()
            if not requested:
                requested = _pick(console, "Select a profile", [(p, p) for p in sorted(settings.profiles)])
                if requested is None:
                    continue
            if requested not in settings.profiles:
                console.print(
                    f"[warning]Unknown profile {requested!r}. Available: "
                    f"{', '.join(sorted(settings.profiles))}[/]"
                )
            else:
                cfg = settings.profiles[requested]
                requested_mode = cfg.get("mode")
                if requested_mode:
                    if settings.enforced_mode and requested_mode != settings.enforced_mode:
                        console.print(
                            f"[warning]Profile {requested!r} wants mode '{requested_mode}', but "
                            f"mode is locked to '{settings.enforced_mode}' by policy -- keeping "
                            "the locked mode.[/]"
                        )
                    else:
                        agent.mode = requested_mode
                        agent.tool_context.mode = requested_mode
                if "yolo" in cfg:
                    if settings.enforced_mode and cfg["yolo"]:
                        console.print(
                            "[warning]yolo is disabled by organization policy -- profile's yolo "
                            "setting ignored.[/]"
                        )
                    else:
                        agent.yolo = bool(cfg["yolo"])
                        agent.tool_context.yolo = agent.yolo
                console.print(f"[success]Applied profile {requested!r} (mode={agent.mode}, yolo={agent.yolo}).[/]")
            continue

        if lowered == "/theme" or lowered.startswith("/theme "):
            requested = stripped[len("/theme") :].strip()
            if not requested:
                requested = _pick(console, "Select a theme", [(t, t) for t in sorted(THEMES)])
                if requested is None:
                    continue
            if requested not in THEMES:
                console.print(f"[warning]Unknown theme {requested!r}. Available: {', '.join(sorted(THEMES))}[/]")
            else:
                console.push_theme(THEMES[requested])
                set_user_theme(requested)
                console.print(f"[success]Theme is now {requested} (saved for future sessions).[/]")
            continue

        if lowered == "/export" or lowered.startswith("/export "):
            transcript = render_transcript(agent.messages, config.provider)
            if not transcript.strip():
                console.print("[dim]Nothing to export yet.[/]")
                continue
            requested_path = stripped[len("/export") :].strip() or f"myriaxon-session-{int(_time.time())}.md"
            try:
                Path(requested_path).write_text(transcript, encoding="utf-8")
                console.print(f"[success]Exported conversation to {escape(requested_path)}.[/]")
            except OSError as exc:
                console.print(f"[error]Could not write {escape(requested_path)}: {escape(str(exc))}[/]")
            continue

        if lowered == "/fallback-model" or lowered.startswith("/fallback-model "):
            requested = stripped[len("/fallback-model") :].strip()
            if not requested:
                console.print(f"[dim]Current fallback model: {agent.fallback_model or '(none)'}[/]")
            elif requested.lower() in ("none", "clear", "off"):
                agent.fallback_model = ""
                console.print("[success]Fallback model disabled.[/]")
            else:
                agent.fallback_model = requested
                console.print(f"[success]Fallback model is now {requested}.[/]")
            continue

        if lowered == "/reauth":
            new_key = Prompt.ask("API key", password=True, console=console)
            new_endpoint = Prompt.ask("API endpoint", default=config.base_url, console=console)
            # preserve whatever model is actually active (may differ from startup if /model was used)
            config = replace(config, api_key=new_key.strip(), base_url=new_endpoint.strip(), model=agent.model)
            new_agent = rebuild_fn(config, keep_messages=agent.messages)
            if new_agent is not None:
                agent = new_agent
                console.print("[success]Re-authenticated for this session (not saved to config.json).[/]")
            continue

        if lowered == "/vendor" or lowered.startswith("/vendor "):
            # The three gateways this build targets, switchable mid-session: deepseek, netra, ollama.
            # Kept beside `/provider`, which handles the other four and asks for a key every time --
            # /vendor is the short form that reuses a key already in the environment or already saved,
            # so switching back and forth during a session does not mean pasting the same key again.
            if settings.enforced_provider:
                console.print(
                    f"[warning]Provider is locked to '{settings.enforced_provider}' by "
                    "organization policy (settings.json) and cannot be changed here.[/]"
                )
                continue
            requested = lowered.removeprefix("/vendor").strip()
            if not requested:
                console.print(f"[bold]Vendor:[/] {config.provider}:{agent.model}")
                for name, blurb in (
                    (PROVIDER_DEEPSEEK, "DeepSeek's own API (api.deepseek.com)"),
                    (PROVIDER_NETRA, "gateway for the same models (api.netraruntime.com)"),
                    (PROVIDER_OLLAMA, "a model on this computer, no key needed"),
                ):
                    mark = " [success](current)[/]" if config.provider == name else ""
                    console.print(f"  [bold]{name}[/]  {blurb}{mark}")
                console.print("[dim]Use:  /vendor deepseek | /vendor netra | /vendor ollama[/]")
                continue
            new_config, problem = switch_vendor(config, requested)
            if new_config is None:
                console.print(f"[warning]{escape(problem)}[/]")
                continue
            if new_config.provider != PROVIDER_OLLAMA and not new_config.api_key:
                console.print(
                    f"[warning]{escape(requested)} has no API key saved. Run `myriaxon --reauth`, or "
                    f"put {PROVIDER_ENV_KEY_FALLBACK.get(requested, 'the key')}=... in .env.[/]"
                )
                continue
            switched = rebuild_fn(new_config, keep_messages=agent.messages)
            if switched is None:
                console.print(f"[error]Could not build an agent for {escape(requested)}.[/]")
                continue
            agent = switched
            config = new_config
            # Saved, so the next run starts where this one left off. The dashboard's picker saves for
            # the same reason: a choice that does not survive a restart is a choice the user has to
            # make twice.
            try:
                save_config(config)
                saved = "  (saved)"
            except Exception:  # noqa: BLE001 - an unwritable config is not a failed switch
                saved = "  (not saved -- config.json is not writable)"
            if config.provider == PROVIDER_OLLAMA:
                from . import ollama as ollama_module

                for note in ollama_module.startup_notes(config.base_url, agent.model):
                    console.print(f"[warning]{escape(note)}[/]")
            console.print(
                f"[success]Vendor is now {escape(config.provider)}:{escape(agent.model)}[/]{saved}"
            )
            continue

        if lowered == "/provider" or lowered.startswith("/provider "):
            if settings.enforced_provider:
                console.print(
                    f"[warning]Provider is locked to '{settings.enforced_provider}' by "
                    "organization policy (settings.json) and cannot be changed here.[/]"
                )
                continue
            requested = lowered.removeprefix("/provider").strip()
            if not requested:
                # No argument -- arrow-key picker instead of forcing a typed name.
                target_provider = _pick(
                    console,
                    "Select a provider",
                    [
                        (PROVIDER_ANTHROPIC, "Anthropic"),
                        (PROVIDER_OPENAI_COMPAT, "OpenAI-compatible (custom endpoint)"),
                        (PROVIDER_OPENROUTER, "OpenRouter"),
                        (PROVIDER_GEMINI, "Gemini"),
                        (PROVIDER_DEEPSEEK, "DeepSeek"),
                    ],
                    default=config.provider,
                )
                if target_provider is None:
                    continue
            else:
                target_provider = _PROVIDER_ALIASES.get(requested)
            if target_provider is None:
                console.print(
                    "[warning]Usage: /provider [anthropic|openai_compat|openrouter|gemini|deepseek|netra][/] "
                    f"(current: {config.provider}; no argument opens a picker)"
                )
                continue

            env_var_name = PROVIDER_ENV_KEY_FALLBACK.get(target_provider)
            env_value = os.environ.get(env_var_name) if env_var_name else None
            if env_value:
                new_key = Prompt.ask(
                    f"API key (blank = use {env_var_name} from the environment)",
                    password=True,
                    default="",
                    console=console,
                )
                if not new_key:
                    new_key = env_value
            else:
                new_key = Prompt.ask("API key", password=True, console=console)

            if target_provider == PROVIDER_DEEPSEEK:
                new_endpoint = DEFAULT_DEEPSEEK_BASE_URL
                new_model = _pick(
                    console, "Select a model", [(m, m) for m in DEEPSEEK_MODELS], default=DEEPSEEK_MODELS[0]
                )
                if new_model is None:
                    continue
            elif target_provider == PROVIDER_OPENROUTER:
                new_endpoint = DEFAULT_OPENROUTER_BASE_URL
                model_choice = _pick(
                    console,
                    "Select a model (favorites shown -- OpenRouter has hundreds more)",
                    [(m, m) for m in OPENROUTER_FAVORITE_MODELS] + [("__other__", "Other (type manually)")],
                    default=OPENROUTER_FAVORITE_MODELS[0],
                )
                if model_choice is None:
                    continue
                new_model = (
                    Prompt.ask("Model", console=console) if model_choice == "__other__" else model_choice
                )
            else:
                default_endpoint = _PROVIDER_DEFAULT_ENDPOINTS.get(target_provider, "")
                new_endpoint = Prompt.ask(
                    "API endpoint", default=default_endpoint or None, console=console
                )
                new_model = Prompt.ask("Model", console=console)
            config = replace(
                config,
                provider=target_provider,
                api_key=new_key.strip(),
                base_url=new_endpoint.strip(),
                model=new_model.strip(),
            )
            # Hard clear on provider switch -- the two message-history formats aren't compatible.
            # The just-finished session is already saved on disk (every run_turn saves) and stays
            # resumable next launch in this folder even though this live process is moving on.
            new_agent = rebuild_fn(config, keep_messages=None)
            if new_agent is not None:
                agent = new_agent
                console.print(
                    f"[success]Switched to provider {target_provider}. Starting a fresh conversation "
                    "(the previous one is saved and resumable next launch in this folder).[/]"
                )
            continue

        if lowered == "/undo":
            entries = checkpoints.list_checkpoints(limit=1)
            if not entries:
                console.print("[dim]No checkpoints recorded yet -- nothing to undo.[/]")
            else:
                result, is_error = checkpoints.execute_checkpoint_restore(
                    {"id": entries[0]["id"]}, agent.tool_context
                )
                console.print(f"[{'error' if is_error else 'success'}]{escape(result)}[/]")
            continue

        if lowered == "/rewind" or lowered.startswith("/rewind "):
            requested = stripped[len("/rewind") :].strip()
            if not requested:
                _print_rewind_points(console)
            else:
                message, message_count = checkpoints.rewind_to(requested)
                if message_count is None:
                    console.print(f"[warning]{escape(message)}[/]")
                else:
                    agent.messages = agent.messages[:message_count]
                    console.print(
                        f"[success]{escape(message)}[/] [dim]Conversation reverted to just before "
                        "that turn too.[/]"
                    )
            continue

        if lowered == "/retry":
            if last_user_text is None:
                console.print("[dim]Nothing to retry yet.[/]")
            else:
                console.print(f"[dim]Retrying: {escape(last_user_text[:200])}[/]")
                _run_turn_safely(agent, last_user_text, console)
            continue

        if lowered == "/trace":
            agent.tool_context.trace_enabled = not agent.tool_context.trace_enabled
            console.print(
                f"[success]Tool-call tracing is now {'ON' if agent.tool_context.trace_enabled else 'OFF'}.[/]"
            )
            continue

        if lowered == "/diff":
            _show_session_diff(console, session_start_time)
            continue

        if lowered == "/doctor":
            _run_doctor(console, config, agent, settings)
            continue

        if lowered == "/plugins":
            _print_plugins(console)
            continue

        text_to_send = commands.resolve_input(stripped, custom_commands)
        if text_to_send == stripped and is_unknown_slash_command(stripped):
            # Not a built-in, not a custom command: a typo. Say so instead of spending a model turn on it.
            console.print(
                f"[warning]Unknown command {escape(stripped.split(None, 1)[0])}.[/] "
                "Type /help for the list, or /commands for your own."
            )
            continue
        last_user_text = text_to_send
        # A locus set active by `/loci chat` receives the message instead of Myriaxon. Same entry point
        # as every other message, so scheduling, hooks and the transcript behave identically.
        if _send_to_active_dot(agent, text_to_send):
            continue
        _run_turn_safely(agent, text_to_send, console)


def _set_active_dot(console, agent, locus) -> None:
    """Put the REPL into, or out of, a locus's conversation.

    Held on the agent's tool context because that is the one object the input path and the prompt
    both already have. `None` means "talk to Myriaxon", which is the default and the way back.
    """
    if agent is None:
        console.print("[warning]No active agent, so there is nothing to switch.[/]")
        return
    context = getattr(agent, "tool_context", None)
    if context is None:
        console.print("[warning]This session has no tool context to hold the switch.[/]")
        return

    if locus is None:
        if not getattr(context, "active_dot", ""):
            console.print("[dim]Already talking to Myriaxon.[/]")
            return
        previous = context.active_dot
        context.active_dot = ""
        console.print(f"[success]Back to Myriaxon.[/] [dim]{escape(previous)} will keep its "
                      "objectives and wake on its own schedule if it is proactive.[/]")
        return

    if not locus.enabled:
        # Enabled is what `/loci enable` sets, and a disabled locus refuses to run -- so entering a
        # conversation with one would produce a refusal on the very next message. Say so here, where
        # the fix is one command away, rather than at the first thing the user types.
        console.print(f"[warning]{escape(locus.name)} is disabled, so it will not answer.[/] "
                      f"[dim]Run /loci enable {escape(locus.name)} first.[/]")
        return

    context.active_dot = locus.name
    console.print(f"[success]Talking to {escape(locus.name)}[/] "
                  f"[dim]({escape(locus.role or 'agent')})[/]")
    console.print(f"[dim]Everything you type now goes to {escape(locus.name)} and keeps one "
                  f"conversation. /loci off returns to Myriaxon.[/]")
    if locus.goals:
        open_goals = [g for g in locus.goals if getattr(g, "status", "") not in ("done", "cancelled")]
        if open_goals:
            console.print(f"[dim]{len(open_goals)} open objective(s); "
                          f"first: {escape(str(open_goals[0].title)[:80])}[/]")


def _send_to_active_dot(agent, text: str) -> bool:
    """Route one REPL message to the active locus. False when no locus is active.

    Uses `execute_locus_run`, the same path `/loci run` and the `locus_run` tool take, so a message typed
    here gets the identical preflight, lease, retry policy and identity.
    """
    context = getattr(agent, "tool_context", None)
    name = getattr(context, "active_dot", "") if context is not None else ""
    if not name:
        return False
    from . import tool_registry
    from .loci import get_registry

    registry = get_registry()
    registry.reload()
    locus = registry.get(name)
    if locus is None:
        console_note = f"{name} no longer exists; back to Myriaxon."
        getattr(context, "console", None) and context.console.print(f"[warning]{escape(console_note)}[/]")
        context.active_dot = ""
        return True

    result, failed = tool_registry.execute_locus_run(
        {"locus": name, "message": text}, context
    )
    console = getattr(context, "console", None)
    if console is not None:
        console.print(f"[error]{escape(result)}[/]" if failed else result)
    return True


def _print_plugins(console) -> None:
    """`/plugins` -- what loaded, what it contributed, and why anything failed.

    Failures are the point of this command. The loader deliberately skips a broken plugin rather than
    stopping the agent, so without a way to ask, a plugin that silently contributes nothing is
    indistinguishable from one that was never found.
    """
    from .plugins import get_registry

    registry = get_registry()
    summary = registry.summary()
    if not summary["plugins"]:
        console.print("[dim]No plugins loaded. See docs/PLUGINS.md for how to write one.[/]")
        return

    for plugin in summary["plugins"]:
        if plugin["ok"]:
            detail = []
            if plugin["tools"]:
                detail.append(f"{len(plugin['tools'])} tool(s): {', '.join(plugin['tools'])}")
            if plugin["hooks"]:
                detail.append(f"{len(plugin['hooks'])} hook(s)")
            suffix = " -- " + " Â· ".join(detail) if detail else " -- no tools or hooks"
            version = f" v{plugin['version']}" if plugin["version"] else ""
            console.print(f"[success]loaded[/] {escape(plugin['name'])}{escape(version)}{escape(suffix)}")
        else:
            console.print(f"[error]failed[/] {escape(plugin['name'])}: {escape(plugin['error'])}")

    if summary["hooks"]:
        active = ", ".join(f"{event}Ã—{count}" for event, count in summary["hooks"].items())
        console.print(f"[dim]active hooks: {escape(active)}[/]")


def _run_loci_repl_command(console, args: list[str], agent=None) -> None:
    """`/loci` -- manage the persistent agents.

    Named `_run_loci_repl_command` because `_run_loci_command(args, console)`, further down, is the headless
    `myriaxon loci` entry point. The two shared one name after the bulk rename from "dots", and Python keeps only the
    later definition, so the interactive `/loci` command was calling the headless one with its arguments swapped.

    Kept as one handler with a small verb set rather than several slash commands: a locus is a
    single concept with a lifecycle, and splitting `create`/`list`/`rm` across the command
    namespace would make it harder to discover, not easier to use.
    """
    from . import loci

    registry = loci.get_registry()
    verb = (args[0].lower() if args else "list")

    if verb in ("list", "ls", ""):
        console.print(format_loci(registry.all()))
        return

    if verb == "create":
        rest = args[1:]
        if not rest:
            console.print(
                "[warning]Usage: /loci create <name> [--role R] [--goal G] [--instructions I] "
                "[--proactive] [--every SECONDS][/]"
            )
            return
        name = rest[0]
        role = "generalist"
        goals: list[str] = []
        instructions = ""
        proactive = False
        interval = loci.DEFAULT_PROACTIVE_INTERVAL_SECONDS

        index = 1
        while index < len(rest):
            flag = rest[index]
            value = rest[index + 1] if index + 1 < len(rest) else ""
            if flag in ("--role", "--goal", "--instructions", "--every"):
                if not value:
                    console.print(f"[warning]{flag} needs a value.[/]")
                    return
                if flag == "--role":
                    role = value
                elif flag == "--goal":
                    goals.append(value)
                elif flag == "--instructions":
                    instructions = value
                else:
                    try:
                        interval = max(30, int(value))
                    except ValueError:
                        console.print("[warning]--every takes a whole number of seconds.[/]")
                        return
                index += 2
                continue
            if flag == "--proactive":
                proactive = True
                index += 1
                continue
            console.print(f"[warning]Unknown option {escape(flag)}.[/]")
            return

        try:
            locus = registry.create(
                name, role=role, instructions=instructions, goals=goals,
                proactive=proactive, interval_seconds=interval,
            )
        except ValueError as exc:
            console.print(f"[error]{escape(str(exc))}[/]")
            return
        console.print(f"[success]Created[/] {escape(locus.name)} [[{locus.id}]].")
        console.print(loci.format_locus(locus, verbose=True))
        if not goals:
            console.print(
                "[warning]It has no objectives, so it will do nothing until you give it one:[/]"
                f"\n  /loci goal {escape(locus.name)} \"<what it should achieve>\""
            )
        return

    if verb in ("rm", "remove", "delete"):
        if len(args) < 2:
            console.print("[warning]Usage: /loci rm <name>[/]")
            return
        locus = registry.get(args[1])
        if locus is None:
            console.print(f"[error]No locus named {escape(args[1])}.[/]")
            return
        # Deleting removes the identity and its objectives, so it is confirmed through the same
        # path as any other mutating action -- an allow/deny rule can cover `locus_delete`, and Plan
        # mode blocks it. Requiring a live agent context is deliberate: there is no way to reach
        # this command without one, and a fallback context would silently bypass the policy layer.
        if agent is None:
            console.print("[warning]Deleting a locus needs an active agent context.[/]")
            return
        proceed, blocked = tools.confirm_action(
            console,
            _text_panel(f"Delete the locus {locus.name!r} and its {len(locus.goals)} objective(s)?"),
            agent.tool_context,
            "locus_delete",
        )
        if blocked:
            console.print(f"[warning]{escape(blocked)}[/]")
            return
        if not proceed:
            console.print("[dim]Cancelled.[/]")
            return
        registry.delete(locus.id)
        console.print(f"[success]Deleted[/] {escape(locus.name)}.")
        return

    if verb == "show":
        if len(args) < 2:
            console.print("[warning]Usage: /loci show <name>[/]")
            return
        locus = registry.get(args[1])
        if locus is None:
            console.print(f"[error]No locus named {escape(args[1])}.[/]")
            return
        console.print(loci.format_locus(locus, verbose=True))
        return

    if verb == "goal":
        # /loci goal <locus> <title>            -- add
        # /loci goal <locus> <ref> --done|--active|--blocked [--note N]
        if len(args) < 3:
            console.print(
                "[warning]Usage: /loci goal <locus> \"<objective>\"  |  "
                '/loci goal <locus> <id> --done|--active|--blocked [--note "why"][/]'
            )
            return
        locus = registry.get(args[1])
        if locus is None:
            console.print(f"[error]No locus named {escape(args[1])}.[/]")
            return
        status_flag = next((a for a in args[3:] if a.startswith("--")), "")
        note = ""
        if "--note" in args:
            note_index = args.index("--note")
            note = args[note_index + 1] if note_index + 1 < len(args) else ""
        if status_flag:
            target = {"--done": "done", "--active": "active", "--blocked": "blocked", "--pending": "pending"}
            goal = locus.set_goal_status(args[2], target.get(status_flag, ""), note)
            if goal is None:
                console.print(f"[error]No objective matching {escape(args[2])} on {escape(locus.name)}.[/]")
                return
            registry.save()
            console.print(f"[success]{escape(locus.name)}[/]: [{goal.id}] {escape(goal.title)} -> {goal.status}")
        else:
            goal = locus.add_goal(args[2])
            registry.save()
            console.print(f"[success]Added[/] [{goal.id}] {escape(goal.title)} to {escape(locus.name)}.")
        return

    if verb == "chat" or (verb == "off" and len(args) < 2):
        # Talk to a locus continuously, rather than asking it one question at a time.
        #
        # This is what `/loci enable analyst` looked like it should do and did not. `run` spawns a
        # disposable sub-agent per message, so there was no conversation: each question started from
        # nothing, and everything you typed in between went to Myriaxon. The dashboard has had a
        # persistent per-locus conversation for a while, which is why the same locus answers as itself
        # there and as Myriaxon here.
        #
        # When a locus is active, ordinary input is routed to it -- so the transcript reads as one
        # discussion with that locus, and `/loci off` hands the prompt back to Myriaxon.
        #
        # Placed BEFORE the proactive handler because `/loci off` was already a shorthand for
        # `/loci proactive <name> off`, and that handler claims the verb first. The two are
        # distinguished by arity: `/loci off` alone leaves the conversation, `/loci off <name>` keeps
        # its existing meaning of turning proactivity off for that locus. Both are documented.
        if verb == "off" or len(args) < 2 or args[1].lower() in ("off", "none", "-"):
            _set_active_dot(console, agent, None)
            return
        locus = registry.get(args[1])
        if locus is None:
            names = ", ".join(sorted(d.name for d in registry.all())) or "none"
            console.print(f"[error]No locus named {escape(args[1])}.[/] [dim]Available: {escape(names)}[/]")
            return
        _set_active_dot(console, agent, locus)
        return

    if verb in ("on", "off", "proactive"):
        if len(args) < 2:
            console.print("[warning]Usage: /loci proactive <name> on|off[/]")
            return
        locus = registry.get(args[1])
        if locus is None:
            console.print(f"[error]No locus named {escape(args[1])}.[/]")
            return
        target = (args[2].lower() if len(args) > 2 else ("off" if verb == "off" else "on"))
        locus.proactive = target in ("on", "true", "yes", "1")
        locus.log("note", f"proactive {'enabled' if locus.proactive else 'disabled'}")
        registry.save()
        state = "on" if locus.proactive else "off"
        console.print(f"[success]{escape(locus.name)}[/]: proactive {state}")
        if locus.proactive:
            console.print(
                "[dim]It will advance its objectives while a Myriaxon session is open, on the "
                "interval shown by /loci show. Its writes still obey the permission mode.[/]"
            )
        return

    if verb in ("chat", "off"):
        # Talk to a locus continuously, rather than asking it one question at a time.
        #
        # This is what `/loci enable analyst` looked like it should do and did not. `run` spawns a
        # disposable sub-agent per message, so there was no conversation: each question started from
        # nothing, and everything you typed in between went to Myriaxon. The dashboard has had a
        # persistent per-locus conversation for a while, which is why the same locus answers as itself
        # there and as Myriaxon here.
        #
        # When a locus is active, ordinary input is routed to it -- so the transcript reads as one
        # discussion with that locus, and `/loci off` hands the prompt back to Myriaxon.
        #
        # `off` is accepted as a verb in its own right, not only as the first argument to `chat`.
        # The help text and the switch message both say `/loci off`, so if the parser did not accept
        # it the advertised way out of the mode would be the one command that did not work.
        if verb == "off" or len(args) < 2 or args[1].lower() in ("off", "none", "-"):
            _set_active_dot(console, agent, None)
            return
        locus = registry.get(args[1])
        if locus is None:
            names = ", ".join(sorted(d.name for d in registry.all())) or "none"
            console.print(f"[error]No locus named {escape(args[1])}.[/] [dim]Available: {escape(names)}[/]")
            return
        _set_active_dot(console, agent, locus)
        return

    if verb == "run":
        if len(args) < 2:
            console.print("[warning]Usage: /loci run <name> [message][/]")
            return
        if agent is None:
            console.print("[warning]Starting a locus needs an active agent.[/]")
            return
        locus = registry.get(args[1])
        if locus is None:
            console.print(f"[error]No locus named {escape(args[1])}.[/]")
            return
        message = " ".join(args[2:]).strip() or (
            "Take the next concrete step on your first open objective."
        )
        console.print(f"[dim]{escape(locus.name)} is working...[/]")
        from . import tool_registry

        text, failed = tool_registry.execute_locus_run(
            {"locus": locus.name, "message": message}, agent.tool_context
        )
        console.print(f"[error]{escape(text)}[/]" if failed else text)
        return

    if verb in ("enable", "disable"):
        if len(args) < 2:
            console.print(f"[warning]Usage: /loci {verb} <name>[/]")
            return
        locus = registry.get(args[1])
        if locus is None:
            console.print(f"[error]No locus named {escape(args[1])}.[/]")
            return
        wanted = verb == "enable"
        locus.enabled = wanted
        locus.log("note", f"{'enabled' if wanted else 'disabled'} from the terminal")
        registry.persist(locus)
        if wanted:
            console.print(f"[success]{escape(locus.name)} is enabled.[/]")
            # The hint used to say only "message it with /loci run <name> <message>", which is the
            # one-shot form: a fresh sub-agent per message, with no memory between them. Reporting
            # that as the way to talk to a persistent agent was the misleading half -- `/loci chat`
            # is what makes it a conversation.
            console.print(f"[dim]  /loci chat {escape(locus.name)}   talk to it continuously[/]")
            console.print(f"[dim]  /loci run {escape(locus.name)} <msg>   ask it one thing[/]")
        else:
            console.print(f"[success]{escape(locus.name)} is disabled.[/] "
                          "[dim]Nothing will run it until you enable it again.[/]")
            # If the terminal was mid-conversation with it, that conversation is over.
            if getattr(getattr(agent, "tool_context", None), "active_dot", "") == locus.name:
                agent.tool_context.active_dot = ""
                console.print("[dim]Back to Myriaxon.[/]")
        return

    console.print(
        f"[warning]Unknown /loci subcommand {escape(verb)}.[/]\n"
        "[dim]Try: /loci Â· /loci create Â· /loci show Â· /loci run Â· /loci chat Â· /loci off Â· "
        "/loci goal Â· /loci enable|disable Â· /loci proactive Â· /loci rm[/]"
    )


def _run_ollama_command(args, console) -> int:
    """`myriaxon ollama` -- find Ollama on this machine, report its models, build the `myriaxon` model, or switch to one.

    Ollama differs from every other provider in that it needs no key and may not be running at all, so
    "not found" is a normal answer rather than an error. The command therefore reports what it looked
    for and what answered, and says which of the three situations applies -- not installed, installed
    but not serving, serving with nothing pulled -- because those have three different fixes.

    `--use` is what makes the detection usable: without it this would be a report the user reads and
    then has to translate into a flag by hand. `--create` goes one step further and builds the model
    that `ollama run myriaxon` runs, because Ollama on its own gives every model a 4,096-token window.
    """
    import os

    from . import constants, ollama

    if args.host:
        # A one-off probe of a different address. Set in the environment rather than passed down,
        # because `candidate_hosts()` is also used by the status line and both must agree.
        os.environ[ollama.HOST_ENV] = args.host

    wants_details = args.create or args.modelfile or args.remove
    probe = ollama.detect(with_details=wants_details or not args.quick)

    if args.json:
        import json

        payload = {
            "base_url": probe.base_url,
            "reachable": probe.reachable,
            "installed": probe.installed,
            "models": [
                {
                    "name": m.name,
                    "context_length": m.context_length,
                    "num_ctx": m.num_ctx,
                    "usable_context": ollama.runtime_context(m, probe.loaded.get(m.name, 0)),
                    "loaded": m.name in probe.loaded,
                    "parameter_size": m.parameter_size,
                    "family": m.family,
                    "quantisation": m.quantisation,
                    "capabilities": m.capabilities,
                    "supports_tools": ollama.tool_capable(m),
                    "size_bytes": m.size_bytes,
                    "kv_bytes_per_token": m.kv_bytes_per_token,
                }
                for m in probe.models
            ],
        }
        console.print(json.dumps(payload, indent=2), markup=False, highlight=False, soft_wrap=True)
        return 0 if probe.reachable else 1

    if args.remove:
        return _ollama_remove(args, probe, console, ollama)
    if args.create or args.modelfile:
        return _ollama_create(args, probe, console, constants, ollama)

    if args.use:
        return _ollama_switch(args.use, probe, console, constants, ollama)

    for line in ollama.report(with_details=not args.quick):
        console.print(escape(line) if line.strip() else "")
    return 0 if probe.reachable else 1


def _ollama_save(name: str, probe, console, constants) -> None:
    """Point the saved configuration at the Ollama model `name`."""
    import json

    from .config import CONFIG_FILE

    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8")) if CONFIG_FILE.is_file() else {}
    except Exception:  # noqa: BLE001
        data = {}
    if data.get("provider") != constants.PROVIDER_OLLAMA:
        data["fallback_model"] = ""     # a model name from another provider means nothing to Ollama
    data["provider"] = constants.PROVIDER_OLLAMA
    # A placeholder, not a secret: the OpenAI-compatible client rejects an empty key before the request
    # is sent, and Ollama ignores whatever is there. It REPLACES whatever key was saved, never keeps it: a key kept
    # next to an Ollama address would be sent to that address, and for an Ollama on another computer that is a
    # stranger's server.
    data["api_key"] = constants.OLLAMA_PLACEHOLDER_KEY
    data["base_url"] = probe.base_url.rstrip("/") + "/v1"
    data["model"] = name

    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8", newline="\n")
    console.print(f"  saved to  {escape(str(CONFIG_FILE))}")


def _ollama_switch(name: str, probe, console, constants, ollama) -> int:
    """Point the saved configuration at a local model.

    Refuses a name Ollama does not have, and lists what it does. Switching to a model that is not
    pulled would produce a connection error on the next turn, which reads as the harness being broken
    rather than as a typo -- and Ollama's own error for that is not surfaced until a turn is attempted.
    """
    wanted = name.strip()

    if not probe.reachable:
        console.print(f"[error]{escape(probe.explain())}[/]")
        return 1
    model = probe.model(wanted)
    if model is None:
        console.print(f"[error]no local model named '{escape(wanted)}'[/]")
        console.print("")
        for line in ollama.report(with_details=True):
            console.print(escape(line) if line.strip() else "")
        return 1

    window = ollama.runtime_context(model, probe.loaded.get(model.name, 0))
    console.print(f"[success]now using {escape(model.name)} on {escape(probe.base_url)}[/]")
    console.print(f"  context   {window:,} tokens"
                  + (f" (the model supports {model.context_length:,})" if model.context_length > window else ""))
    console.print(f"  tools     {'yes' if ollama.tool_capable(model) else 'NO -- this model cannot call tools'}")
    if model.capabilities:
        console.print(f"  abilities {', '.join(sorted(model.capabilities))}")
    _ollama_save(ollama.plain_name(model.name), probe, console, constants)
    if window < ollama.MIN_AGENT_CONTEXT:
        console.print(f"[warning]Ollama runs this model with only {window:,} tokens, too small for the agent. "
                      f"`myriaxon ollama --create` builds one with the full window.[/]")
    return 0


def _ollama_create(args, probe, console, constants, ollama) -> int:
    """`myriaxon ollama --create` -- build the Ollama model that `ollama run myriaxon` runs, and point Myriaxon at it.

    Ollama runs every model with a 4,096-token window unless the model says otherwise, and the OpenAI-compatible
    endpoint Myriaxon talks to cannot ask for more. The only way to a larger window is a model whose own settings carry
    it, so this builds one from a model the user already has (their files are not copied) with that model's full window,
    and a short Myriaxon persona for the plain `ollama run` chat.

    The cost of a big window is memory, and a machine that cannot hold it does not fail cleanly: it swaps. So the command
    says what the window costs against what this machine has, and loads the model afterwards (to prove it runs) only when
    the estimate says there is room.
    """
    if not probe.reachable:
        console.print(f"[error]{escape(probe.explain())}[/]")
        return 1

    name = (args.name or ollama.MYRIAXON_MODEL_NAME).strip()
    base = ollama.choose_base(probe, args.base)
    if base is None:
        if args.base:
            console.print(f"[error]no local model named '{escape(args.base)}'[/]")
            console.print("")
            for line in ollama.report(with_details=True):
                console.print(escape(line) if line.strip() else "")
        else:
            console.print("[error]None of the models Ollama has can call tools, which the agent needs. Pull one that can, "
                          "for example:  ollama pull qwen2.5-coder:7b[/]")
        return 1
    if not ollama.tool_capable(base):
        console.print(f"[warning]{escape(base.name)} cannot call tools, so the agent will not be able to use the new model; "
                      f"`ollama run` chat will still work.[/]")

    window = ollama.parse_window(args.ctx, base.context_length)
    if window <= 0:
        console.print(f"[error]--ctx must be 'max' or a number of tokens such as 65536 or 64k, not '{escape(args.ctx)}'[/]")
        return 2
    if window < ollama.MIN_AGENT_CONTEXT:
        console.print(f"[error]A {window:,}-token window leaves the agent no room to work: its own prompt is about 4,700 "
                      f"tokens. Use {ollama.MIN_AGENT_CONTEXT:,} or more.[/]")
        return 2
    if base.context_length and window > base.context_length:
        console.print(f"[error]{escape(base.name)} supports {base.context_length:,} tokens at most, not {window:,}.[/]")
        return 2

    if args.modelfile:
        console.print(ollama.modelfile_text(base.name, window), markup=False, highlight=False, soft_wrap=True)
        return 0

    existing = probe.model(name)
    if existing is not None and not ollama.made_by_us(existing):
        console.print(f"[error]Ollama already has a model called '{escape(name)}' that this command did not make, and creating "
                      f"it again would overwrite it. Use another name (--name), or remove that one yourself with "
                      f"`ollama rm {escape(name)}`.[/]")
        return 1

    loaded = ollama.loaded_models(probe.base_url)
    fit = ollama.estimate_fit(base, window, loaded_bytes=sum(entry[1] for entry in loaded.values()))
    console.print(f"{'Replacing' if existing else 'Creating'} '{escape(name)}' from {escape(base.name)} with a {window:,}-token "
                  f"window" + (f" (the model supports {base.context_length:,})." if base.context_length else "."))
    for line in fit.lines(base.name):
        console.print(f"  {escape(line)}")

    result = ollama.create_model(probe.base_url, name, base.name, window,
                                 progress=lambda status: console.print(f"[dim]  {escape(status)}[/]"))
    if not result.ok:
        console.print(f"[error]Could not create '{escape(name)}': {escape(result.message)}[/]")
        console.print(f"[dim]By hand:  myriaxon ollama --modelfile --from {escape(base.name)} > Modelfile   then   "
                      f"ollama create {escape(name)} -f Modelfile[/]")
        return 1
    console.print(f"[success]Created '{escape(name)}'.[/] It shares {escape(base.name)}'s files; nothing was copied.")

    status = 0
    if args.no_check:
        pass
    elif fit.verdict == "fits":
        console.print("Loading it once to check that it runs ...")
        ok, message, entry = ollama.probe_load(probe.base_url, name)
        if ok:
            size, in_vram = entry.get("size", 0), entry.get("size_vram", 0)
            console.print(f"[success]It runs.[/] Ollama allocated a {entry.get('context_length', 0):,}-token window, "
                          f"{size / ollama.GIB:.1f} GB in memory"
                          + (f", {100 * in_vram // size}% of it on the GPU." if size else "."))
        else:
            console.print(f"[error]It was created but would not load: {escape(message)}[/]")
            console.print(f"[dim]Try a smaller window:  myriaxon ollama --create --ctx "
                          f"{max(ollama.MIN_AGENT_CONTEXT, window // 2 // 1024 * 1024)}[/]")
            status = 1
    else:
        console.print("[dim]Not loaded to check: " + ("it needs more memory than is free right now, so it will load the first time "
                      "you use it." if fit.verdict in ("tight", "too-big") else "this machine's memory could not be measured.") + "[/]")

    if not args.no_use:
        console.print(f"[success]Myriaxon now uses '{escape(name)}'[/] on {escape(probe.base_url)}")
        _ollama_save(name, probe, console, constants)
    console.print("")
    console.print(f"  Chat    ollama run {escape(name)}      [dim](plain chat; no tools)[/]")
    console.print("  Agent   myriaxon                [dim](files, shell, git and web tools, under your permission rules)[/]")
    console.print(f"  Undo    myriaxon ollama --remove{' --name ' + escape(name) if name != ollama.MYRIAXON_MODEL_NAME else ''}"
                  f"     [dim](deletes '{escape(name)}' only; {escape(base.name)} stays)[/]")
    return status


def _ollama_remove(args, probe, console, ollama) -> int:
    """`myriaxon ollama --remove` -- delete the model `--create` built. Refuses any other, so a typo cannot delete a base model."""
    if not probe.reachable:
        console.print(f"[error]{escape(probe.explain())}[/]")
        return 1
    name = (args.name or ollama.MYRIAXON_MODEL_NAME).strip()
    model = probe.model(name)
    if model is None:
        console.print(f"[warning]Ollama has no model called '{escape(name)}'; nothing to remove.[/]")
        return 0
    if not ollama.made_by_us(model):
        console.print(f"[error]'{escape(name)}' was not made by `myriaxon ollama --create`, so it is not removed. If you do "
                      f"mean to delete it:  ollama rm {escape(name)}[/]")
        return 1
    ok, message = ollama.remove_model(probe.base_url, model.name)
    if not ok:
        console.print(f"[error]Could not remove '{escape(name)}': {escape(message)}[/]")
        return 1
    console.print(f"[success]Removed '{escape(name)}'.[/] The model it was built from is untouched.")
    from .config import load_config
    from .constants import PROVIDER_OLLAMA

    saved = load_config()
    if saved and saved.provider == PROVIDER_OLLAMA and saved.model in (name, model.name):
        console.print("[warning]Myriaxon was set to use it. Choose another with `myriaxon ollama --use <model>`, or run "
                      "`myriaxon --reauth`.[/]")
    return 0


def _run_audit_command(args, console) -> int:
    """`myriaxon audit` -- show what compaction removed, and recover it.

    The compaction audit records every tool result pruned, every span the summariser was denied, and every
    summary produced. That record is only useful if it can be read, so this is the reader.

    Three things it answers that nothing could answer before:

    * *What did compaction throw away?* -- each entry shows the kind, size, reason and origin.
    * *What did the agent actually see?* -- `--show DIGEST` prints the exact stored bytes, so a decision made
      on compacted context can be reconstructed rather than guessed at.
    * *Is compaction losing something important?* -- totals by kind make the loss rate visible, which is the
      precondition for improving it.

    Reads only. Nothing here mutates a log; the logs are append-only by construction.
    """
    from . import compaction_audit

    if args.list:
        sessions = compaction_audit.list_sessions()
        if not sessions:
            console.print(f"[dim]No compaction audits yet. They are written to "
                          f"{escape(str(compaction_audit.AUDIT_DIR))} as sessions run.[/]")
            return 0
        console.print(f"[bold]Compaction audit sessions[/] ({escape(str(compaction_audit.AUDIT_DIR))})")
        for session in sessions:
            console.print(
                f"  {session['session']:<20} {session['entries']:>5} entries  "
                f"{session['chars']:>12,} chars  {session['modified']}"
            )
        return 0

    session = args.session or compaction_audit.latest_session()
    if not session:
        console.print("[dim]No compaction audit recorded yet. This fills as a session compacts its "
                      "context; run with MYRIAXON_COMPACTION_AUDIT=0 to disable it.[/]")
        return 0

    recorder = compaction_audit.audit_for(session)

    if args.show:
        content = recorder.retrieve(args.show)
        if content is None:
            console.print(f"[error]No stored content for digest {escape(args.show)}.[/]")
            return 1
        # Printed raw, not through rich: this is recovered bytes, and markup would corrupt them.
        print(content)
        return 0

    entries = recorder.read_log()
    if not entries:
        console.print(f"[dim]Session {escape(session)} has an audit log but no entries.[/]")
        return 0

    if args.json:
        print(json.dumps({"session": session, "entries": entries}, ensure_ascii=False, indent=2))
        return 0

    totals = recorder.summary()
    console.print(f"[bold]Compaction audit:[/] {escape(session)}")
    summary_bits = ", ".join(
        f"{kind} {count}" for kind, count in sorted(totals["entries"].items())
    ) or "nothing recorded"
    console.print(f"  {summary_bits}")
    console.print(f"  log: {escape(str(recorder.log_path))}")

    shown = entries[-max(1, args.limit):]
    if len(entries) > len(shown):
        console.print(f"  [dim]showing the last {len(shown)} of {len(entries)} entries[/]")
    console.print()
    for entry in shown:
        kind = str(entry.get("kind", "?"))
        chars = int(entry.get("chars", 0))
        when = _time.strftime("%H:%M:%S", _time.localtime(float(entry.get("at", 0))))
        console.print(
            f"  [bold]{kind:<10}[/] {chars:>9,} chars  {when}  "
            f"[dim]{escape(str(entry.get('reason', '')))[:70]}[/]"
        )
        digest = str(entry.get("digest", ""))
        if digest:
            console.print(f"             [dim]recover with: myriaxon audit --show {digest}[/]")
    return 0


def _arm_locus_wakeup(session, agent) -> None:
    """Make the idle prompt return on its own when a Locus comes due -- INSIDE A LOCUS SESSION ONLY.

    Called from prompt_toolkit's `pre_run`, i.e. once the prompt's event loop is live and `app.loop` exists.

    The timer calls `app.exit()`, which makes `session.prompt()` return immediately. Control then goes back to
    the top of the REPL loop -- on the main thread, where the agent and its ToolContext may legitimately be
    touched. A background thread that ran Locus turns directly would be faster to write and would break the
    single-threaded invariant every agent backend depends on.

    Deliberately quiet and defensive. A wake-up mechanism that can crash the prompt would be worse than a Locus
    that fires late, so every failure path here returns having done nothing.

    AUTONOMY IS A LOCUS PROPERTY, NOT A MYRIAXON ONE -- and this function used to ignore that, which is why a
    plain `Myriaxon` session self-woke forever.

    Self-direction is the whole point of a Locus: it plans, it decides, it acts on its own objectives. A
    Myriaxon session has none of that. The person is the driver; the agent answers. So a prompt that returns
    on its own in Myriaxon mode is not autonomy, it is an interruption loop -- and it cannot even succeed,
    because there is nothing for it to run.

    The loop was worse than pointless, it was self-sustaining. `next_wakeup_seconds()` returns 0.0 whenever
    anything is due, which makes the timer fire almost immediately; the pass then finds nothing it is allowed
    to run -- a Locus whose preflight refuses stays permanently due, because a refused run does not advance
    `last_run_at` -- so nothing happens, the prompt re-arms, and it exits again. Measured on this machine with
    `anker`: due, zero open objectives, preflight refusing. The caret reset forever and said nothing.

    THE GATE. `active_dot` is the mode flag the prompt already uses two hundred lines up: it holds the Locus's
    name inside a Locus session and is empty in a Myriaxon one. Waking is armed only when it is set, so:

        Myria >          the prompt waits for you. No timer at all.
        <locus> >        a due Locus runs on its own, which is the feature.

    `MYRIAXON_DOT_WAKEUP=0` remains as the escape hatch for anyone who wants a Locus prompt that never
    self-wakes either.
    """
    if agent is None or getattr(agent, "tool_context", None) is None:
        return
    # Inside a Locus session only. See the note above: in a Myriaxon session there are no objectives to
    # advance, so there is nothing for a wake-up to do and every wake is a pure interruption.
    #
    # `.strip()`, because `active_dot` is a plain string field and a whitespace value is truthy -- without it a
    # value of `"   "` would count as "a Locus is active" and re-arm the very timer this gate removes.
    if not getattr(agent.tool_context, "active_dot", "").strip():
        return
    limit = _dot_wakeup_limit()
    if limit is None:
        return
    try:
        from . import loci as _loci

        registry = _loci.get_registry()
        # NOTHING TO RUN MEANS NO TIMER AT ALL. `next_wakeup_seconds()` returns the idle cap (30s by default)
        # when no Locus is runnable, and that cap exists so a deployment can notice a Locus enabled in the
        # meantime. For an interactive prompt the same value is a different thing entirely: a timer that fires
        # every 30 seconds to run nothing, which is the interruption loop again, merely slower.
        #
        # `headless()` is the honest question -- enabled, proactive, and with work it is ALLOWED to do, which
        # means it already applies the preflight that `due()` ignores. Empty means there is nothing this timer
        # could achieve, so it is not armed.
        #
        # The trade, stated rather than hidden: a Locus that becomes runnable while you sit at the prompt is
        # picked up on your next keypress instead of immediately. That is the right way round here. Firing a
        # timer under somebody's hands to discover there is nothing to do is worse than waiting for them.
        if not registry.headless():
            return
        delay = min(registry.next_wakeup_seconds(), limit)
    except Exception:
        logger.exception("Could not compute the next Locus wake-up; the prompt will not self-wake.")
        return

    app = getattr(session, "app", None)
    loop = getattr(app, "loop", None)
    if app is None or loop is None:
        return

    def _wake() -> None:
        try:
            # Nothing to do if the prompt already returned (the user typed something first).
            if getattr(app, "is_running", False):
                app.exit(result=LOCUS_WAKE_SENTINEL)
        except Exception:
            logger.exception("Locus wake-up timer failed; continuing without it.")

    try:
        loop.call_later(max(0.05, delay), _wake)
    except Exception:
        logger.exception("Could not schedule the Locus wake-up timer.")


def _dot_wakeup_limit() -> float | None:
    """The longest the prompt may idle before re-checking, or None to disable self-waking.

    `MYRIAXON_DOT_WAKEUP=0` disables it for anyone who wants a prompt that never returns on its own, and the
    value otherwise bounds the wait so a Locus created or enabled while the user is thinking is noticed within
    that window rather than at the next interval boundary.
    """
    raw = os.environ.get("MYRIAXON_DOT_WAKEUP", "15")
    if raw.strip() in ("", "0", "false", "no"):
        return None
    try:
        return max(1.0, float(raw))
    except ValueError:
        return 15.0


def _run_avatar_command(args, console) -> int:
    """`myriaxon avatar` -- show Loci, or one expression of it.

    ## The measured bug this function is shaped around

    `[{N}]` is a valid Rich markup tag, so the lantern vanished the moment the art went through Rich's markup parser:
    Rich read `[â—]` as a style tag it did not recognise and ate it. The art is therefore printed with `markup=False`,
    and that is not a detail -- it is the difference between showing the character and showing a hole where the
    character's light should be.
    """
    from . import loci_character as C
    from . import loci_surface as S

    terminal = C.detect_terminal(force_plain=bool(getattr(args, "plain", False)))

    if getattr(args, "expressions", False) or getattr(args, "check", False):
        # `--check` and `--expressions` list what exists; the JSON form is for a build step or a test.
        if getattr(args, "json", False):
            import json

            console.print(json.dumps(C.manifest(), indent=2, ensure_ascii=False), markup=False, highlight=False)
            return 0
        console.print(S.render_sheet(terminal=terminal), markup=False, highlight=False)
        return 0

    name = (args.expression or "").strip() or "idle"
    if getattr(args, "json", False):
        import json

        from . import loci_svg

        known = name in C.EXPRESSIONS
        console.print(json.dumps({"expression": name, "known": known,
                                  "manifest": loci_svg.manifest()}, indent=2, ensure_ascii=False),
                      markup=False, highlight=False)
        return 0 if known else 1

    size = getattr(args, "size", "") or ""
    ok, drawn = S.render_one(name, terminal=terminal)
    console.print(drawn, markup=False, highlight=False)
    if not ok:
        # The typo is REPORTED rather than passed over. A caller who asked for a face that does not exist should not
        # be left believing they saw it.
        console.print(f"[error]No expression named {name!r}.[/]")
        console.print(f"[dim]Known: {', '.join(C.EXPRESSIONS)}[/]")
        return 1
    console.print(
        f"[dim]{C.expression_for(name).id} -- {C.expression_for(name).label} -- "
        f"{C.describe_terminal(terminal)}[/]"
    )
    return 0


def _run_export_command(args, console) -> int:
    """`myriaxon export` -- hand someone the run as one reviewable artefact.

    Sanitised by default, and the sanitising is reported rather than silent: a reader who cannot tell redaction
    happened will read `[redacted]` as the real value. `--no-sanitize` exists for a local copy and is named
    explicitly, because the opposite default is how a credential ends up in a pasted transcript.
    """
    from pathlib import Path as _Path

    from . import run_export

    bundle = run_export.build(
        _Path.cwd(),
        session_id=args.session,
        sanitize=not args.no_sanitize,
        include_messages=not args.no_messages,
    )
    destination = _Path(args.out) if args.out else None
    ok, message, path = run_export.write(bundle, destination, fmt=args.format)

    if not ok:
        console.print(f"[error]{escape(message)}[/]")
        return 1

    # The sanitising line is printed even on success, and printed as a warning when it was off. It is the one fact
    # a person needs before sending the file anywhere.
    if bundle.sanitized.applied:
        console.print(f"[dim]{escape(bundle.sanitized.render())}[/]")
    else:
        console.print(f"[warning]{escape(bundle.sanitized.render())}[/]")
    if bundle.gaps:
        console.print(f"[dim]incomplete: {escape('; '.join(bundle.gaps))}[/]")
    console.print(f"[success]{escape(message)}[/]")
    return 0


def _run_branch_command(args, console) -> int:
    """`myriaxon branch` -- list, create or resume session branches.

    Exists because a conversation you cannot branch is a conversation you cannot explore in: the only options were
    to argue with the accumulated context or to throw it away. Verified by `tools/verify_session_lineage.py`.

    Every mode is non-destructive. `--fork` writes a new file and leaves the active session byte-for-byte alone;
    `--resume` copies a branch into the active file and leaves the branch in place, so resuming is repeatable.
    """
    import json as _json

    from . import session_lineage

    root = Path.cwd()

    if args.resume:
        ok, message = session_lineage.resume_branch(args.resume, root)
        if args.json:
            print(_json.dumps({"ok": ok, "message": message}, ensure_ascii=False))
        elif ok:
            console.print(f"[success]{escape(message)}[/]")
        else:
            console.print(f"[error]{escape(message)}[/]")
        return 0 if ok else 1

    if args.fork:
        result = session_lineage.fork_session(
            name=args.name, fork_index=args.at, project_root=root
        )
        if args.json:
            print(_json.dumps({
                "ok": result.ok, "session_id": result.session_id,
                "inherited": result.inherited, "parent": result.parent, "reason": result.reason,
            }, ensure_ascii=False))
        elif result.ok:
            console.print(f"[success]{escape(result.render())}[/]")
            console.print(
                "[dim]The active session is untouched. Continue in the branch with "
                f"`myriaxon branch --resume {escape(result.session_id)}`.[/]"
            )
        else:
            # A refusal is a real answer, not a crash: it names the reason so the caller can fix the index.
            console.print(f"[error]{escape(result.render())}[/]")
        return 0 if result.ok else 1

    rendered = session_lineage.render_tree(root)
    if args.json:
        tree = session_lineage.load_tree(root)
        print(_json.dumps({
            "branches": [
                {"session_id": b.session_id, "name": b.name, "parent": b.parent,
                 "fork_index": b.fork_index, "created_at": b.created_at}
                for b in sorted(tree.values(), key=lambda b: b.created_at)
            ],
            "active": (load_session(root) or {}).get("session_id", ""),
        }, ensure_ascii=False))
    else:
        console.print(escape(rendered))
        active = (load_session(root) or {}).get("session_id", "")
        if active:
            console.print(f"\n[dim]Active session: {escape(active)}[/]")
    return 0


def _run_doctor_command(args, console) -> int:
    """`myriaxon doctor` -- inspect local state, and repair only what is safe to repair.

    Distinct from `verify`, which probes *capabilities* (does this skill load, does this tool get called). This
    inspects *state*: is the config parseable, is it readable by others, are the state files valid JSON, is the
    saved session usable, are staged proposals intact.

    `--fix` requires `--dry-run` to be meaningful, not to be safe: every shipped repair declares its effects
    before running and a dry run performs none of them, so the difference between the two is exactly what the
    effects list says. A repair that could not be dry-run-safe is not shipped as an automatic one.
    """
    from . import health

    health.register_builtin_checks()

    only = [args.check] if args.check else None
    if args.check and args.check not in health.registered():
        console.print(f"[error]No health check with id {escape(args.check)}.[/]")
        available = ", ".join(sorted(health.registered()))
        console.print(f"  Available: {escape(available)}")
        return 1

    try:
        findings = health.run_checks(
            only=only, include_opt_in=args.all, min_severity=args.min_severity
        )
    except Exception as exc:  # noqa: BLE001
        console.print(f"[error]The health pass could not run: {escape(str(exc))}[/]")
        return 1

    repairs: list = []
    if args.fix:
        repairs = health.repair_all(findings, dry_run=args.dry_run, only=only)

    if args.json:
        print(json.dumps({
            "summary": health.summarise(findings),
            "findings": {
                check_id: [
                    {"severity": f.severity, "message": f.message, "path": f.path,
                     "fix_hint": f.fix_hint, "target": f.target}
                    for f in found
                ]
                for check_id, found in findings.items()
            },
            "repairs": [
                {"check_id": r.check_id, "status": r.status, "reason": r.reason,
                 "changes": r.changes, "warnings": r.warnings, "dry_run": r.dry_run,
                 "effects": [
                     {"kind": e.kind, "action": e.action, "target": e.target,
                      "dry_run_safe": e.dry_run_safe}
                     for e in r.effects
                 ]}
                for r in repairs
            ],
        }, ensure_ascii=False, indent=2))
    else:
        console.print("[bold]Health check[/]")
        console.print()
        any_findings = False
        for check_id, found in sorted(findings.items()):
            if not found:
                continue
            any_findings = True
            console.print(f"  [bold]{escape(check_id)}[/]")
            for finding in found:
                console.print(finding.render())
            console.print()
        if not any_findings:
            tally = health.summarise(findings)
            console.print(f"  [success]No problems found[/] ({tally['checks_run']} check(s) ran).")

        if args.fix:
            if not repairs:
                console.print("  [dim]Nothing to repair.[/]")
            for outcome in repairs:
                console.print(outcome.render())
        elif any_findings:
            console.print("  [dim]Run with --fix --dry-run to see what repairing would change.[/]")

        tally = health.summarise(findings)
        console.print()
        console.print(
            f"  {tally['checks_run']} check(s), {tally['checks_with_findings']} with findings: "
            f"{tally[health.ERROR]} error(s), {tally[health.WARNING]} warning(s), {tally[health.INFO]} info."
        )

    # Exit 1 only for errors. A warning is something to look at, not a failure of the installation, and making
    # every warning a non-zero exit would train a reader to ignore the status.
    errors = health.summarise(findings)[health.ERROR]
    return 1 if errors else 0


def _run_skills_command(args, console) -> int:
    """`myriaxon skills` -- the skill lifecycle and its usage.

    Two things this makes visible that were not before:

    * **Which skills are actually invoked.** `verify` proves a skill loads; only usage proves it earns the space
      it takes in every system prompt. A skill with zero uses is a question, not a fact.
    * **What is staged versus live.** A generated skill can be proposed and reviewed before it becomes real,
      with a rollback record captured at the moment it is applied.

    The default is a read-only report. Every mutating action is an explicit flag, because a command whose bare
    form changes what future sessions load would be a surprising thing to type.
    """
    from . import skill_usage, skill_workshop

    def _find(ref: str):
        proposals = skill_workshop.load_proposals()
        if ref in proposals:
            return proposals[ref]
        matches = [p for p in proposals.values() if p.target_name == ref and p.is_live]
        return matches[0] if len(matches) == 1 else None

    # ---- mutations first, each with its own explicit flag
    if args.apply or args.reject or args.quarantine or args.stale or args.rollback:
        action = (
            ("apply", args.apply) if args.apply else
            ("reject", args.reject) if args.reject else
            ("quarantine", args.quarantine) if args.quarantine else
            ("stale", args.stale) if args.stale else
            ("rollback", args.rollback)
        )
        verb, ref = action
        if verb == "rollback":
            outcome = skill_workshop.rollback(ref)
            if outcome.ok:
                for note in outcome.notes:
                    console.print(f"  [success]{escape(note)}[/]")
                return 0
            for error in outcome.errors:
                console.print(f"  [error]{escape(error)}[/]")
            return 1

        if verb == "apply":
            outcome = skill_workshop.apply(ref, expected_revision_hash=args.expect)
        elif verb == "reject":
            outcome = skill_workshop.reject(ref, args.reason, expected_revision_hash=args.expect)
        elif verb == "quarantine":
            outcome = skill_workshop.quarantine(ref, args.reason, expected_revision_hash=args.expect)
        else:
            outcome = skill_workshop.mark_stale(ref, args.reason, expected_revision_hash=args.expect)

        if outcome.ok and outcome.proposal is not None:
            console.print(
                f"  [success]{verb}ed[/] {escape(outcome.proposal.id)} "
                f"({escape(outcome.proposal.target_name)}) -> {escape(outcome.proposal.status)}"
            )
            for note in outcome.notes:
                console.print(f"      {escape(note)}")
            return 0
        for error in outcome.errors:
            console.print(f"  [error]{escape(error)}[/]")
        return 1

    if args.show:
        proposal = _find(args.show)
        if proposal is None:
            console.print(f"[error]No proposal matching {escape(args.show)}.[/]")
            return 1
        console.print(proposal.render())
        console.print()
        draft = skill_workshop.draft_text(proposal)
        if draft:
            print(draft)
        else:
            console.print("  [warning]The draft file is missing; this proposal cannot be applied.[/]")
        return 0

    if args.history:
        events = skill_workshop.read_events()
        if args.json:
            print(json.dumps(events, ensure_ascii=False, indent=2))
            return 0
        if not events:
            console.print("[dim]No proposal events yet.[/]")
            return 0
        console.print(f"[bold]Proposal history[/] ({len(events)} event(s))")
        for event in events:
            stamp = _time.strftime("%Y-%m-%d %H:%M", _time.localtime(event.get("at", 0)))
            console.print(
                f"  {event.get('sequence', 0):>4}  {stamp}  "
                f"{escape(str(event.get('proposal_id', '')))}  "
                f"[bold]{escape(str(event.get('event_type', '')))}[/]  "
                f"[dim]{escape(str(event.get('detail', ''))[:70])}[/]"
            )
        return 0

    # ---- read-only reports
    if args.usage:
        from .skills import discover_skills

        skills = discover_skills()
        rows = skill_usage.report(skills)
        tally = skill_usage.summary(skills)
        if args.json:
            print(json.dumps({"summary": tally, "skills": rows}, ensure_ascii=False, indent=2))
            return 0
        console.print("[bold]Skill usage[/]")
        console.print()
        for row in rows:
            marker = "" if row["installed"] else " [dim](no longer installed)[/]"
            if row["use_count"] == 0:
                console.print(f"  {escape(row['name']):<34} [warning]never used[/]{marker}")
            else:
                age = _time.time() - row["last_used_at"]
                when = (
                    f"{int(age)}s" if age < 90 else
                    f"{int(age // 60)}m" if age < 5400 else
                    f"{int(age // 3600)}h" if age < 172800 else
                    f"{int(age // 86400)}d"
                )
                by = f", by {escape(row['last_agent_id'])}" if row["last_agent_id"] else ""
                console.print(
                    f"  {escape(row['name']):<34} {row['use_count']:>4} use(s), last {when} ago{by}{marker}"
                )
        console.print()
        console.print(
            f"  {tally['used_at_least_once']}/{tally['discovered']} discovered skill(s) have been used; "
            f"{tally['never_used']} never have."
        )
        if tally["never_used"]:
            console.print(
                "  [dim]A never-used skill still costs space in every system prompt. If it is not needed, "
                "remove it; if it is, the listing may need a better description.[/]"
            )
        return 0

    proposals = skill_workshop.listing(status=args.status)
    tally = skill_workshop.summary()
    if args.json:
        print(json.dumps({
            "summary": tally,
            "proposals": [p.to_dict() for p in proposals],
        }, ensure_ascii=False, indent=2))
        return 0

    if not proposals:
        console.print("[dim]No staged skill proposals. An agent's save_skill stages one here for review "
                      "instead of writing it live.[/]")
        return 0

    console.print(f"[bold]Skill proposals[/] ({tally['total']} total)")
    console.print()
    for proposal in proposals:
        console.print(proposal.render())
        if skill_workshop.draft_missing(proposal):
            console.print("      [warning]degraded: the draft file is missing[/]")
    console.print()
    console.print(
        "  [dim]apply: myriaxon skills --apply <id>   |   reject: --reject <id>   |   "
        "draft: --show <id>   |   history: --history[/]"
    )
    return 0


def _run_roles_command(args, console) -> int:
    """`myriaxon roles` -- list, inspect, validate and install agent roles.

    A role is a portable template; a Locus is a live agent. Keeping them separate is what makes "what does a
    reviewer do here?" answerable and shareable rather than a property of one machine's `loci.json`.

    Defaults to listing, because a command whose bare form installs agents would be a surprising thing to type.
    """
    from . import roles as roles_mod

    available = roles_mod.discover_roles()

    if args.check:
        failures = 0
        checks = 0
        for name, role in sorted(available.items()):
            report = roles_mod.validate_role(role)
            checks += 1
            if not report.ok:
                failures += 1
            if not report.ok or args.json:
                console.print(report.render())
        if args.json:
            print(json.dumps({
                "checked": checks,
                "failed": failures,
                "roles": [
                    {"name": n, "ok": roles_mod.validate_role(r).ok,
                     "errors": roles_mod.validate_role(r).errors,
                     "source": str(r.source)}
                    for n, r in sorted(available.items())
                ],
            }, ensure_ascii=False, indent=2))
        else:
            for name, role in sorted(available.items()):
                report = roles_mod.validate_role(role)
                if report.ok:
                    console.print(f"  [success]PASS[/]  {escape(name)}")
            console.print()
            colour = "success" if not failures else "error"
            console.print(f"[{colour}]{checks - failures}/{checks} roles valid[/]")
        return 1 if failures else 0

    if args.show:
        role = available.get(args.show)
        if role is None:
            console.print(f"[error]No role named {escape(args.show)}.[/]")
            console.print(f"  Available: {escape(', '.join(sorted(available)) or '(none)')}")
            return 1
        if args.json:
            from dataclasses import asdict

            data = asdict(role)
            data["source"] = str(role.source)
            print(json.dumps(data, ensure_ascii=False, indent=2))
            return 0
        console.print(role.render())
        console.print()
        print(role.instructions)
        return 0

    if args.install or args.team:
        from . import loci as loci_module

        registry = loci_module.get_registry()
        goals = [args.goal] if args.goal else None

        if args.team:
            created, errors = roles_mod.install_team(registry, available)
            for locus in created:
                console.print(f"  [success]created[/] {escape(locus.name)} ({escape(locus.role)})")
            for message in errors:
                console.print(f"  [error]skipped:[/] {escape(message)}")
            console.print()
            if created:
                console.print(
                    f"[success]Installed {len(created)} role(s).[/] They have no objectives yet -- a role says "
                    "what kind of work this is, not what you want done. Add one per Locus with locus_goal_add."
                )
            return 1 if errors and not created else 0

        if not args.names:
            console.print("[error]--install needs at least one role name (or use --team).[/]")
            console.print(f"  Available: {escape(', '.join(sorted(available)) or '(none)')}")
            return 1

        created_any = False
        failed = 0
        for name in args.names:
            role = available.get(name)
            if role is None:
                console.print(f"  [error]no such role: {escape(name)}[/]")
                failed += 1
                continue
            locus, report = roles_mod.install_role(
                role, registry, goals=goals,
                proactive=True if args.proactive else None,
            )
            if locus is None:
                console.print(f"  [error]{escape(name)} is not installable:[/]")
                for message in report.errors:
                    console.print(f"      {escape(message)}")
                failed += 1
                continue
            created_any = True
            console.print(f"  [success]created[/] {escape(locus.name)} from role {escape(role.name)}")
        if created_any and not goals:
            console.print()
            console.print(
                "  [dim]The new Locus has no objective yet, so it will not wake. A role is the kind of agent; "
                "the goal is what you want done.[/]"
            )
            suggested = available.get(args.names[0])
            if suggested is not None and suggested.suggested_goal:
                console.print(f"  [dim]This role suggests: {escape(suggested.suggested_goal)}[/]")
        return 1 if failed else 0

    # Default: list.
    if args.json:
        print(json.dumps({
            "roles": [
                {"name": r.name, "description": r.description, "theme": r.theme,
                 "proactive": r.proactive, "source": str(r.source)}
                for r in sorted(available.values(), key=lambda x: x.name)
            ]
        }, ensure_ascii=False, indent=2))
        return 0

    if not available:
        console.print("[dim]No roles found. Built-in roles ship with Myriaxon; add your own under "
                      ".myriaxon/roles/<name>/ROLE.md.[/]")
        return 0

    console.print(f"[bold]Roles[/] ({len(available)} available)")
    console.print()
    for role in sorted(available.values(), key=lambda r: r.name):
        console.print(role.render())
    console.print()
    console.print("  [dim]Install one with: myriaxon roles --install <name>   |   "
                  "the default team: myriaxon roles --team[/]")
    return 0


def _run_instructions_command(args, console) -> int:
    """`myriaxon instructions` -- show the instruction layer.

    The instruction layer is assembled on every launch and injected into every turn, and until now there was no
    way to see it. That made two very different failures indistinguishable from the outside: the agent ignoring
    a rule, and the rule never having loaded at all.

    Reports the sources in precedence order, the ones that were considered and skipped, and the cost in a form
    that matters -- characters, lines, and estimated tokens *per turn*. Then warns if the layer is large enough
    to hurt adherence. It never truncates: silently dropping instructions would change what the agent knows
    without telling anyone, which is the failure this command exists to expose.
    """
    from . import instructions as instructions_mod

    try:
        result = instructions_mod.load_instructions()
    except Exception as exc:  # noqa: BLE001
        console.print(f"[error]Could not load instructions: {escape(str(exc))}[/]")
        return 1

    if args.json:
        print(json.dumps({
            "total_chars": result.total_chars,
            "total_lines": result.total_lines,
            "est_tokens_per_turn": result.est_tokens,
            "over_budget": result.over_budget,
            "recommended_chars": instructions_mod.RECOMMENDED_CHARS,
            "recommended_lines": instructions_mod.RECOMMENDED_LINES,
            "warnings": result.warnings,
            "sources": [
                {
                    "path": str(s.path),
                    "kind": s.kind,
                    "loaded": s.loaded,
                    "chars": s.chars,
                    "lines": s.lines,
                    "depth": s.depth,
                    "note": s.note,
                }
                for s in result.sources
            ],
        }, ensure_ascii=False, indent=2))
        return 0

    if args.show:
        if not result.text:
            console.print("[dim]No instruction files loaded, so nothing would be injected.[/]")
            return 0
        # Printed raw: this is the exact text the model receives, and rich markup would corrupt it.
        print(result.text)
        return 0

    console.print("[bold]Instruction layer[/]")
    console.print()
    print(result.render())
    console.print()

    if result.warnings:
        for warning in result.warnings:
            console.print(f"  [warning]warning:[/] {escape(warning)}")
        console.print()
        console.print("  [dim]Nothing was truncated. Use `@path` imports to split a large file by topic, "
                      "or save_skill for anything procedural that is not needed on every turn.[/]")
    elif result.loaded:
        console.print("  [success]Within the recommended budget.[/]")
    return 0


def _run_loci_command(args, console) -> int:
    """`myriaxon loci` -- run due Loci with no interactive session.

    This is the deployment entry point the Loci design always described and never had. Before it, the only
    caller of the proactive pass was the top of the REPL loop, so a Locus ran when the owner pressed Enter -- and
    "always-on agent" meant "agent that is on while you are sitting at the terminal". A cron job, a systemd
    timer or a container can call this instead:

        */5 * * * *  myriaxon loci --run         # catch up on whatever is due
        myriaxon loci --watch --max-seconds 3600 # stay resident for an hour

    Everything expensive is delegated: `locus_run` owns the preflight refusal, the run slot, the cross-process
    lease, the retry policy and the outcome record. This function only decides *which* Loci are due and in what
    order, so a guarantee cannot drift between the interactive and the headless path.

    Exit status distinguishes "worked" from "had nothing to do": 0 when every attempted run succeeded, 1 when
    one failed. A quiet success is reported rather than silent, because an agent that looks idle and an agent
    that is broken are indistinguishable from the outside otherwise.
    """
    from . import loci as loci_module

    registry = loci_module.get_registry()

    async def _status() -> int:
        due = registry.due()
        heads = registry.headless()
        wait = registry.next_wakeup_seconds()
        if args.json:
            print(json.dumps({
                "headless": [d.name for d in heads],
                "due": [d.name for d in due],
                "next_wakeup_seconds": wait,
                "loci": [
                    {
                        "name": d.name,
                        "enabled": d.enabled,
                        "proactive": d.proactive,
                        "open_goals": len(d.runnable_goals()),
                        "interval_seconds": d.interval_seconds,
                        "seconds_until_due": d.seconds_until_due(),
                        "last_run_at": d.last_run_at,
                    }
                    for d in registry.all()
                ],
            }, ensure_ascii=False, indent=2))
            return 0
        if not heads:
            console.print("[dim]No Locus is set up to work unattended. A Locus needs to be enabled, have "
                          "proactive mode on, and have at least one open objective.[/]")
            for d in registry.all():
                why = []
                if not d.enabled:
                    # A self-disabled Locus must never read as a deliberate choice. OpenClaw's own warning is
                    # that a disabled job "is invisible to every guard ... and will never fire or explain
                    # itself -- a silent non-outcome, which is a worse failure than a job that runs and
                    # visibly complains." So it says who disabled it, why, and how to undo it.
                    why.append("DISABLED ITSELF -- see below" if d.auto_disabled_at else "disabled")
                if not d.proactive:
                    why.append("proactive off")
                if not d.runnable_goals():
                    why.append("no open goal")
                console.print(f"  {escape(d.name):<20} {'not eligible: ' + ', '.join(why) if why else 'ready'}")
            self_disabled = [d for d in registry.all() if d.auto_disabled_at]
            if self_disabled:
                console.print()
                console.print(f"[warning]{len(self_disabled)} Locus(s) disabled themselves after repeated "
                              "failures. They will not wake until reset:[/]")
                for d in self_disabled:
                    console.print(f"  {escape(d.name):<20} {escape(d.auto_disabled_reason)}")
                    console.print(f"  {'':<20} [dim]restore with: myriaxon loci --reset {escape(d.name)}[/]")
            return 0
        console.print(f"[bold]Loci ready to work unattended:[/] {len(heads)}")
        for d in heads:
            until = d.seconds_until_due()
            mark = "DUE NOW" if until <= 0 else f"in {until}s"
            console.print(
                f"  {escape(d.name):<20} {len(d.runnable_goals())} open goal(s), "
                f"interval {d.interval_seconds}s, next run {mark}"
            )
        console.print(f"  next wake-up in {wait:.0f}s" if wait else "  due now")
        return 0

    if args.autonomy:
        name = (args.locus or (args.subject[0] if args.subject else "")).strip()
        if not name:
            # Reporting every locus is the useful reading of a bare flag with no name, and it is a
            # read: nothing is written on this path.
            rows = registry.all()
            if not rows:
                console.print("[dim]No Loci yet. One appears here once you make one.[/]")
                return 0
            console.print("[bold]Autonomy[/]  (assisted = works only the objectives you gave it)")
            for d in rows:
                mode = d.autonomy
                steps = d.step_budget()
                mark = "[success]self-directed[/]" if d.is_self_directed() else "assisted"
                console.print(f"  [bold]{escape(d.name)}[/]  {mark}  up to {steps} step(s) per run")
                if d.is_self_directed():
                    console.print(f"      confined to {escape(str(d.workspace_path()))}")
            console.print("[dim]Set it with:  myriaxon loci --autonomy self_directed --locus <name>[/]")
            return 0

        locus = registry.get(name)
        if locus is None:
            console.print(f"[error]No Locus named {escape(name)}.[/]")
            return 1
        wanted = str(args.autonomy).strip().lower()
        if wanted in ("?", "show", ""):
            console.print(f"{escape(locus.name)}: autonomy={locus.autonomy} "
                          f"({'may set its own objectives' if locus.is_self_directed() else 'only yours'}), "
                          f"up to {locus.step_budget()} step(s) per run")
            return 0
        if wanted not in loci_module.AUTONOMY_MODES:
            console.print(f"[warning]Unknown mode {escape(wanted)!r}. Choose "
                          f"{', '.join(loci_module.AUTONOMY_MODES)}.[/]")
            return 1
        was = locus.autonomy
        locus.autonomy = wanted
        registry.persist(locus)
        console.print(f"[success]{escape(locus.name)}: autonomy {escape(was)} -> {escape(wanted)}.[/]")
        if locus.is_self_directed():
            # The boundary is stated at the moment it is granted, not buried in a doc. The owner chose
            # full self-direction WITH confinement, and a message that only said "self-directed" would
            # let "autonomous" be read as "unrestricted" -- which is the one misreading that matters.
            console.print(f"  It may now decide and record its own objectives, and act only inside\n"
                          f"  {escape(str(locus.workspace_path()))}")
            console.print(f"  Steps per run: {locus.step_budget()} (owner objectives still outrank its own).")
        return 0

    if getattr(args, "supervise", False):
        # Pure Python, cross-platform, no OS scheduler involved. This is the same loop the dashboard
        # runs on a thread, exposed for a headless deployment -- a container, a systemd unit, a
        # `while true` in a shell -- or for anyone who wants the clock somewhere other than the UI.
        #
        # `--watch` above already loops; this exists separately because it uses the SHARED
        # `run_due_once`, so the headless path and the dashboard path cannot drift into two different
        # definitions of "advance what is due".
        interval = loci_module.supervisor_interval()
        console.print(
            f"[success]Supervising: due Loci run every {interval:.0f}s. Ctrl-C to stop.[/]"
        )
        consecutive_errors = 0
        try:
            while True:
                try:
                    runs, failures = loci_module.run_due_once(console=console)
                    if runs:
                        console.print(f"[dim]  {runs} run(s), {failures} failure(s)[/]")
                    consecutive_errors = 0
                except Exception as exc:  # noqa: BLE001 - a pass must never kill the supervisor
                    consecutive_errors += 1
                    console.print(f"[error]supervisor pass failed: {escape(str(exc))}[/]")
                    # Backed off rather than spinning: a pass that fails instantly would otherwise
                    # become a busy loop against a broken dependency, which is louder than the fault
                    # and harder to diagnose.
                    if consecutive_errors >= 5:
                        console.print("[error]five consecutive failures; stopping.[/]")
                        return 1
                _time.sleep(interval)
        except KeyboardInterrupt:
            console.print("\n[dim]Stopped.[/]")
            return 0

    if args.status:
        return _run_async(_status())

    if getattr(args, "usage", False):
        # The ledger records every turn; without a way to read it the record is a file nobody opens,
        # and "we log the cost" becomes indistinguishable from "we know the cost".
        from . import usage as usage_module

        entries = usage_module.read_ledger()
        if getattr(args, "json", False):
            print(json.dumps(usage_module.ledger_summary(entries), indent=2))
            return 0
        table_date = ""
        try:
            from .bench.prices import PRICE_TABLE_DATE

            table_date = PRICE_TABLE_DATE
        except Exception:  # noqa: BLE001 - pricing is optional
            pass
        console.print(usage_module.format_ledger_summary(
            usage_module.ledger_summary(entries), table_date=table_date
        ))
        # Per-agent attribution, because "what did the swarm cost" without "which member" is not
        # actionable when one member is misbehaving.
        by_agent: dict[str, float] = {}
        for row in entries:
            who = str(row.get("agent") or row.get("source") or "(interactive)")
            cost = row.get("cost_usd")
            if cost is not None:
                by_agent[who] = by_agent.get(who, 0.0) + float(cost)
        if by_agent:
            console.print("\n[bold]By agent:[/]")
            for who, cost in sorted(by_agent.items(), key=lambda kv: -kv[1]):
                console.print(f"  {escape(who):24} ${cost:.4f}")
        console.print(f"[dim]Ledger: {usage_module.ledger_path()}[/]")
        return 0

    if args.reset:
        # The one-command way back from a self-disable. Without it the auto-disable would be a trap: a Locus that
        # stopped itself and could only be restarted by hand-editing loci.json would be worse than one that
        # kept failing loudly, which is the trade OpenClaw's docs warn about.
        locus = registry.get(args.reset)
        if locus is None:
            console.print(f"[error]No Locus named {escape(args.reset)}.[/]")
            names = ", ".join(d.name for d in registry.all()) or "(none)"
            console.print(f"  Available: {escape(names)}")
            return 1
        if locus.enabled and not locus.auto_disabled_at and locus.consecutive_failures == 0:
            console.print(f"[dim]{escape(locus.name)} has nothing to reset: it is enabled and its failure "
                          "ledger is already empty.[/]")
            return 0
        summary = loci_module.reset_failure_ledger(locus)
        registry.persist(locus)
        console.print(f"[success]Reset {escape(locus.name)}: {escape(summary)}.[/]")
        # Resetting the ledger re-enables the Locus, but it does not make it wakeable: that also needs proactive
        # mode and an open objective. Reporting "reset" and stopping there would hand back a Locus that still
        # never runs -- another silent non-outcome, which is the failure this whole mechanism exists to remove.
        blockers = []
        if not locus.proactive:
            blockers.append("proactive mode is off")
        if not locus.runnable_goals():
            blocked = [g for g in locus.goals if g.status == loci_module.GOAL_BLOCKED]
            if blocked:
                blockers.append(
                    f"{len(blocked)} goal(s) blocked, e.g. {blocked[0].title!r} -- set one back to pending"
                )
            else:
                blockers.append("it has no objective")
        if blockers:
            console.print(f"  [warning]It still will not wake: {'; '.join(blockers)}.[/]")
        else:
            console.print(f"  It will wake again in {locus.seconds_until_due()}s.")
        return 0

    if not (args.run or args.watch):
        # Bare `myriaxon loci` reports rather than acting, so a mistyped command never starts spending tokens.
        console.print("[dim]Nothing to do: pass --run to work the due Loci, --watch to stay resident, "
                      "or --status to see which Loci are eligible.[/]")
        return _status_sync(registry, console, args)

    # The agent is built once and reused across every run in this process. Building it per Locus would pay the
    # provider SDK import and MCP discovery again for each one.
    try:
        config = resolve_config(console, reauth=False, ollama=getattr(args, "ollama", False),
                                model=getattr(args, "model", None) or "",
                                vendor=getattr(args, "vendor", "") or "")
    except Exception as exc:  # noqa: BLE001
        console.print(f"[error]Could not resolve configuration: {escape(str(exc))}[/]")
        return 1

    try:
        agent = _build_agent_for_command(config, console, args)
    except Exception as exc:  # noqa: BLE001
        console.print(f"[error]Could not build an agent: {escape(str(exc))}[/]")
        return 1

    if agent.tool_context is None:
        console.print("[error]The agent has no tool context, so no Locus can run.[/]")
        return 1

    deadline = (_time.time() + args.max_seconds) if args.max_seconds > 0 else None
    total_runs = 0
    total_failures = 0

    try:
        while True:
            due = registry.due()
            if args.locus:
                due = [d for d in due if d.name == args.locus or d.id == args.locus]

            for locus in due:
                ran, failed = _run_one_dot(locus, agent, console, args)
                total_runs += int(ran)
                total_failures += int(failed)

            if not args.watch:
                break
            if deadline is not None and _time.time() >= deadline:
                console.print("[dim]--watch reached --max-seconds; stopping.[/]")
                break
            wait = registry.next_wakeup_seconds()
            if deadline is not None:
                wait = min(wait, max(0.0, deadline - _time.time()))
            if wait <= 0:
                wait = 1.0
            console.print(f"[dim]-- sleeping {wait:.0f}s until the next Locus is due[/]")
            _time.sleep(wait)
    except KeyboardInterrupt:
        console.print("\n[dim]Interrupted; stopping.[/]")
    finally:
        try:
            if getattr(agent, "mcp_bridge", None) is not None:
                agent.mcp_bridge.shutdown()
        except Exception:
            logger.exception("Could not shut the MCP bridge down cleanly.")

    if args.json:
        print(json.dumps({"runs": total_runs, "failures": total_failures}, ensure_ascii=False))
    else:
        console.print(f"[bold]{total_runs} run(s), {total_failures} failure(s).[/]")
    return 1 if total_failures else 0


def _status_sync(registry, console, args) -> int:
    """The reporting path, for the case where no agent is built. Shares the fields with `--status`."""
    heads = registry.headless()
    # Interrupted runs, surfaced rather than left for the next wake-up to discover. A resume that only
    # announces itself in a `[dim]` line at the start of a run is a resume the owner never learns about,
    # and "it quietly continued something from last week" is the failure mode of durability that nobody
    # anticipates when they ask for it.
    interrupted = run_state.list_resumable()
    if args.json:
        print(json.dumps({"headless": [d.name for d in heads],
                          "due": [d.name for d in registry.due()],
                          "next_wakeup_seconds": registry.next_wakeup_seconds(),
                          "interrupted_runs": [
                              {"run_id": s.run_id, "locus": s.locus_name, "step": s.step,
                               "budget": s.budget, "goal": s.goal_title,
                               "age_seconds": int(_time.time() - s.updated_at)}
                              for s in interrupted
                          ]},
                         ensure_ascii=False, indent=2))
        return 0
    if not heads:
        console.print("[dim]No Locus is currently eligible to work unattended.[/]")
    else:
        for d in heads:
            until = d.seconds_until_due()
            console.print(
                f"  {escape(d.name):<20} {len(d.runnable_goals())} open goal(s), "
                f"next run {'DUE NOW' if until <= 0 else f'in {until}s'}"
            )
    if interrupted:
        console.print("")
        console.print(f"[warning]{len(interrupted)} interrupted run(s) will resume:[/]")
        for state in interrupted:
            age = int(_time.time() - state.updated_at)
            console.print(f"  {escape(run_state.format_run_state(state))} [dim]({age}s ago)[/]")

    # The swarm board, shown alongside the loci because they are the same story: the board is how these
    # agents hand work to each other. Printed only when something is on it, so a single-agent user is not
    # told about a mechanism they have never used.
    try:
        from . import locus_swarm

        board = locus_swarm.board_status()
        if board["total"]:
            console.print("")
            console.print(escape(locus_swarm.format_board(board)))
    except Exception:  # noqa: BLE001 - a status panel must never fail on a missing board
        pass

    # Repeating failures, which is the one operational signal a log cannot give. Shown only when something
    # has failed enough times to be a defect -- a single failure is noise and reporting it here would train
    # the reader to ignore the section.
    try:
        from . import dead_letter

        offenders = dead_letter.repeat_offenders()
        if offenders:
            console.print("")
            console.print("[warning]Repeating failures (3+ in 24h):[/]")
            for item in offenders[:5]:
                console.print(f"  {escape(item['locus'])} / {escape(item['kind'])}: "
                              f"{item['count']}x, last {escape(item['last'])}")
                console.print(f"      [dim]{escape(item['reason'][:160])}[/]")
            console.print("[dim]  full report: myriaxon loci --dead-letters[/]")
    except Exception:  # noqa: BLE001
        pass
    return 0


def _run_async(coro):
    """Run a coroutine that was only wrapped to reuse the async `_status` body synchronously."""
    import asyncio

    return asyncio.run(coro)


def _run_one_dot(locus, agent, console, args) -> tuple[bool, bool]:
    """Run one Locus's full step budget. Returns `(ran_anything, failed)`.

    Mirrors the interactive pass deliberately, including `take_run` for atomic goal-plus-prompt selection and
    the "stop on a failed step" rule: a failed step is already recorded with its real reason by `locus_run`, and
    continuing would spend more tokens on the same obstacle.

    Durable across a crash. This is the level-4 requirement: without it, a kill at step 9 of 12 discarded
    every step and the next wake-up began the objective again. `run_state` checkpoints after each step
    and the loop resumes from the recorded step, so a long run survives the process that started it.
    """
    taken = locus.take_run()
    if taken is None:
        return False, False
    goal, first_prompt = taken

    # Mark this context as a Locus wake-up for the duration, so the completion gate can see that this turn carries
    # no human authority. Restored afterwards rather than left set: a stale marker would make a later interactive
    # turn refuse a completion a person actually asked for.
    previous_source = getattr(agent.tool_context, "locus_run_source", None)
    if agent.tool_context is not None:
        agent.tool_context.locus_run_source = "locus"
    try:
        state = run_state.begin_run(
            run_state.run_id_for(locus, goal), locus=locus, goal=goal,
            budget=locus.step_budget(),
        )
        # Published in the environment for the duration of the run, so code far from here can find the
        # ledger without a run-state parameter threaded through it. `messaging.send` is the first
        # consumer -- it is reached from the agent's tools, the notifier and the webhook, none of which
        # should have to know that runs exist in order to avoid double-sending a message.
        #
        # Restored in the `finally` rather than deleted: a nested run (a Locus delegating to a
        # sub-agent) must not blank the id its parent is still using.
        previous_run_id = os.environ.get("MYRIAXON_RUN_ID")
        os.environ["MYRIAXON_RUN_ID"] = state.run_id
        if state.resumable:
            console.print(
                f"[dim]-- resuming {escape(locus.name)} at step {state.step + 1} "
                f"(an earlier run was interrupted)[/]"
            )
        try:
            return _run_locus_steps(locus, goal, first_prompt, agent, console, args, state=state)
        finally:
            if previous_run_id is None:
                os.environ.pop("MYRIAXON_RUN_ID", None)
            else:
                os.environ["MYRIAXON_RUN_ID"] = previous_run_id
    finally:
        if agent.tool_context is not None:
            agent.tool_context.locus_run_source = previous_source


def _run_locus_steps(locus, goal, first_prompt, agent, console, args, state=None) -> tuple[bool, bool]:
    """The per-step loop, split out so the provenance marker can wrap it in a `try/finally`.

    Two different numbers, and conflating them was a bug in the first draft of the resume support:

      * `step`        -- the step number the Locus is ON, which drives the prompt and the verdict
      * `steps_done`  -- how much of the budget has been spent, which drives the remaining count

    A resumed run starts at a `step` above 1 but has spent the steps already recorded. Using `step` for
    both would give a resumed run a full budget again -- a task that crashed late could then cost twice
    what it was allowed to.
    """
    if not args.json:
        console.print(f"[dim]-- {escape(locus.name)} is working on: {escape(goal.title)}[/]")

    budget = locus.step_budget()
    resuming = state is not None and state.resumable
    # A step whose result was never recorded is REPEATED rather than skipped: it may or may not have
    # happened, and repeating is the safe direction for everything except an outbound side effect --
    # those are guarded separately by the side-effect ledger, which is the only place where "already
    # done" can be known rather than guessed.
    step = (state.step + 1) if resuming else 1
    steps_done = state.step if resuming else 0

    if resuming and not args.json:
        console.print(
            f"[dim]-- resuming at step {step} of {budget}; {max(0, budget - steps_done)} step(s) left; "
            f"repeating the interrupted step[/]"
        )

    ran = False
    executed_any = False
    # Watches the TURNS, not the tool calls. `loop_detection` already covers a repeating tool call; this
    # covers a repeating ANSWER, which is the gap that let a measured session spend six turns restating a
    # conclusion it had already given. See `myriaxon/agent_discipline.py` for that measurement.
    from . import agent_discipline

    repetition = agent_discipline.RepetitionDetector(
        run_id=run_state.run_id_for(locus, goal) if state is not None else str(getattr(locus, "id", ""))
    )
    while steps_done < budget:
        prompt = first_prompt if (step == 1 and not resuming) else locus.continuation_prompt(step)
        if not prompt:
            break
        # Read BEFORE the step replaces the set, so the check below can see what this step did.
        called_before = set(getattr(agent.tool_context, "tools_called_this_turn", ()) or ())
        try:
            text, failed = tool_registry.dispatch_tool(
                "locus_run",
                {"locus": locus.name, "message": prompt, "_proactive": True},
                agent.tool_context,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Headless locus %s failed", locus.name)
            locus.log("error", str(exc))
            locus_registry_touch(locus)
            # Filed where somebody can find it. A logger line answers "what happened at 14:02"; this
            # answers "what is broken right now", and the COUNT is what turns a repeated failure from noise
            # into a defect. A pointer rather than the traceback: the full text is already in the log, and a
            # store that grows to megabytes gets deleted to save space, taking the signal with it.
            try:
                from . import dead_letter

                dead_letter.record(
                    locus.name, goal.title, dead_letter.KIND_FAILED,
                    f"{type(exc).__name__}: {exc}",
                    pointer=f"step {step}, run {getattr(state, 'run_id', '')}",
                )
            except Exception:  # noqa: BLE001 - a diagnostic must never fail a dying run
                pass
            if state is not None:
                # Marked failed rather than left open: the next wake-up should not resume a run that
                # ended in an exception, because the exception is the thing that needs looking at.
                run_state.finish_run(state, outcome="failed")
            return ran, True
        ran = True
        called_now = set(getattr(agent.tool_context, "tools_called_this_turn", ()) or ())
        from . import loci as _loci_module

        if _loci_module.unverified_execution_note(goal.title, called_now | called_before):
            # An objective that asked for a command to be run, and no command was run. Recorded on the
            # goal rather than raised: the work may be genuinely complete and merely unproven, and
            # failing the run would throw away a correct fix. What must not happen is the false claim
            # going into the record unchallenged.
            locus.log("note", "objective asked for execution; none happened this step")
        if called_now & {"run_bash", "bash_output", "kill_bash"}:
            executed_any = True
        if args.json:
            print(json.dumps({"locus": locus.name, "goal": goal.title, "step": step,
                              "ok": not failed, "text": text[:2000]}, ensure_ascii=False))
        else:
            console.print(text if not failed else f"[error]{escape(str(text))}[/]")

        # Checkpoint AFTER the step's result is known, never before: a snapshot written first would
        # claim a step that had not happened, and a crash would then skip it.
        steps_done += 1
        if state is not None:
            run_state.record_step(state, steps_done, text=str(text or ""))

        # Did this turn say anything new? A turn that acted counts as progress even when it reads like the
        # last one; a turn that ran nothing and restated an earlier answer does not.
        verdict = repetition.record(
            str(text or ""),
            used_tools=bool(called_now & {"run_bash", "bash_output", "kill_bash", "write_file",
                                          "edit_file", "swarm_post", "swarm_claim"}),
        )
        if verdict.repeating:
            # STOPPING is the point. The measured session ran six more turns after the first restatement;
            # the useful outcome is to end the run with a reason rather than pay for the remaining steps.
            #
            # Recorded on the goal as `blocked` rather than failed: nothing broke, and the honest report is
            # that the agent ran out of ideas for this objective -- which is information a person needs and
            # a retry will not produce.
            locus.log("note", agent_discipline.format_verdict(verdict))
            if not args.json:
                console.print(f"[warning]{escape(verdict.message)}[/]")
            locus_registry_touch(locus)
            try:
                from . import dead_letter

                dead_letter.record(locus.name, goal.title, dead_letter.KIND_REPEATING,
                                   verdict.message, pointer=f"step {step}")
            except Exception:  # noqa: BLE001
                pass
            if state is not None:
                run_state.finish_run(state, outcome="repeating")
            return ran, False

        if failed:
            # Tell the NEXT step about it. Without this the continuation prompt asked for "the next concrete
            # step" with no indication that the last one had not worked, so an agent rediscovered the same
            # wall and stopped at it again -- the shape a benchmark run showed, where every step announced
            # that the shell was blocked. See `Locus.note_step_failure`.
            try:
                locus.note_step_failure(str(text or "")[:400])
            except Exception:  # noqa: BLE001
                pass
            locus_registry_touch(locus)
            if state is not None:
                run_state.finish_run(state, outcome="failed")
            return ran, True
        # A step that worked clears the streak, so the re-plan note does not outlive the problem it describes.
        try:
            locus.clear_step_failures()
        except Exception:  # noqa: BLE001
            pass
        step += 1

    locus_registry_touch(locus)
    if state is not None:
        # Budget exhausted with no failure: recorded as "budget" rather than "done", because the run
        # stopping and the objective being complete are different facts and the completion gate is the
        # only thing entitled to decide the second one.
        run_state.finish_run(state, outcome="budget" if steps_done >= budget else "done")
    return ran, False


def locus_registry_touch(locus) -> None:
    """Persist a Locus after a run. Wrapped so a persistence failure is logged, not raised mid-loop."""
    try:
        from . import loci as loci_module

        loci_module.get_registry().touch(locus)
    except Exception:
        logger.exception("Could not persist locus %s after its run", getattr(locus, "name", "?"))


def _build_agent_for_command(config, console, args):
    """The smallest agent construction that is still the real one.

    `_build_agent` is the same function the interactive path calls, so a Locus's tools, permission layer and
    provider handling here are identical to an interactive turn's. MCP, hooks and a scheduler are left off
    because a headless run has no interactive approval surface to answer for them -- and no REPL to schedule
    into, which is exactly what this command replaces.

    Parameter names are positional in `_build_agent`'s signature (`discovered_skills`, `loaded_hooks`, and so
    on), so they are passed by keyword here to keep a future reorder from silently swapping two list arguments.
    """
    from .skills import discover_skills
    from .subagents import discover_subagent_types

    built = _build_agent(
        config=config,
        model=args.model or config.model,
        console=console,
        yolo=bool(getattr(args, "yolo", False)),
        mcp_bridge=None,
        mcp_tool_defs=None,
        url_mcp_servers=[],
        discovered_skills=discover_skills(),
        discovered_subagent_types=discover_subagent_types(),
        loaded_hooks={},
        project_instructions=None,
        scheduler=None,
        settings=load_settings(),
        max_tokens=getattr(args, "max_tokens", None),
        effort=getattr(args, "effort", None),
        thinking=bool(getattr(args, "thinking", False)),
        mode=getattr(args, "mode", None) or DEFAULT_MODE,
        no_compact=bool(getattr(args, "no_compact", False)),
        profile=getattr(args, "profile", None),
        agent_mode=getattr(args, "agent_mode", None) or "single",
    )
    # An unattended Locus is exactly the case a sandbox exists for: nobody is watching, and it may
    # want to run commands. Turn the container on automatically when one is available, instead of
    # leaving a Locus unable to run anything because a flag was not passed.
    #
    # Measured before this: a confined Locus was refused EVERY shell command, including `echo`, with
    # "a shell command cannot be confined to this Locus's workspace, and no sandbox is available" --
    # while Docker 29.8.2 was running in WSL the whole time, invisible to a Windows PATH lookup.
    #
    # Only ever turned ON here: an explicit --no-sandbox or a settings-level disable still wins,
    # because a host that has been deliberately refused a container must not get one back from a
    # convenience default.
    if built is not None and getattr(built, "tool_context", None) is not None:
        from .tools import docker_available, sandbox_image

        if docker_available() and not getattr(args, "no_sandbox", False):
            built.tool_context.sandbox_enabled = True
            # Report WHICH image, because the difference is visible from inside: the default carries
            # only `pwsh`, so a locus that needs python or git will fail in a way that looks like a
            # broken tool rather than a missing capability. Saying the image up front makes an
            # avoidable failure a known limitation.
            console.print(f"[dim]Locus sandbox: {escape(sandbox_image())} "
                          f"(shell commands run inside it, network off)[/]")
    return built


def _run_bench_command(args, console) -> int:
    """`myriaxon bench` -- measure the harness, so speed is a number rather than a claim.

    Reports cold start, total CLI import, the slowest modules by **self** time, and tool dispatch overhead.

    The self-time column matters. `-X importtime` prints `self | cumulative | module`, and the cumulative
    figure includes everything imported beneath a module. Ranking by it credits a parent with its children's
    cost: this repository already produced one wrong headline figure (1.24 s) that way, when the real cost of
    that subsystem was a fraction of it. Self time is additive, so it says where the time actually is.
    """
    from . import benchmark

    report = benchmark.run_bench(runs=max(1, args.runs))

    if args.save:
        try:
            path = Path(args.save)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                            encoding="utf-8", newline="\n")
            console.print(f"[success]Baseline written to {escape(str(path))}[/]")
        except Exception as exc:  # noqa: BLE001
            console.print(f"[error]Could not write baseline: {escape(str(exc))}[/]")
            return 1

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        console.print("[bold]Myriaxon benchmark[/]")
        console.print()
        # Rendered with print, not rich markup: module paths contain characters rich would interpret.
        print(benchmark.render(report))

    if args.compare:
        try:
            baseline = json.loads(Path(args.compare).read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            console.print(f"[error]Could not read baseline: {escape(str(exc))}[/]")
            return 1
        console.print()
        console.print(f"[bold]Compared with {escape(args.compare)}[/]")
        for line in benchmark.compare(baseline, report):
            print(line)

    return 0


def _run_verify_command(args, console) -> int:
    """`myriaxon verify` -- probe the harness's own capabilities and report what actually works.

    Two things this harness asks you to trust are checked here rather than assumed:

    * **Skills** are instructions a future session reads and follows. Each is loaded through the same path the
      agent uses, then validated for structure, safety and staleness.
    * **Plugin tools** are third-party code. Each handler is *called* with a schema-valid payload, because a
      function object existing is not evidence that calling it works.

    Exit status is the interface: 0 when nothing that was probed failed, 1 otherwise. A "not probed" result is
    deliberately distinct from a pass, so a health check cannot report success for something it skipped.

    What a pass does *not* mean is printed with the results. A well-formed skill is not a correct one, and
    saying otherwise would be the most misleading thing this command could do.
    """
    from . import capability

    findings: list[capability.Finding] = []
    notes: list[str] = []

    skills: dict = {}
    try:
        from .skills import discover_skills

        skills = discover_skills()
    except Exception as exc:  # noqa: BLE001
        notes.append(f"Could not discover skills: {type(exc).__name__}: {exc}")

    # A ToolContext is needed to call a plugin handler. It is assembled here from defaults rather than taken
    # from a live agent, so `verify` works before any session exists -- which is when a user is most likely to
    # run it. Passed as a plain object because that is all a plugin handler is contracted to receive.
    probe_ctx = None
    try:
        from types import SimpleNamespace

        probe_ctx = SimpleNamespace(
            client=None,
            model="",
            max_tokens=0,
            console=console,
            yolo=False,
            tools=[],
            skills=skills,
            mode="verify",
        )
    except Exception as exc:  # noqa: BLE001
        notes.append(f"Plugin handlers not probed ({type(exc).__name__}: {exc}).")

    try:
        report = capability.verify_capabilities(skills=skills, ctx=probe_ctx)
        findings.extend(report.findings)
        notes.extend(report.notes)
    except Exception as exc:  # noqa: BLE001
        console.print(f"[error]Capability verification failed: {escape(str(exc))}[/]")
        return 1

    if args.tools:
        try:
            from . import tool_registry

            definitions = tool_registry.build_tool_defs() or []
            tool_findings = capability.verify_tool_defs(definitions)
            findings.extend(tool_findings)
        except Exception as exc:  # noqa: BLE001
            notes.append(f"Could not check tool definitions: {type(exc).__name__}: {exc}")

    # Recompute the verdict over everything gathered, including any tool definitions added above.
    failures = [f for f in findings if f.probed and not f.ok]
    passes = [f for f in findings if f.probed and f.ok]
    skipped = [f for f in findings if not f.probed]

    if args.json:
        print(json.dumps({
            "ok": not failures,
            "summary": f"{len(passes)} passed, {len(failures)} failed, {len(skipped)} not probed",
            "findings": [
                {"kind": f.kind, "name": f.name, "ok": f.ok, "probed": f.probed,
                 "detail": f.detail, "errors": f.errors, "warnings": f.warnings}
                for f in findings
            ],
            "notes": notes,
        }, ensure_ascii=False, indent=2))
    else:
        console.print("[bold]Capability verification[/]")
        console.print()
        show_all = args.verbose
        for finding in findings:
            # Default output shows problems and skips; passes are summarised unless --verbose.
            if finding.ok and finding.probed and not show_all:
                continue
            console.print(finding.render(verbose=args.verbose))
        console.print()
        for note in notes:
            console.print(f"  [dim]note: {escape(note)}[/]")
        colour = "success" if not failures else "error"
        console.print(
            f"[{colour}]{len(passes)} passed, {len(failures)} failed, "
            f"{len(skipped)} not probed[/]"
        )
        if passes and not show_all:
            console.print("  [dim](passing capabilities hidden; use --verbose to list them)[/]")

    if args.manifest:
        try:
            path = capability.write_manifest(args.manifest, findings, notes)
            console.print(f"  manifest written to {escape(str(path))}")
        except Exception as exc:  # noqa: BLE001
            console.print(f"[error]Could not write manifest: {escape(str(exc))}[/]")
            return 1

    if args.adopt:
        adopted, unchanged = _adopt_unprovenanced_skills(skills, console)
        if adopted:
            console.print(
                f"\n  [success]Marked {adopted} skill(s) as agent-written and unverified. They will now show "
                f"as [self-generated, unverified] in the skill listing rather than passing as reviewed.[/]"
            )
        console.print(f"  [dim]{unchanged} skill(s) already had provenance or could not be adopted.[/]")

    return 1 if failures else 0


def _adopt_unprovenanced_skills(skills: dict, console) -> tuple[int, int]:
    """Give provenance to skills written before provenance existed.

    Reports each change and each refusal rather than only a total, because this rewrites files in the user's
    configuration directory. A silent bulk edit of someone's skills would be exactly the kind of unannounced
    change this codebase keeps finding in other tools.
    """
    from . import skill_trust

    adopted = 0
    unchanged = 0
    for name, skill in sorted(skills.items()):
        changed, message = skill_trust.adopt_unprovenanced(skill.source)
        if changed:
            adopted += 1
            console.print(f"  [dim]adopted {escape(name)}: {escape(message)}[/]")
        else:
            unchanged += 1
    return adopted, unchanged


def _run_search_command(args, console) -> int:
    """`myriaxon search` -- report, set or test the web-search provider key.

    The `web_search` tool tells its caller when it is running on key-free backends, which cover
    reference material well and current events poorly. That message is only useful if the fix is
    reachable, and an environment variable the user has to discover is not reachable.
    """
    import json
    import os

    from . import constants, web_tools

    path = constants.CONFIG_DIR / web_tools.SEARCH_CONFIG_FILE

    def stored() -> dict:
        try:
            data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
            return data if isinstance(data, dict) else {}
        except Exception:  # noqa: BLE001
            return {}

    if args.clear:
        name = str(args.clear).strip().lower()
        data = stored()
        existed = data.pop(name, None) is not None
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8", newline="\n")
        console.print(
            f"[success]removed the stored key for {escape(name)}[/]" if existed
            else f"[warning]no stored key for {escape(name)}[/]"
        )
        return 0

    if args.set:
        if ":" not in args.set:
            console.print("[warning]--set wants PROVIDER:KEY, e.g. --set brave:BSAxxxx[/]")
            return 2
        name, _, key = args.set.partition(":")
        name = name.strip().lower()
        known = {p["name"] for p in web_tools.SEARCH_PROVIDERS}
        if name not in known:
            console.print(f"[warning]unknown provider {escape(name)}; "
                          f"choose from {', '.join(sorted(known))}[/]")
            return 2
        if not key.strip():
            console.print("[warning]the key is empty[/]")
            return 2
        data = stored()
        data[name] = key.strip()
        path.parent.mkdir(parents=True, exist_ok=True)
        # Written to the config directory, not to a shell profile: a key in a shell profile ends up in
        # every child process and in the history of whatever set it.
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8", newline="\n")
        console.print(f"[success]stored the {escape(name)} key[/] [dim]({escape(str(path))})[/]")
        console.print("[dim]It is read on the next search; no restart needed.[/]")
        return 0

    # ---- report ----------------------------------------------------------------------
    console.print(f"[bold]{APP_NAME} search[/]")
    console.print()
    active = web_tools.configured_search_provider()
    if active:
        provider, key = active
        source = "environment" if os.environ.get(provider["env"]) else f"stored in {path.name}"
        console.print(f"  active      : [success]{escape(provider['label'])}[/] ({source})")
        console.print(f"  key         : {key[:4]}â€¦{key[-4:]}" if len(key) > 10 else "  key         : set")
    else:
        console.print("  active      : [warning]none â€” key-free backends only[/]")
        console.print("                These cover reference material well and current events poorly.")
    console.print(f"  backends    : {', '.join(web_tools.search_backends())}")
    console.print()
    console.print("[bold]available providers[/]")
    for provider in web_tools.SEARCH_PROVIDERS:
        env_set = "env set" if os.environ.get(provider["env"]) else "â€”"
        file_set = "stored" if stored().get(provider["name"]) else "â€”"
        console.print(f"  {provider['name']:8} {provider['label']:24} {provider['free']:34} "
                      f"[dim]{provider['env']}: {env_set}, {file_set}[/]")
    console.print()
    console.print("[dim]Set one with:  myriaxon search --set brave:YOUR_KEY[/]")
    for provider in web_tools.SEARCH_PROVIDERS:
        console.print(f"[dim]  {provider['name']:8} {provider['docs']}[/]")

    if args.test:
        console.print()
        console.print("[bold]live test[/]")
        query = "what is the latest python release"
        text, is_error = web_tools.web_search(query, limit=3)
        console.print(f"  query: {escape(query)}")
        for line in str(text).splitlines()[:14]:
            console.print(f"  {escape(line)}")
        console.print("[error]the test failed[/]" if is_error else "[success]search returned results[/]")
        return 1 if is_error else 0
    return 0


def _bot_guidance(console) -> None:
    """What this is, and how it differs from a chat window. Printed, not assumed.

    A bot here is a persistent agent, and the difference is not cosmetic -- it is the reason the
    thing is useful. Saying so once, in the place someone looks when they start one, is cheaper than
    a page of documentation nobody opens.
    """
    console.print("[bold]what makes this different from a chat window[/]")
    console.print("  [dim]Â·[/] [bold]It has hands.[/] It reads and writes files, runs commands and")
    console.print("    searches the web in the workspace you point it at. A chat window can only talk.")
    console.print("  [dim]Â·[/] [bold]It remembers.[/] Role, instructions, objectives and progress")
    console.print("    live in loci.json and survive restarts -- close the terminal, it still knows.")
    console.print("  [dim]Â·[/] [bold]It keeps working.[/] Objectives persist, so it resumes where it")
    console.print("    stopped instead of starting over. A proactive bot also wakes on an interval.")
    console.print("  [dim]Â·[/] [bold]It is reachable.[/] The same agent answers here, in the")
    console.print("    dashboard and from your phone -- one agent, several doors.")
    console.print("  [dim]Â·[/] [bold]It can learn.[/] When it finds a reusable approach it saves a")
    console.print("    skill, and that skill is available to every later run.")
    console.print("  [dim]Â·[/] [bold]It asks.[/] An objective it cannot finish becomes `blocked` with")
    console.print("    the exact question, instead of a confident guess.")
    console.print()
    console.print("  The bot is this server's webhook, not a separate process: inbound messages arrive")
    console.print("  at [bold]POST /api/inbound[/] and replies go out through the provider's API.")
    console.print()


def _bot_tunnel_help(console, port: int) -> None:
    """How to make the webhook reachable, and the honest caveat about each.

    Printed from `myriaxon/tunnel.py` rather than written twice here: the commands it prints are the
    ones it actually runs, so the advice and the behaviour cannot drift apart.
    """
    from . import tunnel as tunnel_mod

    found = tunnel_mod.available()
    console.print("[bold]make the webhook reachable[/]")
    if found:
        names = ", ".join(str(t["name"]) for t in found)
        console.print(f"  Available here: [success]{escape(names)}[/]")
        console.print(f"  Run [bold]myriaxon bot --serve --tunnel[/] and the public URL is printed,")
        console.print("  ready to paste into your provider's webhook setting.")
    else:
        console.print("  Your provider has to POST to a public URL, and this machine is not one.")
    console.print()
    for line in tunnel_mod.install_hint(port):
        console.print(f"  {escape(line)}" if line else "")
    console.print("  [warning]Whichever you use, set a shared secret.[/] A public webhook with no")
    console.print("  secret and an allowlisted number is one leaked URL away from being a remote")
    console.print("  shell; the secret is the outer gate and the allowlist is the inner one.")


def _bot_ask(prompt: str, default: str = "") -> str:
    """One interactive question, tolerating a non-interactive stdin.

    Returns the default when there is nothing to read, which is what makes `--yes` and a piped
    shell both work: a wizard that hangs on a missing stdin is worse than one that picks a default.
    """
    try:
        if not sys.stdin or not sys.stdin.isatty():
            return default
    except Exception:  # noqa: BLE001
        return default
    try:
        answer = input(f"{prompt}{f' [{default}]' if default else ''}: ").strip()
    except (EOFError, KeyboardInterrupt):
        return default
    return answer or default


def _run_bot_command(args, console) -> int:
    """`myriaxon bot` -- create, configure, test or serve a bot.

    The bot is the web server's `/api/inbound` route, not a separate process, so "starting" it means
    running a server and pointing a provider at it. That is the whole shape, and it is printed rather
    than left to be inferred.
    """
    from . import bot as bot_module
    from . import bot_templates
    from . import constants
    from .messaging import load_messaging_config, save_messaging_config

    console.print(f"[bold]{APP_NAME} bot[/]  [dim]({constants.MESSAGING_FILE})[/]")
    console.print()

    # ---- list the templates and stop -------------------------------------------------
    if args.list:
        console.print(bot_templates.describe())
        console.print()
        console.print("[dim]Create one with:  myriaxon bot --template researcher[/]")
        return 0

    if args.templates:
        console.print(bot_templates.describe())
        console.print()
        _bot_guidance(console)
        _bot_tunnel_help(console, args.port)
        return 0

    # ---- create a bot ------------------------------------------------------------------
    created_name = ""
    if args.template:
        template = bot_templates.get(args.template)
        if template is None:
            console.print(f"[error]No template {escape(args.template)!r}. "
                          f"Known: {', '.join(bot_templates.TEMPLATE_KEYS)}[/]")
            return 2
        objectives = [o for o in (args.goal or []) if o.strip()] or None
        locus, was_created = bot_templates.create(
            template,
            proactive=True if args.proactive else None,
            interval=args.every or None,
            objectives=objectives,
        )
        created_name = locus.name
        verb = "created" if was_created else "updated"
        console.print(f"[success]{verb}[/] bot [bold]{escape(locus.name)}[/] "
                      f"[dim]({escape(template.role)})[/]")
        console.print(f"  {template.emoji} {escape(template.tagline)}")
        console.print(f"  objectives : {len(locus.goals)}")
        for goal in locus.goals:
            console.print(f"    [{goal.id}] {escape(goal.title)}")
        console.print(f"  proactive  : {locus.proactive}"
                      + (f" every {locus.interval_seconds}s" if locus.proactive else " (answers when asked)"))
        console.print(f"  workspace  : {escape(str(locus.workspace_path()))}")
        console.print()

    # ---- an interactive setup when nothing was specified -------------------------------
    interactive = not any([args.template, args.allow, args.provider, args.serve, args.self_test])
    if interactive and not args.yes:
        console.print("Nothing configured yet. Let's set one up.")
        console.print()
        console.print(bot_templates.describe())
        console.print()
        choice = _bot_ask(f"Which bot? ({'/'.join(bot_templates.TEMPLATE_KEYS)}, or blank to skip)",
                          "researcher")
        template = bot_templates.get(choice)
        if template is not None:
            locus, _ = bot_templates.create(template)
            created_name = locus.name
            console.print(f"[success]created[/] [bold]{escape(locus.name)}[/] "
                          f"[dim]({escape(template.role)})[/]")
        number = _bot_ask("Your phone number, in +country format (blank to skip)")
        if number:
            args.allow = [number]
        console.print()

    # ---- configuration -----------------------------------------------------------------
    patch: dict = {}
    if args.provider:
        patch["provider"] = args.provider
    if args.allow:
        current = load_messaging_config(constants.MESSAGING_FILE)
        merged = list(current.allow)
        for number in args.allow:
            if number not in merged:
                merged.append(number)
        patch["allow"] = merged
    if args.secret:
        patch["secret"] = args.secret
    if args.enable:
        patch["enabled"] = True
    if patch:
        save_messaging_config(constants.MESSAGING_FILE, patch)
        console.print(f"[success]configuration saved[/] [dim]({escape(str(constants.MESSAGING_FILE))})[/]")
        console.print()

    # ---- readiness ---------------------------------------------------------------------
    state = bot_module.readiness()
    console.print("[bold]readiness[/]")
    for line in state.lines():
        console.print(f"  {escape(line)}" if line else "")

    # ---- differences, and how to reach it ----------------------------------------------
    console.print()
    _bot_guidance(console)
    _bot_tunnel_help(console, args.port)

    # ---- self test ---------------------------------------------------------------------
    if args.self_test:
        console.print()
        console.print("[bold]self test[/]")
        if not state.ready:
            console.print("[warning]not ready; fix the items above first.[/]")
            return 1
        from pathlib import Path as _Path

        from .dashboard.server import DashboardApp

        workspace = _Path(args.workspace).expanduser().resolve() if args.workspace else _Path.cwd()
        config = resolve_config(console, reauth=args.reauth, ollama=args.ollama, model=args.model or "", vendor=getattr(args, "vendor", "") or "")
        app = DashboardApp(workspace=workspace, config=config)
        result = bot_module.self_test(app, deliver=not args.no_deliver)
        ok = bool(result.get("ok"))
        console.print(f"  stage    : {escape(str(result.get('stage', '?')))}")
        if result.get("sender"):
            console.print(f"  sender   : {escape(str(result['sender']))}")
        if result.get("answer"):
            answer = str(result["answer"]).strip().replace("\n", " ")[:200]
            console.print(f"  answered : {escape(answer)}")
        if result.get("reason"):
            console.print(f"  reason   : [warning]{escape(str(result['reason']))}[/]")
        if result.get("note"):
            console.print(f"  note     : [dim]{escape(str(result['note']))}[/]")
        console.print(
            "[success]the inbound path works end to end.[/]" if ok
            else "[error]the self test did not complete.[/]"
        )
        return 0 if ok else 1

    # ---- serve -------------------------------------------------------------------------
    if not args.serve:
        console.print()
        if created_name:
            console.print(f"Talk to it now with:  [bold]myriaxon[/] then [bold]/loci run {escape(created_name)} "
                          f"\"your request\"[/]")
            console.print(f"                    or the dashboard's Loci panel: "
                          f"http://127.0.0.1:{args.port}/")
        console.print("[dim]Nothing was started. Add --serve to run the server that receives "
                      "messages.[/]")
        return 0 if state.ready else 1

    console.print()
    console.print(f"[bold]serving on {args.host}:{args.port}[/] â€” webhook at /api/inbound")
    from pathlib import Path as _Path

    from .dashboard.server import serve as _serve_dashboard

    config = resolve_config(console, reauth=args.reauth, ollama=args.ollama, model=args.model or "", vendor=getattr(args, "vendor", "") or "")
    workspace = _Path(args.workspace).expanduser().resolve() if args.workspace else _Path.cwd()
    if not workspace.is_dir():
        console.print(f"[error]--workspace is not a directory: {escape(str(workspace))}[/]")
        return 2

    # Started before the server so the URL is known and printed alongside it, and stopped in the
    # `finally` so Ctrl+C does not leave a public tunnel running after the process it points at.
    tunnel = None
    if getattr(args, "tunnel", False):
        from . import tunnel as tunnel_mod

        current = bot_module.readiness()
        if not current.has_secret:
            # Refused rather than warned: this opens a public route into an agent that runs shell
            # commands, and the allowlist alone is one leaked URL from being insufficient.
            console.print()
            console.print("[error]refusing to open a tunnel without a shared secret.[/]")
            console.print("  Set one and try again:")
            console.print("    myriaxon bot --secret YOUR_LONG_RANDOM_VALUE --serve --tunnel")
            console.print("  [dim]The secret is the outer gate; the allowlist is the inner one.[/]")
            return 2
        if not current.ready:
            console.print()
            console.print("[warning]the bot is not ready yet:[/]")
            for problem in current.problems:
                console.print(f"  - {escape(problem)}")
            console.print("[dim]Starting the server anyway; fix the above for messages to work.[/]")

        console.print()
        console.print("[bold]opening a public tunnelâ€¦[/]")
        tunnel = tunnel_mod.Tunnel(args.port, on_line=lambda line: console.print(f"  [dim]{escape(line)}[/]"))
        if not tunnel.start():
            console.print(f"[warning]{escape(tunnel.error)}[/]")
            console.print("[dim]The server still starts; it is just not reachable from outside.[/]")
            tunnel = None
        else:
            url = tunnel_mod.webhook_url(tunnel.url)
            tunnel_mod.remember(tunnel.url, tunnel.tool)
            console.print()
            console.print(f"[success]public webhook:[/] [bold]{escape(url)}[/]")
            if tunnel_mod.wait_until_serving(url):
                console.print("[success]verified: the public URL reaches this server.[/]")
            else:
                console.print("[warning]the URL is not answering yet â€” give it a few seconds, then "
                              "check it in a browser before pasting it into your provider.[/]")
            console.print("[dim]Paste that into your provider's webhook setting.[/]")

    try:
        _serve_dashboard(
            workspace,
            config=config,
            host=args.host,
            port=args.port,
            mode=args.mode or DEFAULT_MODE,
            open_browser=not args.no_browser,
        )
    finally:
        if tunnel is not None:
            tunnel.stop()
            console.print("[dim]tunnel closed.[/]")
    return 0


def _mission_command(app, console) -> int:
    """`/mission` -- the Mission control snapshot as text.

    `mission_control.render_lines()` existed and was exported, and nothing called it: the changelog
    described a terminal view of the dashboard that had no way to reach. This is that way.
    """
    from .mission_control import render_lines, snapshot

    try:
        for line in render_lines(snapshot(app)):
            console.print(escape(line) if line else "")
    except Exception as exc:  # noqa: BLE001
        console.print(f"[error]could not read mission state: {type(exc).__name__}: {exc}[/]")
        return 1
    return 0


class _MissionShim:
    """The handful of attributes `mission_control.snapshot` reads, for a terminal session.

    The snapshot was written against the dashboard's application object. The terminal has no such
    object -- but it does have the same underlying things: a workspace, a config, a session manager
    and a start time. This presents them under the names the snapshot expects, so one implementation
    serves both surfaces instead of a second, subtly different terminal reporter.
    """

    def __init__(self, workspace=None, config=None) -> None:
        from pathlib import Path

        self.workspace = Path(workspace or Path.cwd())
        self.config = config
        self.started_at = _PROCESS_STARTED_AT
        try:
            from .dashboard.session import SessionManager

            self.sessions = SessionManager(self.workspace)
        except Exception:  # noqa: BLE001 - the snapshot reports a missing source as a health row
            self.sessions = None
        for name in ("plugins", "endpoints", "policy", "settings", "uploads", "uploads_dir"):
            setattr(self, name, None)


_PROCESS_STARTED_AT = __import__("time").time()
_MISSION_APP: "_MissionShim | None" = None


def _mission_app(config=None):
    """One shim per process, so its `started_at` means "since this session began"."""
    global _MISSION_APP
    if _MISSION_APP is None:
        _MISSION_APP = _MissionShim(config=config)
    if config is not None:
        _MISSION_APP.config = config
    return _MISSION_APP


def _text_panel(text: str):
    from rich.panel import Panel as _Panel
    from rich.text import Text as _Text

    return _Panel(_Text(text), border_style="tool.confirm.question")


def format_loci(loci_list) -> str:
    from . import loci as loci_module

    return loci_module.format_registry(loci_list)




def _show_deletion_idle(console) -> None:
    """Section 8.6: a brief `idle` with the window dark, not a sad face.

    Called after a deletion, and after the CONFIRMATION rather than before it, so the quiet lands on a completed
    action rather than on a question. A sad face here would be the harness expressing an opinion about the user
    removing their own data, which is exactly what the clause exists to prevent.
    """
    try:
        from . import loci_svg

        character_module.Character().state.apply(
            character_module.expression.SIGNAL_QUIET, now=_time.time())
        svg = loci_svg.render_svg("idle", size=96, window_dark=True)
        # Drawn only when a terminal can show it. Reporting the state in words otherwise, so a surface that cannot
        # render it does not silently skip the transition.
        if getattr(console, "is_terminal", False):
            console.print(svg)
        else:
            console.print("[dim]The window goes dark for a moment.[/]")
    except Exception:  # noqa: BLE001 - a cosmetic transition must never fail a deletion that already happened
        logger.debug("Deletion idle skipped", exc_info=True)

def _run_loci_stores(args, console) -> int:
    """`myriaxon loci <verb>` -- Loci's personal stores. Always returns an exit code, never a fall-through."""
    from . import loci_capabilities as store

    verb = args.verb
    text = " ".join(args.subject or []).strip()

    if verb in ("win", "wins"):
        if verb == "wins" or not text:
            wins = store.list_wins(limit=args.limit)
            if not wins:
                console.print('[dim]No wins yet. `myriaxon loci win "..."` records one.[/]')
                return 0
            for win in wins:
                console.print(f"  {escape(win.render())}")
            return 0
        recorded = store.add_win(text, about=args.about)
        if recorded is None:
            console.print("[error]A win needs some text.[/]")
            return 1
        total = len(store.list_wins(limit=10_000))
        console.print(f"[success]Added to the wins jar.[/] {escape(recorded.text)}")
        console.print(f"[dim]{total} win(s) recorded.[/]")
        return 0

    if verb in ("mood", "ground", "reframe", "overwhelmed", "kind", "connect", "rest"):
        from . import companion

        if verb == "mood":
            # No text: LIST what was recorded, with the opt-in for the pattern view.
            if not text:
                moods = companion.list_moods(limit=args.limit)
                if not moods:
                    console.print("[dim]No check-ins yet.[/]")
                    console.print("[dim]`myriaxon loci mood low` records one. "
                                  "Levels: " + ", ".join(companion.MOODS) + "[/]")
                    return 0
                for mood in moods:
                    console.print(f"  {escape(mood.render())}")
                if args.patterns:
                    pattern = companion.reflect_mood(opted_in=True, days=args.days)
                    if pattern.lower_on:
                        console.print(f"[dim]{escape(pattern.render())}[/]")
                    else:
                        console.print("[dim]Nothing stands out across those days.[/]")
                else:
                    console.print("[dim]Add --patterns to look at these over time.[/]")
                return 0
            recorded = companion.check_in(text, note=args.about)
            if recorded is None:
                console.print(f"[error]{escape(text)} is not one of the five levels.[/]")
                console.print("[dim]Use one of: " + ", ".join(companion.MOODS) + "[/]")
                return 1
            console.print(f"[success]Noted.[/] {escape(recorded.mood)}")
            if recorded.mood in companion.LOWER_MOODS:
                # Section 4's `caring` row: "User shares sadness, stress, grief". A check-in at this level is that,
                # and the face following the content is the whole point of having a face.
                # A bare Character rather than `for_agent`: a CLI subcommand has no agent, and registering a
                # throwaway one in the per-agent registry would leave an entry behind for a process that is about to
                # exit. Terminal() is detected here, which is cheap and is what decides whether the face renders.
                character_module.Character().state.apply(
                    character_module.expression.SIGNAL_MOOD_LOW, now=_time.time())
            return 0

        if verb == "ground":
            exercise = companion.start_grounding(text or "5-4-3-2-1")
            for line in exercise.render().splitlines():
                console.print(escape(line))
            return 0

        if verb in ("reframe", "overwhelmed", "kind"):
            if not text:
                console.print(f"[error]Nothing to work from. `myriaxon loci {verb} \"...\"`[/]")
                return 1
            builder = {
                "reframe": companion.reframe_prompt,
                "overwhelmed": companion.overwhelm_prompt,
                "kind": companion.compassion_prompt,
            }[verb]
            console.print(escape(builder(text)))
            console.print()
            console.print("[dim]Paste that into a session.[/]")
            return 0

        if verb == "connect":
            console.print(escape(companion.connection_prompt(who=args.who)))
            return 0

        if verb == "rest":
            nudge = companion.rest_nudge()
            console.print(escape(nudge) if nudge else "[dim]Not night time -- nothing to say here.[/]")
            return 0

    if verb == "todo":
        # The documented shape is `loci todo add "..."`, so the FIRST word of `subject` is the action unless it is
        # an id or plain text. Recognising the small action set is better than requiring a flag, because the design
        # document writes the command without one.
        action, rest = (args.subject[0].lower() if args.subject else ""), list(args.subject[1:])
        if action in ("add", "new"):
            recorded = store.add_task(" ".join(rest), due=args.day or "")
            if recorded is None:
                console.print("[error]A task needs some text.[/]")
                return 1
            console.print(f"[success]Added.[/] {escape(recorded.text)}")
            console.print(f"[dim]{escape(recorded.render())}[/]")
            return 0

        if action in ("done", "finish", "complete") and rest:
            finished = store.complete_task(rest[0])
            if finished is None:
                console.print(f"[error]No open task with id {escape(rest[0])}.[/]")
                return 1
            console.print(f"[success]Done.[/] {escape(finished.text)}")
            return 0

        if action in ("snooze", "later") and rest:
            moved = store.snooze_task(rest[0], days=args.days)
            if moved is None:
                console.print(f"[error]No task with id {escape(rest[0])}.[/]")
                return 1
            # The DATE, not how late it was. Section 8.1 asks for gentle wording and section 8.5 forbids guilt.
            console.print(f"[success]Moved.[/] {escape(moved.text)} -- now due {escape(moved.due)}")
            return 0

        if action in ("forget", "remove", "delete") and rest:
            dropped = store.forget_task(rest[0])
            if dropped is None:
                console.print(f"[error]No task with id {escape(rest[0])}.[/]")
                return 1
            console.print(f"[success]Removed.[/] {escape(dropped.text)}")
            return 0

        if action in ("reopen", "undo") and rest:
            again = store.reopen_task(rest[0])
            if again is None:
                console.print(f"[error]No finished task with id {escape(rest[0])}.[/]")
                return 1
            console.print(f"[success]Reopened.[/] {escape(again.text)}")
            return 0

        # Listing. `--done` shows what is finished; otherwise what is open.
        tasks = store.list_tasks(include_done=args.done or args.all, limit=args.limit)
        if not tasks:
            console.print("[dim]Nothing on the list.[/]")
            console.print('[dim]`myriaxon loci todo add "..."` records one.[/]')
            return 0
        for task in tasks:
            style = "dim" if task.done else ""
            line = f"  {task.render()}"
            console.print(f"[{style}]{escape(line)}[/]" if style else escape(line))
        return 0

    if verb == "today":
        summary = store.today_summary()
        # Rendered as plain text through one function rather than assembled here, so the wording has a single home
        # and cannot drift between the CLI and any other surface.
        for line in store.render_today(summary).splitlines():
            console.print(escape(line))
        return 0

    if verb == "plan":
        dump = " ".join(args.subject or []).strip()
        if not dump:
            console.print('[error]Nothing to plan from. `myriaxon loci plan "..."`[/]')
            console.print("[dim]A brain-dump, in your own words. Loci turns it into at most three priorities.[/]")
            return 1
        # A PROMPT is printed, not a plan: choosing what matters is the one part a model should not do silently on
        # the user's behalf. Section 8.1's row says "Asks before adding more", which is this shape.
        prompt = store.plan_prompt(dump, limit=3)
        console.print(escape(prompt))
        console.print()
        console.print("[dim]Paste that into a session, or run `myriaxon run` with it.[/]")
        return 0

    if verb == "remember":
        if not text:
            console.print('[error]Nothing to remember. `myriaxon loci remember "..." --room work`[/]')
            return 1
        memory = store.remember(text, room=args.room or "ideas")
        if memory is None:
            console.print("[error]Nothing to remember.[/]")
            return 1
        console.print(f"[success]Remembered.[/] [{escape(memory.room)}] {escape(memory.text)}")
        return 0

    if verb == "recall":
        found = store.recall(text, room=args.room, limit=args.limit)
        if not found:
            # Stated plainly, then the rooms are listed so the next attempt has somewhere to go. Not padded into
            # encouragement -- section 1.3 forbids false reassurance.
            console.print("[dim]Nothing matches.[/]")
            for room, count in store.rooms().items():
                console.print(f"[dim]  {escape(room)}: {count}[/]")
            return 0
        for memory in found:
            console.print(f"  {escape(memory.render())}")
        return 0

    if verb == "forget":
        # The whole-store wipe is signalled by `--confirm` ALONE, with no subject. Requiring it, and requiring the
        # absence of an id, is what makes an accidental wipe impossible: `loci forget walk` deletes one entry, and
        # there is no spelling that deletes everything without the word `confirm` in it.
        if not text:
            if not args.confirm:
                console.print("[warning]`loci forget` with nothing to forget would delete every memory, win,")
                console.print("[warning]habit, journal entry and focus session. Re-run with --confirm.[/]")
                return 1
            counts = store.forget_all(confirm=True)
            total = sum(counts.values())
            console.print(f"[success]Removed {total} entr{'y' if total == 1 else 'ies'}.[/]")
            for kind, count in sorted(counts.items()):
                console.print(f"[dim]  {kind}: {count}[/]")
            # The wipe matters MORE of the two: deleting everything is the moment someone is most likely to
            # feel they have thrown something away, and section 8.6 answers that with a brief dark window
            # rather than a reaction.
            _show_deletion_idle(console)
            return 0
        removed = store.forget(text) or store.forget_win(text) or store.forget_journal(text)
        if removed is None:
            console.print(f"[error]Nothing with id {escape(text)!r}.[/]")
            console.print("[dim]Ids come from `loci recall`, `loci wins` and `loci journal`.[/]")
            return 1
        console.print(f"[success]Forgotten.[/] {escape(getattr(removed, 'text', '')[:80])}")
        # Section 8.6, and AFTER the confirmation: the quiet lands on a completed action rather than on a
        # question.
        _show_deletion_idle(console)
        return 0

    if verb in ("habit", "habits"):
        if verb == "habits" or not text:
            habits = store.list_habits()
            if not habits:
                console.print('[dim]No habits tracked. `myriaxon loci habit "walk"` starts one.[/]')
                return 0
            for habit in habits:
                console.print(f"  {escape(habit.render())}")
            return 0
        # An existing name is marked done; a new one is created. One verb, because the common case -- "I did it" --
        # should not need a second command to learn.
        marked = store.mark_habit(text, day=args.day)
        if marked is not None:
            console.print(f"[success]Marked.[/] {escape(marked.render())}")
            return 0
        created = store.declare_habit(text)
        if created is None:
            console.print("[error]A habit needs a name.[/]")
            return 1
        console.print(f"[success]Tracking {escape(created.name)!r}.[/] Mark it done with the same command.")
        return 0

    if verb == "journal":
        if not text:
            entries = store.list_journal(limit=args.limit)
            if not entries:
                console.print(f"[dim]Nothing written yet. Today's prompt:[/] {escape(store.journal_prompt())}")
                return 0
            for entry in entries:
                # Printed raw: journal text is the user's own words and must not be reflowed or interpreted
                # as console markup.
                console.print(entry.render(), markup=False, highlight=False)
                console.print("")
            return 0
        entry = store.write_journal(text, prompt=args.about)
        if entry is None:
            console.print("[error]Nothing to write.[/]")
            return 1
        # Stored and NOT condensed. See the note in `loci_capabilities`: its absence is the privacy feature.
        console.print(f"[success]Written.[/] {len(store.list_journal(limit=10_000))} entr(ies) now.")
        return 0

    if verb == "focus":
        # `loci focus "task"` starts one; `loci focus` reports; `loci focus end` stops early.
        #
        # The running session drives the character's `focus` expression, which is the whole reason this capability is
        # in the companion layer rather than left to a shell timer: a timer that cannot change the face is a timer,
        # and one that can is the character noticing that you are working.
        if args.confirm or (args.subject and args.subject[0].lower() == "end"):
            ended = store.end_focus()
            if ended is None:
                console.print("[dim]No focus session running.[/]")
                return 0
            spent = int((_time.time() - ended.started_at) // 60)
            console.print(f"[success]Focus ended.[/] {escape(ended.task or '(untitled)')} -- {spent} min")
            return 0
        if not text:
            running = store.current_focus()
            if running is None:
                console.print('[dim]No focus session running. `myriaxon loci focus "what you are doing"`[/]')
                for session in store.focus_history(limit=5):
                    console.print(f"[dim]  {escape(session.render())}[/]")
                return 0
            left = running.remaining_seconds()
            filled = int(running.progress() * 20)
            console.print(f"[bold]{escape(running.task or '(untitled)')}[/]")
            console.print(f"  [{'#' * filled}{'.' * (20 - filled)}] {left // 60}:{left % 60:02d} left")
            return 0
        session = store.start_focus(text, minutes=args.days or store.DEFAULT_FOCUS_MINUTES)
        console.print(f"[success]Focus started.[/] {escape(session.task)} -- {session.minutes} min")
        console.print("[dim]`myriaxon loci focus end` stops early.[/]")
        return 0

    if verb == "reflect":
        recap = store.reflect(days=args.days)
        if recap["empty"]:
            console.print(f"[dim]Nothing recorded in the last {recap['days']} days.[/]")
            return 0
        console.print(f"[bold]The last {recap['days']} days[/]")
        console.print(f"  wins       {recap['wins']}")
        console.print(f"  memories   {recap['memories']}")
        console.print(f"  journal    {recap['journal_entries']}")
        for habit in recap["habits"]:
            # The spec's wording rule, in code: a ratio, never "streak broken".
            console.print(f"  {escape(habit['name'])}: {habit['done']} of the last {habit['of']} days")
        for example in recap["wins_examples"]:
            console.print(f"[dim]  - {escape(example)}[/]")
        return 0

    # No verb: say what exists rather than silently doing nothing.
    console.print("[bold]Loci's stores[/]")
    counts = store.summary()
    console.print(f"  wins       {counts['wins']}")
    console.print(f"  memories   {counts['memories']}")
    console.print(f"  habits     {counts['habits']}")
    console.print(f"  journal    {counts['journal']}")
    console.print(f"  focus      {'running' if counts['focus_running'] else 'idle'}")
    console.print(f"[dim]  kept in {escape(counts['directory'])}[/]")
    console.print("[dim]Verbs: win, remember, recall, forget, habit, journal, reflect, focus.[/]")
    return 0


def main(argv: list[str] | None = None) -> int:
    # Force UTF-8 stdout/stderr regardless of the OS's ambient console codepage -- confirmed
    # live this was a real, serious bug: on any Windows console using a legacy codepage (e.g.
    # cp1252 -- common on corporate-managed images and non-interactive/piped contexts), Rich's
    # own spinner (ThreeColorSpinner's braille frames, theme.py) or literally any Unicode
    # character in a model's own response could crash the entire turn with an unhandled
    # UnicodeEncodeError. errors="replace" is a deliberate belt-and-suspenders: even if some
    # other stream still can't represent a character, it's swapped for a placeholder instead of
    # crashing the whole process.
    #
    # NOTHING IS DONE WHEN THE ENCODING IS ALREADY UTF-8, and that guard is a fix rather than a
    # micro-optimisation. Reconfiguring a stream that is already correct is a WRITE to somebody else's
    # object: under pytest, `sys.stdout` is the capture stream, and calling `reconfigure` on it was
    # reliably followed by `ValueError: I/O operation on closed file` raised from inside Rich
    # (`rich/console.py` -> `self.file.write`) in a LATER test. Measured: the failure moved between
    # `test_the_ollama_flag_without_ollama_explains_and_exits` and `test_a_saved_ollama_choice_is_kept`
    # across runs, which is why no single test file ever reproduced it -- it needs an accumulated run to
    # reach a stream that has been reconfigured and then closed.
    #
    # Every environment this actually needs to fix is a legacy codepage, where the encoding is NOT utf-8,
    # so the guard cannot skip a case the loop exists for.
    for _stream in (sys.stdout, sys.stderr):
        try:
            if (getattr(_stream, "encoding", "") or "").lower().replace("-", "") == "utf8":
                continue
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass  # not a real TextIOWrapper (e.g. redirected to something unusual) -- best-effort

    configure_logging()
    load_dotenv_into_environ()
    args = parse_args(argv)
    settings = load_settings()
    # -p/--print --output-format json is meant for scripting -- the whole point is a single,
    # cleanly parseable JSON object on stdout (see the print() call further below). Confirmed
    # live this was broken: setup-phase warnings (e.g. McpBridge.start()'s "mcp package isn't
    # installed" notice) print on this same `console` well before the json_mode branch is ever
    # reached, so a naive json.loads(stdout) in a calling script would choke on the extra text
    # line(s) preceding the actual JSON. Deciding quiet=True this early -- instead of the old
    # approach of building a second `quiet_console` and swapping it in just for the agent, after
    # setup had already printed to the original one -- means every setup-phase warning is
    # suppressed too, not just the agent's own streamed output.
    json_mode = bool(args.prompt) and args.output_format == "json"
    console = get_console(settings.theme, quiet=json_mode)

    if args.command == "auth":
        run_auth_wizard(console, load_config())
        return 0

    if args.command == "web":
        # The dashboard binds a port and serves the same agent. Handled before the normal startup so
        # it needs no prompt, no banner, and no REPL wiring.
        from pathlib import Path as _Path

        from .dashboard.server import serve as _serve_dashboard

        config_for_web = resolve_config(console, reauth=args.reauth, ollama=args.ollama, model=args.model or "", vendor=getattr(args, "vendor", "") or "")
        workspace = _Path(args.workspace).expanduser().resolve() if args.workspace else _Path.cwd()
        if not workspace.is_dir():
            console.print(f"[error]--workspace is not a directory: {escape(str(workspace))}[/]")
            return 2
        _serve_dashboard(
            workspace,
            config=config_for_web,
            host=args.host,
            port=args.port,
            # Settings carry only `enforced_mode`; the --mode flag already defaults to DEFAULT_MODE.
            # `args.mode` may come from the global flag or from `web --mode`; the subcommand's value
            # is used when given, so both spellings work and neither is silently ignored.
            mode=settings.enforced_mode or args.mode or DEFAULT_MODE,
            open_browser=not args.no_browser,
        )
        return 0

    if args.command == "bot":
        return _run_bot_command(args, console)

    if args.command == "search":
        return _run_search_command(args, console)

    if args.command == "ollama":
        return _run_ollama_command(args, console)

    if args.command == "audit":
        return _run_audit_command(args, console)

    if args.command == "verify":
        return _run_verify_command(args, console)

    if args.command == "bench":
        return _run_bench_command(args, console)

    if args.command == "loci":
        # With a verb (`myriaxon loci win ...`) it is Loci's own stores; without one it is the always-on agents.
        if args.verb:
            return _run_loci_stores(args, console)
        return _run_loci_command(args, console)

    if args.command == "instructions":
        return _run_instructions_command(args, console)

    if args.command == "roles":
        return _run_roles_command(args, console)

    if args.command == "skills":
        return _run_skills_command(args, console)

    if args.command == "doctor":
        return _run_doctor_command(args, console)

    if args.command == "branch":
        return _run_branch_command(args, console)

    if args.command == "export":
        return _run_export_command(args, console)

    if args.command == "avatar":
        return _run_avatar_command(args, console)

    config = resolve_config(console, reauth=args.reauth, ollama=args.ollama, model=args.model or "", vendor=getattr(args, "vendor", "") or "")
    _mark("resolve_config")
    model = args.model or config.model
    # CLI flag beats the environment variable beats auto-detection.
    if getattr(args, "gemini_protocol", None):
        config.gemini_protocol = args.gemini_protocol

    if settings.enforced_provider and config.provider != settings.enforced_provider:
        console.print(
            f"[error]Organization policy (settings.json) requires provider "
            f"'{settings.enforced_provider}', but the active configuration uses "
            f"'{config.provider}'. Run '{APP_NAME} auth' and choose "
            f"'{settings.enforced_provider}' to continue.[/]"
        )
        return 1
    if settings.enforced_mode and args.yolo:
        console.print(
            f"[warning]--yolo is disabled by organization policy (mode locked to "
            f"'{settings.enforced_mode}').[/]"
        )
        args.yolo = False

    # Start loading the provider's SDK now, in a background thread, and let it overlap the setup
    # work below (MCP discovery, skills, hooks, session scan). Measured on this machine, a provider
    # SDK import costs 12-55s, so overlapping it is the difference between paying that before the
    # first prompt and not paying it at all.
    preload_provider_sdk(config.provider)

    # Plugins load before the agent is built so their tools are present from the first turn.
    # `project_root=None` makes plugins use the same convention as settings (the current directory),
    # rather than inventing a second notion of "the project".
    from .constants import CONFIG_DIR
    from .plugins import load_plugins

    plugin_registry = load_plugins(CONFIG_DIR, None)
    _failed_plugins = plugin_registry.summary()["failed"]
    if _failed_plugins:
        console.print(
            f"[warning]Plugin(s) failed to load: {escape(', '.join(_failed_plugins))} "
            "-- see /plugins for the reason[/]"
        )

    mcp_config = load_mcp_config()
    _mark("mcp_config")
    # Both transports count. This previously checked only stdio_servers, so a configured remote server
    # (Figma, Microsoft 365, GitHub, any hosted MCP) was loaded from mcp_servers.json and then never
    # started -- it silently contributed no tools.
    _has_mcp_servers = bool(mcp_config["stdio_servers"] or mcp_config.get("url_servers"))
    # Despite the name this is every MCP tool, from both transports -- McpBridge.start() returns
    # stdio and remote HTTP/SSE tools in one list. The old name `mcp_tool_defs` was referenced here
    # but never assigned anywhere, so this call raised NameError on every launch, before the banner
    # and outside the try below, which surfaced as a raw traceback.
    mcp_bridge = McpBridge(console) if _has_mcp_servers else None
    mcp_tool_defs = (
        mcp_bridge.start(mcp_config["stdio_servers"], mcp_config.get("url_servers")) if mcp_bridge else []
    )
    discovered_skills = discover_skills()
    _mark("discover_skills")
    discovered_commands = commands.discover_commands(console=console)
    discovered_subagent_types = discover_subagent_types()
    loaded_hooks = load_hooks_config()
    project_instructions = load_project_instructions()
    scheduler = Scheduler()
    _mark("discovery (commands/agents/hooks/scheduler)")

    initial_messages = None
    if not args.no_resume and not args.prompt:
        saved = load_session()
        if saved is not None and saved.get("provider") == config.provider:
            turn_count = sum(
                1 for m in saved.get("messages", []) if m.get("role") == "user" or m.get("type") == "user_input"
            )
            if select_option(
                console,
                f"Resume previous session in this folder ({saved.get('model')}, ~{turn_count} turn(s))?",
                [("y", "Yes"), ("n", "No")],
                default="y",
                cancellable=True,
            ) == "y":
                initial_messages = sanitize_resumed_messages(saved.get("messages"))
                model = saved.get("model", model)
                args.mode = saved.get("mode", args.mode)
                args.effort = saved.get("effort", args.effort)
                args.thinking = saved.get("thinking", args.thinking)

    def _apply_sandbox_setting(built_agent):
        if built_agent is not None:
            built_agent.tool_context.sandbox_enabled = args.sandbox or settings.enforced_sandbox
        return built_agent

    def rebuild_fn(new_config: Config, keep_messages: list[dict] | None):
        return _apply_sandbox_setting(_build_agent(
            new_config,
            new_config.model,
            console,
            args.yolo,
            mcp_bridge,
            mcp_tool_defs,
            mcp_config["url_servers"],
            discovered_skills,
            discovered_subagent_types,
            loaded_hooks,
            project_instructions,
            scheduler,
            settings,
            max_tokens=args.max_tokens,
            effort=args.effort,
            thinking=args.thinking,
            mode=args.mode,
            no_compact=args.no_compact,
            initial_messages=keep_messages,
            profile=args.profile,
            agent_mode=args.agent_mode,
        ))

    agent = _apply_sandbox_setting(_build_agent(
        config,
        model,
        console,
        args.yolo,
        mcp_bridge,
        mcp_tool_defs,
        mcp_config["url_servers"],
        discovered_skills,
        discovered_subagent_types,
        loaded_hooks,
        project_instructions,
        scheduler,
        settings,
        max_tokens=args.max_tokens,
        effort=args.effort,
        thinking=args.thinking,
        mode=args.mode,
        no_compact=args.no_compact,
        initial_messages=initial_messages,
        profile=args.profile,
        agent_mode=args.agent_mode,
    ))

    if agent is None:
        if mcp_bridge is not None:
            mcp_bridge.shutdown()
        return 1
    _mark("agent built (provider SDK + client)")

    try:
        if args.prompt:
            error_message: str | None = None
            try:
                agent.run_turn(commands.resolve_input(args.prompt, discovered_commands))
                _mark("turn complete (model + tools)")
            except Exception as exc:  # noqa: BLE001 -- classified below, never swallowed
                # Isinstance checks rather than a chain of `except <tuple>:` clauses. Both work, but
                # `except _auth_errors() as exc:` evaluates its expression BEFORE the raise, which
                # means the provider SDK gets imported on every run whether or not anything fails --
                # measured at ~16s of duplicate google.genai import on a successful Gemini run. Here
                # the SDK modules are only consulted once an exception actually exists.
                error_message = _classify_run_error(exc, json_mode=json_mode, console=console, logger=logger)
                if error_message is None:
                    return 1  # already reported in a provider-specific way

            if json_mode:
                import json as _json

                print(
                    _json.dumps(
                        {
                            "provider": config.provider,
                            "model": agent.model,
                            "text": agent.last_assistant_text() if error_message is None else None,
                            "usage": {
                                "input_tokens": agent.usage_totals.input_tokens,
                                "output_tokens": agent.usage_totals.output_tokens,
                                "request_count": agent.usage_totals.request_count,
                            },
                            "error": error_message,
                        }
                    )
                )
            elif error_message is not None:
                console.print(f"[error]{escape(error_message)}[/]")

            _print_timing()
            return 1 if error_message is not None else 0

        print_banner(console, config)
        if config.provider == PROVIDER_OLLAMA:
            from . import ollama as ollama_module

            for note in ollama_module.startup_notes(config.base_url, agent.model):
                console.print(f"[warning]{escape(note)}[/]")
            # NO model warm-up here, deliberately, and the measurement is the reason. Evicting the
            # model and timing a real first turn:
            #     without a warm-up   17.2s
            #     with one            22.5s   <-- slower
            # Waiting for the load was the problem: Ollama cancels an in-flight load as soon as the
            # client disconnects, so it cannot be fired and forgotten, and any request that lives long
            # enough to complete the load blocks the turn that follows it. A warm-up that makes the
            # first turn slower is worse than no warm-up, so there is none. The cost is real and
            # belongs to the model, not the harness; a cloud vendor removes it entirely (`/vendor
            # netra`).
        repl(agent, console, config, rebuild_fn, discovered_commands, settings)
        return 0
    except KeyboardInterrupt:
        # Ctrl-C at a prompt that has no way back (a sub-prompt in a command, the setup wizard): a clean exit, not a traceback.
        console.print("\n[dim]Interrupted.[/]")
        return 130
    except EOFError:
        # Input was closed (Ctrl-D, or a pipe that ended) where an answer was required. Say so; it is not a crash.
        console.print("\n[dim]Input closed; exiting.[/]")
        return 0
    except Exception:
        logger.exception("Unhandled error in main()")
        console.print(f"[error]Unexpected error -- details logged to {escape(str(LOG_FILE))}[/]")
        return 1
    finally:
        _mark("turn handler finished")
        if mcp_bridge is not None:
            mcp_bridge.shutdown()
        _mark("mcp bridge shut down")
        _print_timing()


if __name__ == "__main__":
    sys.exit(main())

