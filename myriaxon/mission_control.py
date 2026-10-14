"""Mission control: one honest snapshot of what this machine is actually doing.

The dashboard's Mission control view is a *report*, not a dashboard-shaped decoration. That
distinction is the whole design of this module: every number here is read from a real source --
the live sessions this process owns, the loci registry, the memory store's SQLite index, the
audit log, ``settings.json`` -- and anything that cannot be read is reported as ``0``/``[]`` with
a ``health.items`` row saying why. Nothing is estimated, sampled, demoed or remembered from a
previous run, because a view whose job is to tell you the truth about the machine is worthless
the moment one of its tiles is plausible instead of correct.

Why it is shaped this way
-------------------------
* **Never raises.** This runs on the dashboard's request path, where the browser polls it every
  few seconds. A 500 because the memory database happened to be locked is strictly worse than a
  payload that says "memory unavailable" and keeps reporting the other nine panels. Every source
  is wrapped individually (:class:`_Issues`, :func:`_guard`) so one broken reader degrades one
  section.
* **No network.** The dashboard is local and must stay responsive with the wifi off, so the
  probes are limited to things that answer instantly: is a credential configured, is an SDK
  importable, is a config file present, is a directory writable, is git on PATH.
* **Process-scoped usage, and it says so.** Token counts come from each live session's agent
  (``usage.UsageTotals``). There is no persisted usage ledger in this codebase, so a fresh
  process honestly reports zeros rather than inventing a running total: "spent this process" is
  what the number means, and ``usage.started_at`` says since when.
* **Runs are the sessions this process owns.** A locus being run by *another* process (the
  terminal's proactive pass) has no session here and no per-run turn counter anywhere this module
  can read, so it does not appear as a run row: reporting "0 turns, 0s" for work that has been
  going for minutes would be a plausible lie in the exact field the panel renders. Its activity
  still reaches ``trace``, and its schedule still reaches ``schedules``.
* **Read-only.** The memory store is opened with SQLite's ``mode=ro`` URI, nothing here writes
  state, and every path is read through the ``constants`` module so a test can point it at a
  scratch directory. A status view that mutates the state it is reporting on is a bug waiting to
  be blamed on something else. The one file this module touches is its own writability probe
  (created and deleted to answer "can the agent write here", exactly as
  ``dashboard.paths.is_writable`` does), and it is cached per path so a five-second poll does not
  rewrite it.

Assembled server-side, never in the browser: the view must show what the machine reports, and a
client that computed its own totals could disagree with the API it is displaying.

This module is imported *by* ``dashboard/server.py`` (``/api/mission-control``) and must never
import it back.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sqlite3
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import constants
from .config import PROVIDER_ENV_KEY_FALLBACK
from .constants import (
    DEFAULT_MODE,
    ENV_API_KEY,
    PROVIDER_ANTHROPIC,
    PROVIDER_DEEPSEEK,
    PROVIDER_GEMINI,
    PROVIDER_NETRA,
    PROVIDER_OPENAI_COMPAT,
    PROVIDER_OPENROUTER,
)
from .loci import GOAL_BLOCKED, get_registry
from .mcp_client import load_mcp_config
from .messaging import load_messaging_config
from .settings import load_settings
from .skills import BUILTIN_SKILLS_DIR, discover_skills

# Bounds. A trace feed that grows without limit is a memory leak with a nice name, and the
# browser re-renders this list every five seconds.
TRACE_LIMIT = 200
SESSION_TRACE_EVENTS = 30
LOCUS_ACTIVITY_LIMIT = 60
AUDIT_TAIL_BYTES = 256 * 1024
AUDIT_LINE_LIMIT = 1500
AUDIT_ENTRY_LIMIT = 120
RUN_LIMIT = 40
APPROVAL_LIMIT = 50
MEMORY_RECENT_LIMIT = 12
MEMORY_SNIPPET_CHARS = 160
TEXT_LIMIT = 240

# A locked or busy SQLite file must answer (or fail) inside a dashboard poll, not block it.
MEMORY_TIMEOUT_SECONDS = 0.5

# When the app does not tell us, "since this process imported the snapshot code" is the closest
# honest answer to "since when do these totals apply".
_PROCESS_STARTED_AT = time.time()

_DOT_TITLE_SUFFIX = " · locus"

# Locus activity kinds -> the trace's own vocabulary. A locus's log is the same event stream the
# trace shows, written by a different producer; `loci.ActivityEntry` documents the full set
# (turn/goal/note/error/retry/created), and a kind this build does not know falls back to "ok"
# rather than being dropped -- an unlabelled row is still a real event.
_DOT_ACTIVITY_KINDS = {
    "turn": "turn",
    "goal": "ok",
    "note": "mem",
    "error": "error",
    "retry": "warn",
    "created": "ok",
}

TRACE_KINDS = ("tool", "ok", "warn", "error", "mem", "turn")
HEALTH_STATES = ("ok", "warn", "error", "off")


@dataclass(frozen=True)
class _Provider:
    """One provider this harness can talk to, and what it needs in order to do so.

    ``sdk_required`` is a measured fact, not a preference: Anthropic's agent constructs
    ``anthropic.Anthropic`` directly and Gemini's returns None without ``google-genai`` (see
    ``cli._build_agent``), while the OpenAI-compatible providers default to the built-in httpx
    client (``direct_http_client``) and only fall back to the ``openai`` package. Reporting a
    missing optional SDK as a failure would be a false alarm on the default install.
    """

    id: str
    label: str
    sdk: str
    sdk_required: bool


_PROVIDERS = (
    _Provider(PROVIDER_ANTHROPIC, "Anthropic", "anthropic", True),
    _Provider(PROVIDER_OPENAI_COMPAT, "OpenAI-compatible endpoint", "openai", False),
    _Provider(PROVIDER_OPENROUTER, "OpenRouter", "openai", False),
    _Provider(PROVIDER_DEEPSEEK, "DeepSeek", "openai", False),
    _Provider(PROVIDER_GEMINI, "Google Gemini", "google.genai", True),
    _Provider(PROVIDER_NETRA, "Netra (netraruntime)", "openai", False),
)

# SDK presence cannot change while the process runs (installing one requires a restart for the
# agent paths that import it anyway), and find_spec walks sys.path on every call.
_SDK_CACHE: dict[str, bool] = {}

# The honest writability probe creates and deletes a real file. Cached per path because the
# browser polls this endpoint every five seconds and re-probing a user's project directory that
# often is a side effect nobody asked for.
_WRITABLE_CACHE: dict[str, bool] = {}


class _Issues:
    """Everything this snapshot could not read, collected instead of raised.

    Each entry becomes a ``health.items`` row, so a degraded view explains itself rather than
    looking like an empty one -- "memory unavailable: database is locked" is information;
    a Memory tile showing zero is a lie by omission.
    """

    def __init__(self) -> None:
        self.lines: list[tuple[str, str]] = []

    def add(self, label: str, exc: BaseException | str) -> None:
        text = f"{type(exc).__name__}: {exc}" if isinstance(exc, BaseException) else str(exc)
        self.lines.append((label, text))


def _guard(label: str, issues: _Issues, default: Any, read: Callable[[], Any]) -> Any:
    """Run one source's reader. Its failure degrades that source only, and is recorded."""
    try:
        return read()
    except Exception as exc:  # noqa: BLE001 -- see _Issues: never fatal, always reported
        issues.add(label, exc)
        return default


# ---------------------------------------------------------------------------------------
# Small readers with no dependency of their own
# ---------------------------------------------------------------------------------------
def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list:
    return value if isinstance(value, list) else []


def _clip_text(value: Any, limit: int) -> str:
    """One line, bounded. Trace rows are rendered in a fixed-height list, so newlines in a tool
    result would wreck the layout and an unbounded string would wreck the payload."""
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _first_line(value: Any) -> str:
    text = str(value or "").strip()
    return text.splitlines()[0] if text else ""


def _args_summary(args: Any) -> str:
    if not args:
        return ""
    try:
        text = json.dumps(args, default=str, sort_keys=True)
    except (TypeError, ValueError):
        text = str(args)
    return _clip_text(text, 120)


def _sdk_present(module: str) -> bool:
    """Whether a module can be imported, without importing it.

    ``find_spec`` only consults the filesystem: importing ``openai``/``google.genai`` to answer
    this would cost tens of seconds (measured; see ``cli._run_doctor``, which is deliberately
    lazy for the same reason) on a code path that runs every few seconds.
    """
    if module in _SDK_CACHE:
        return _SDK_CACHE[module]
    try:
        found = importlib.util.find_spec(module) is not None
    except Exception:  # noqa: BLE001 -- a broken parent package is "not available", not fatal
        found = False
    _SDK_CACHE[module] = found
    return found


def _workspace_of(app: Any) -> Path:
    candidate = getattr(app, "workspace", None)
    if candidate:
        try:
            return Path(candidate)
        except TypeError:
            pass
    return Path.cwd()


def _directory_writable(directory: Path) -> bool:
    """True only if a file can really be created and removed there.

    A real write, not ``os.access``: on Windows ``os.access(W_OK)`` consults the ACL and reports
    True for a file that cannot be modified (measured; the same trap documented in
    ``dashboard.paths.is_writable`` and ``constants._is_writable``). The probe is duplicated
    rather than imported so this module pulls in no part of the dashboard package -- the
    dashboard imports it.
    """
    key = str(directory)
    cached = _WRITABLE_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / ".myriaxon_mission_probe"
        probe.write_text("", encoding="utf-8")
        probe.unlink(missing_ok=True)
        result = True
    except OSError:
        result = False
    _WRITABLE_CACHE[key] = result
    return result


def _read_stored_config() -> dict:
    """The saved provider config file, parsed directly rather than through ``config.load_config``.

    Two reasons, both about this running on a poll: ``load_config`` resolves the keychain
    sentinel by calling into the OS credential manager (a blocking call to Windows Credential
    Manager every few seconds, for information a boolean already answers), and it swallows the
    distinction between "no file" and "unreadable file" that the health row wants to report.
    A sentinel is already proof that a key was stored, which is all this needs.
    """
    path = constants.CONFIG_FILE
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _keychain_reference(value: Any) -> bool:
    """Whether a stored key is a reference into the OS keychain rather than the secret itself."""
    return str(value or "").startswith("<") and str(value or "").endswith(">")


def _credential(provider: _Provider, app_config: Any, stored: dict) -> tuple[bool, str]:
    """Whether a provider can authenticate, and a phrase naming what was found.

    Mirrors ``config.resolve_config``: the generic ``MYRIAXON_API_KEY`` wins, then the
    provider's own env fallback, then the saved config, then -- for Gemini -- Vertex AI, which
    legitimately has no key at all because it authenticates with Application Default
    Credentials. Getting that last case wrong would report a working corporate setup as broken.
    """
    env = os.environ
    if env.get(ENV_API_KEY):
        return True, f"API key from ${ENV_API_KEY}"
    fallback = PROVIDER_ENV_KEY_FALLBACK.get(provider.id)
    if fallback and env.get(fallback):
        return True, f"API key from ${fallback}"

    if provider.id == PROVIDER_GEMINI:
        project = ""
        if str(getattr(app_config, "provider", "") or "") == provider.id:
            project = str(getattr(app_config, "vertex_project", "") or "")
        if not project and str(stored.get("provider") or "") == provider.id:
            project = str(stored.get("vertex_project") or "")
        if not project and env.get("GOOGLE_GENAI_USE_VERTEXAI", "").strip().lower() == "true":
            project = env.get("GOOGLE_CLOUD_PROJECT") or env.get("GCLOUD_PROJECT") or ""
        if project:
            return True, f"Vertex AI project {project} (Application Default Credentials)"

    if str(getattr(app_config, "provider", "") or "") == provider.id and getattr(app_config, "api_key", ""):
        return True, "API key in the running configuration"
    if str(stored.get("provider") or "") == provider.id and stored.get("api_key"):
        if _keychain_reference(stored.get("api_key")):
            return True, "API key in the OS keychain (config.json holds a reference)"
        return True, f"API key in {constants.CONFIG_FILE.name}"
    return False, ""


def _provider_items(app_config: Any, stored: dict) -> list[dict]:
    """One health row per provider, the active one first (the ring diagram shows the head)."""
    active = str(getattr(app_config, "provider", "") or "")
    ordered = sorted(_PROVIDERS, key=lambda p: (p.id != active,))
    items: list[dict] = []
    for provider in ordered:
        configured, source = _credential(provider, app_config, stored)
        sdk_ok = _sdk_present(provider.sdk)
        is_active = provider.id == active
        notes: list[str] = []

        if not configured:
            notes.append("no API key configured")
        else:
            notes.append(source)
        if configured and provider.sdk_required and not sdk_ok:
            notes.append(f"the {provider.sdk} package is not installed")
        elif configured and not sdk_ok:
            notes.append(f"{provider.sdk} SDK not installed; the built-in HTTP client is used")
        elif sdk_ok:
            notes.append(f"{provider.sdk} SDK present")

        if configured and (sdk_ok or not provider.sdk_required):
            state = "ok"
        elif configured and is_active:
            # Configured but unusable *right now*: every turn will fail until the package is
            # installed, which is an error on the active provider and only a warning about one
            # nobody has selected.
            state = "error"
        elif configured:
            state = "warn"
        elif not is_active:
            state = "off"
        else:
            # A local OpenAI-compatible endpoint (Ollama, LM Studio, llama.cpp) needs no key, so
            # its absence is a warning about this provider rather than a broken install.
            base_url = str(getattr(app_config, "base_url", "") or "")
            if provider.id == PROVIDER_OPENAI_COMPAT and base_url:
                state = "warn"
                notes = ["no API key configured (fine for a local endpoint that needs none)"]
            else:
                state = "error"
                notes.insert(0, "the active provider cannot authenticate; run `myriaxon auth`")

        items.append(
            {
                "key": f"provider:{provider.id}",
                "label": provider.label,
                "kind": "provider",
                "state": state,
                "detail": " · ".join(notes) if notes else "ready",
            }
        )
    return items


# ---------------------------------------------------------------------------------------
# Sessions: usage, runs, and the trace they contribute
# ---------------------------------------------------------------------------------------
def _sessions(app: Any, issues: _Issues) -> tuple[list[Any], list[dict]]:
    """(live Session objects, their state dicts) newest first.

    The dicts drive ordering and counts; the objects are needed for the things only a live
    session knows -- whether a turn is running right now, and what it has spent. A missing
    object degrades to the dict alone rather than dropping the session from the list.
    """
    manager = getattr(app, "sessions", None)
    if manager is None or not hasattr(manager, "list"):
        issues.add("Sessions", "the application exposes no session manager")
        return [], []
    infos = _guard("Sessions", issues, [], lambda: [i for i in manager.list() if isinstance(i, dict)])
    sessions: list[Any] = []
    for info in infos[:RUN_LIMIT]:
        session = None
        if hasattr(manager, "get"):
            session = _guard("Sessions", issues, None, lambda i=info: manager.get(i.get("id", "")))
        sessions.append(session)
    return sessions, infos


def _session_usage(session: Any) -> tuple[int, int, int]:
    """(requests, input_tokens, output_tokens) from a session's agent, zeros when it has none.

    The source of record is ``usage.UsageTotals``, held on the agent -- the same object
    ``Session._usage_snapshot`` reads and ``/cost`` prints. A session whose agent has not been
    built yet has genuinely sent nothing, so it contributes zeros rather than a share of
    somebody else's total.
    """
    totals = getattr(getattr(session, "agent", None), "usage_totals", None)
    if totals is None:
        return 0, 0, 0
    return (
        _as_int(getattr(totals, "request_count", 0)),
        _as_int(getattr(totals, "input_tokens", 0)),
        _as_int(getattr(totals, "output_tokens", 0)),
    )


def _locus_of(title: str, locus_field: str = "") -> str:
    """The locus a session belongs to.

    ``SessionState.locus`` is the real signal and is preferred. The title convention is kept as a
    fallback for sessions created before that field existed, because their state dictionaries are
    still readable and there is no reason to mislabel an older run.

    Anything that matches neither is an ordinary chat session, which is the safe direction to be
    wrong in: a locus run would show as a chat, never the reverse.
    """
    if locus_field:
        return str(locus_field)
    if title.endswith(_DOT_TITLE_SUFFIX):
        return title[: -len(_DOT_TITLE_SUFFIX)].strip()
    return ""


def _session_facts(
    info: dict, session: Any, config: Any, loci_by_name: dict[str, Any], now: float
) -> dict:
    """One session's run row, its usage, its step count and the trace it contributes.

    ``elapsed_s`` is the span this run has covered: creation to *now* while a turn is in flight,
    creation to last activity once it stops. Adding a second "duration" field would only create
    two numbers that can disagree.
    """
    state = getattr(session, "state", None)
    session_id = str(info.get("id") or getattr(state, "id", "") or "")
    title = str(info.get("title") or getattr(state, "title", "") or "session")
    mode = str(
        info.get("mode") or getattr(state, "mode", "") or getattr(session, "mode", "") or DEFAULT_MODE
    )
    provider = str(
        info.get("provider") or getattr(state, "provider", "") or getattr(config, "provider", "") or ""
    )
    model = str(
        info.get("model") or getattr(state, "model", "") or getattr(config, "model", "") or ""
    )
    created = _as_float(info.get("created_at"), _as_float(getattr(state, "created_at", 0.0)))
    updated = _as_float(info.get("updated_at"), _as_float(getattr(state, "updated_at", created), created))
    turns = _as_int(info.get("turn_count"), _as_int(getattr(state, "turn_count", 0)))
    error = info.get("error", getattr(state, "error", None))
    busy = bool(getattr(session, "busy", False)) or bool(info.get("busy"))

    # Read the trajectory through one guarded call: a session whose recorder is mid-write must
    # cost its own step count, not the whole snapshot. Its failure is not a health row of its
    # own -- `runs[].steps` simply reads 0 for that session, which is the visible symptom.
    events: list[Any] = []
    if session is not None:
        try:
            events = list(session.trajectory())
        except Exception:  # noqa: BLE001
            events = []
    locus_name = _locus_of(title, str(info.get("locus") or getattr(state, "locus", "") or ""))

    # Progress is only defined where real progress exists: a locus's completed share of its own
    # objectives. A chat session has no denominator, so it reports None and the UI draws no bar
    # rather than a made-up percentage.
    progress: float | None = None
    locus = loci_by_name.get(locus_name) if locus_name else None
    if locus is not None:
        goals = list(getattr(locus, "goals", []) or [])
        if goals:
            done = sum(1 for g in goals if getattr(g, "status", "") == "done")
            progress = round(done / len(goals), 4)

    run = {
        "id": session_id,
        "title": title,
        "kind": "locus" if locus_name else "chat",
        "locus": locus_name,
        "provider": provider,
        "model": model,
        "mode": mode,
        "busy": busy,
        "turns": turns,
        "started_at": created,
        "updated_at": updated,
        "elapsed_s": int(max(0.0, (now if busy else updated) - created)),
        "progress": progress,
        "steps": len(events),
        "status": "running" if busy else ("error" if error else "idle"),
        "error": str(error) if error else None,
    }

    trace: list[dict] = []
    if turns:
        trace.append(
            _trace_row(
                updated, title, "turn", f"{turns} turn(s) completed ({model or 'model unknown'})", session_id
            )
        )
    # A TraceEvent carries no timestamp of its own (verified against bench.state.TraceEvent), so
    # the session's last-update time is the closest honest clock for these rows; they are
    # ordered correctly against each other because they arrive in execution order.
    for event in events[-SESSION_TRACE_EVENTS:]:
        tool = str(getattr(event, "tool", "") or "?")
        result = _first_line(getattr(event, "result", ""))
        text = f"{tool} {_args_summary(getattr(event, 'args', {}))}".strip()
        trace.append(
            _trace_row(
                updated,
                title,
                "error" if getattr(event, "is_error", False) else "tool",
                f"{text} -> {result}" if result else text,
                session_id,
            )
        )

    requests, input_tokens, output_tokens = _session_usage(session)
    return {
        "run": run,
        "requests": requests,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "model": model or "unknown",
        "trace": trace,
        "error": run["error"],
    }


def _trace_row(at: float, source: str, kind: str, text: str, run: str) -> dict:
    return {
        "at": float(at),
        "source": _clip_text(source, 80),
        "kind": kind if kind in TRACE_KINDS else "ok",
        "text": _clip_text(text, TEXT_LIMIT),
        "run": _clip_text(run, 80),
    }


# ---------------------------------------------------------------------------------------
# Loci: schedules, open work, and the decisions a person owes them
# ---------------------------------------------------------------------------------------
def _read_dots() -> list[Any]:
    """Every locus, re-read from disk.

    Reloaded on each call rather than served from the registry's startup copy: the terminal and
    the dashboard share one ``loci.json`` and are routinely open together, so a scheduled-work
    panel built from a stale read would be wrong the moment anyone used ``/loci``.
    """
    registry = get_registry()
    registry.reload()
    return list(registry.all())


def _schedules(loci: list[Any]) -> list[dict]:
    """One row per locus with open work.

    A locus with no open objectives is not scheduled for anything, and listing it with a countdown
    would suggest work that does not exist. ``seconds_until_due``/``is_due`` come from the Locus
    itself so the panel and the scheduler can never disagree about when it next wakes.
    """
    rows: list[dict] = []
    for locus in loci:
        goals = list(getattr(locus, "open_goals", list)() or [])
        if not goals:
            continue
        upcoming = getattr(locus, "next_goal", None)
        nxt = upcoming() if callable(upcoming) else None
        rows.append(
            {
                "locus": str(getattr(locus, "name", "") or getattr(locus, "id", "")),
                "role": str(getattr(locus, "role", "") or "generalist"),
                "enabled": bool(getattr(locus, "enabled", True)),
                "proactive": bool(getattr(locus, "proactive", False)),
                "interval_seconds": _as_int(getattr(locus, "interval_seconds", 0)),
                "seconds_until_due": _as_int(_call(locus, "seconds_until_due")),
                "due_now": bool(_call(locus, "is_due")),
                "open_goals": len(goals),
                "next_objective": str(getattr(nxt, "title", "") or ""),
            }
        )
    rows.sort(key=lambda r: (not r["due_now"], r["seconds_until_due"]))
    return rows


def _call(target: Any, name: str, default: Any = None) -> Any:
    """Call a method only if it exists. A Locus from an older ``loci.json`` may predate a field."""
    method = getattr(target, name, None)
    return method() if callable(method) else default


def _approvals(loci: list[Any], session_facts: list[dict], now: float) -> list[dict]:
    """What is waiting on a person, from the only two sources this codebase actually has.

    * A locus goal in ``blocked`` state. Two things put a goal there, and both want a human: the
      agent itself when it needs a decision only its owner can make (``loci.briefing`` tells it
      to), and the harness when a goal has spent its attempt ceiling without finishing
      (``loci._block_exhausted_goal``) -- which is a request to change the approach, not a
      request to wait longer.
    * A session sitting in an error state, which nobody can clear but the user.

    There is deliberately no third source: this codebase has no pending-ask queue (no
    ``pending`` register in ``policy.py`` or ``settings.py``; the tool registry's permission
    prompts are synchronous and terminal-only). If nothing is pending this returns ``[]`` and the
    UI says "Nothing waiting" -- an empty queue is the honest answer, and seeding it with demo
    rows is the one thing this view must never do.
    """
    rows: list[dict] = []
    for locus in loci:
        name = str(getattr(locus, "name", "") or getattr(locus, "id", ""))
        for goal in getattr(locus, "goals", []) or []:
            if str(getattr(goal, "status", "")) != GOAL_BLOCKED:
                continue
            notes = list(getattr(goal, "notes", []) or [])
            last_error = str(getattr(goal, "last_error", "") or "")
            why = str(notes[-1]) if notes else (
                last_error or f"{name} marked this objective blocked and needs a decision"
            )
            rows.append(
                {
                    "id": f"goal:{getattr(locus, 'id', name)}:{getattr(goal, 'id', '')}",
                    "what": str(getattr(goal, "title", "") or "blocked objective"),
                    "why": why,
                    "kind": "goal",
                    "locus": name,
                    "since": _as_float(getattr(goal, "updated_at", 0.0), now),
                }
            )
    for facts in session_facts:
        run = facts["run"]
        if not run["error"]:
            continue
        rows.append(
            {
                "id": f"session:{run['id']}",
                "what": f"Session failed: {run['title']}",
                "why": run["error"],
                "kind": "session",
                "locus": run["locus"],
                "since": run["updated_at"] or now,
            }
        )
    rows.sort(key=lambda r: r["since"], reverse=True)
    return rows[:APPROVAL_LIMIT]


# ---------------------------------------------------------------------------------------
# Audit log, memory store, skills
# ---------------------------------------------------------------------------------------
@dataclass
class _Audit:
    entries: list[dict] = field(default_factory=list)
    state: str = "off"
    detail: str = "no audit log yet; it is written as tools are called"


def _read_audit() -> _Audit:
    """The tail of the JSONL audit log, newest last (the caller reverses for display).

    Tail-only by design: this file is append-only for the life of the installation and can be
    large, and a status poll must not read a hundred megabytes to show the last twenty lines.
    The first line of a tail window is almost always a partial read, so it is dropped -- a
    truncated line that happens to parse would be worse than one that does not.
    """
    path = constants.AUDIT_LOG_FILE
    try:
        if not path.is_file():
            return _Audit()
        size = path.stat().st_size
    except OSError as exc:
        return _Audit(state="error", detail=f"audit log unreadable: {type(exc).__name__}: {exc}")
    if size <= 0:
        return _Audit()

    start = max(0, size - AUDIT_TAIL_BYTES)
    try:
        with path.open("rb") as handle:
            if start:
                handle.seek(start)
            chunk = handle.read(AUDIT_TAIL_BYTES + 4096)
    except OSError as exc:
        return _Audit(state="error", detail=f"audit log unreadable: {type(exc).__name__}: {exc}")

    lines = chunk.decode("utf-8", "replace").splitlines()
    if start:
        lines = lines[1:]
    lines = lines[-AUDIT_LINE_LIMIT:]

    entries: list[dict] = []
    skipped = 0
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            skipped += 1  # a half-written last line, or a hand edit
            continue
        if not isinstance(record, dict):
            continue
        entries.append(
            _trace_row(
                _as_float(record.get("ts")),
                "audit",
                "error" if record.get("is_error") else "tool",
                f"{record.get('tool') or '?'} {_args_summary(record.get('args'))}".strip(),
                # The audit schema records ts/cwd/tool/args/is_error and nothing that identifies
                # the session, so there is no run to attribute these rows to. Empty is the truth.
                "",
            )
        )
    entries = entries[-AUDIT_ENTRY_LIMIT:]
    detail = f"{len(entries)} recent entr{'y' if len(entries) == 1 else 'ies'} read"
    if skipped:
        detail += f"; {skipped} unreadable line(s) skipped"
    return _Audit(entries=entries, state="ok", detail=detail)


@dataclass
class _Memory:
    indexed: int = 0
    recent: list[dict] = field(default_factory=list)
    state: str = "off"
    detail: str = "no memory database yet; it fills as sessions end"


def _read_memory() -> _Memory:
    """The cross-session memory store: how many projects are indexed, and the newest few.

    Opened read-only through SQLite's URI form. A plain ``sqlite3.connect`` to a path that does
    not exist *creates* a database, which would mean a status endpoint silently writing state --
    and a status endpoint that mutates what it reports is how a "why is this file here?" bug is
    born. One row per project (``record_session`` replaces a project's row), so the count is
    "projects remembered", not "sessions ever held".
    """
    path = constants.MEMORY_DB_FILE
    try:
        if not path.is_file():
            return _Memory()
    except OSError as exc:
        return _Memory(state="error", detail=f"memory store unreadable: {type(exc).__name__}: {exc}")

    uri = "file:" + urllib.parse.quote(str(path).replace("\\", "/")) + "?mode=ro"
    con = None
    try:
        con = sqlite3.connect(uri, uri=True, timeout=MEMORY_TIMEOUT_SECONDS)
        exists = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sessions_fts'"
        ).fetchone()
        if not exists:
            return _Memory(state="off", detail="memory database exists but holds no session index yet")
        indexed = _as_int((con.execute("SELECT COUNT(*) FROM sessions_fts").fetchone() or [0])[0])
        rows = con.execute(
            "SELECT project_path, ts, substr(transcript, 1, 400) FROM sessions_fts "
            "ORDER BY ts DESC LIMIT ?",
            (MEMORY_RECENT_LIMIT,),
        ).fetchall()
    except sqlite3.Error as exc:
        return _Memory(state="error", detail=f"memory store unavailable: {type(exc).__name__}: {exc}")
    finally:
        if con is not None:
            try:
                con.close()
            except sqlite3.Error:
                pass

    recent = [
        {
            "project": str(row[0] or ""),
            "when": _as_float(row[1]),
            "snippet": _clip_text(row[2], MEMORY_SNIPPET_CHARS),
        }
        for row in rows
    ]
    return _Memory(
        indexed=indexed,
        recent=recent,
        state="ok",
        detail=f"{indexed} project transcript(s) indexed for search",
    )


def _read_skills(workspace: Path) -> list[dict]:
    """Discovered skills with the scope they came from, highest priority last.

    ``discover_skills`` merges builtin -> user -> project in that order (later wins), so the
    scope of a discovered skill is decided by where its file actually lives; deriving it from
    the path rather than guessing keeps a user skill that shadows a builtin labelled correctly.
    """
    skills = discover_skills(workspace)
    rows: list[dict] = []
    for name, skill in sorted(skills.items()):
        source = getattr(skill, "source", None)
        scope = "project"
        try:
            resolved = Path(source).resolve() if source else None
            if resolved is not None:
                if resolved.is_relative_to(Path(BUILTIN_SKILLS_DIR).resolve()):
                    scope = "builtin"
                elif resolved.is_relative_to(Path(constants.USER_SKILLS_DIR).resolve()):
                    scope = "user"
        except (OSError, TypeError, ValueError):
            scope = "unknown"
        rows.append(
            {
                "name": str(name),
                "scope": scope,
                "description": _clip_text(getattr(skill, "description", ""), TEXT_LIMIT),
            }
        )
    return rows


# ---------------------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------------------
def _health(
    app: Any,
    workspace: Path,
    stored: dict,
    mcp_config: Any,
    messaging: Any,
    memory: _Memory,
    audit: _Audit,
    plugins: Any,
    issues: _Issues,
) -> dict:
    """Readiness rows for everything that can be connected, checked offline.

    ``overall`` is the worst row, with one deliberate exception: ``off`` is "not configured",
    not "broken", so a provider nobody has set up does not turn the whole board red. A board
    where *nothing* is configured is a warning rather than an error -- there is nothing wrong
    with the machine, it has just not been set up yet.
    """
    config = getattr(app, "config", None)
    items = _provider_items(config, stored)

    stdio = list((mcp_config or {}).get("stdio_servers") or []) if isinstance(mcp_config, dict) else []
    urls = list((mcp_config or {}).get("url_servers") or []) if isinstance(mcp_config, dict) else []
    server_count = len(stdio) + len(urls)
    if mcp_config is None:
        mcp_item = {
            "key": "mcp", "label": "MCP servers", "kind": "integration", "state": "warn",
            "detail": "MCP configuration could not be read",
        }
    elif server_count:
        sdk_ok = _sdk_present("mcp")
        names = ", ".join(str(s.get("name") or "?") for s in (stdio + urls)[:4])
        detail = f"{server_count} configured: {names}" + ("" if sdk_ok else "; the mcp package is not installed")
        mcp_item = {
            "key": "mcp", "label": "MCP servers", "kind": "integration",
            "state": "ok" if sdk_ok else "warn", "detail": detail,
        }
    else:
        mcp_item = {
            "key": "mcp", "label": "MCP servers", "kind": "integration", "state": "off",
            "detail": "none configured",
        }
    items.append(mcp_item)

    if messaging is None:
        items.append(
            {"key": "messaging", "label": "Messaging", "kind": "integration", "state": "warn",
             "detail": "messaging configuration could not be read"}
        )
    else:
        usable = bool(_call(messaging, "is_usable", False))
        enabled = bool(getattr(messaging, "enabled", False))
        allowed = len(list(getattr(messaging, "allow", []) or []))
        if usable:
            mess_state, mess_detail = "ok", f"{getattr(messaging, 'provider', 'generic')}; {allowed} allowed sender(s)"
        elif enabled:
            mess_state = "warn"
            mess_detail = "enabled but not usable (no allowed senders, or missing credentials)"
        else:
            mess_state, mess_detail = "off", "disabled"
        items.append(
            {"key": "messaging", "label": "Messaging", "kind": "integration",
             "state": mess_state, "detail": mess_detail}
        )

    writable = _directory_writable(workspace)
    items.append(
        {
            "key": "workspace", "label": "Workspace", "kind": "local",
            "state": "ok" if writable else "error",
            "detail": str(workspace) if writable else f"{workspace} is not writable; write tools will fail",
        }
    )

    git = shutil.which("git")
    items.append(
        {
            "key": "git", "label": "git", "kind": "local",
            "state": "ok" if git else "off",
            "detail": str(git) if git else "not found on PATH; rewind and shadow history are unavailable",
        }
    )

    items.append(
        {"key": "memory", "label": "Memory store", "kind": "local",
         "state": memory.state, "detail": memory.detail}
    )
    items.append(
        {"key": "audit", "label": "Audit log", "kind": "local",
         "state": audit.state, "detail": audit.detail}
    )

    # Only a *failed* plugin earns a row: a plugin that loaded fine is already counted in
    # `counts.plugins`, and a permanent green row for "plugins work" would be noise on a board
    # that is scanned for problems.
    failed_plugins = list((plugins or {}).get("failed") or [])
    if failed_plugins:
        items.append(
            {
                "key": "plugins", "label": "Plugins", "kind": "local", "state": "warn",
                "detail": "; ".join(f"{p['name']}: {p['error']}" for p in failed_plugins[:3]),
            }
        )

    # A source that blew up becomes its own row. Labels already carried by a row above are
    # skipped so one failure is not reported twice.
    known = {item["label"].lower() for item in items}
    for label, text in issues.lines:
        if label.lower() in known:
            continue
        items.append(
            {"key": f"source:{label.lower().replace(' ', '-')}", "label": f"{label} unavailable",
             "kind": "local", "state": "warn", "detail": text}
        )

    worst = "ok"
    for item in items:
        if item["state"] == "error":
            worst = "error"
            break
        if item["state"] == "warn" and worst != "error":
            worst = "warn"
    if worst == "ok" and all(item["state"] == "off" for item in items):
        worst = "warn"

    providers = [i for i in items if i["kind"] == "provider"]
    ready = sum(1 for i in providers if i["state"] == "ok")
    summary = f"{ready} of {len(providers)} providers ready"
    problems = [i for i in items if i["state"] in ("warn", "error") and i["kind"] != "provider"]
    if problems:
        summary += ", " + ", ".join(f"{i['label']} {i['state']}" for i in problems[:3])
    else:
        integrations = [i for i in items if i["kind"] == "integration" and i["state"] == "ok"]
        summary += (
            f", {len(integrations)} integration(s) configured" if integrations else ", no integrations configured"
        )
    return {"overall": worst, "summary": summary, "items": items}


# ---------------------------------------------------------------------------------------
# The snapshot
# ---------------------------------------------------------------------------------------
def snapshot(app: Any) -> dict[str, Any]:
    """Everything the Mission control view renders, gathered from real state.

    ``app`` is the dashboard application (``dashboard.server.DashboardApp``): it supplies the
    workspace, the resolved provider config, the session manager and the plugin registry. Every
    attribute is read with ``getattr`` and every source is individually guarded, so a partially
    constructed app -- or one whose session manager is on fire -- still produces a complete,
    well-formed payload with the failures named in ``health.items``.

    Never raises, never makes a network call, and writes nothing but the workspace writability
    probe (see the module docstring).

    The outer ``except`` is the reason this function's contract is "never raises" rather than
    "should not raise": it is the one guarantee the HTTP route depends on, and a case nobody
    anticipated is exactly the case where a 500 would be hardest to explain. A catastrophic
    failure still returns the full key set, empty, with the exception named in ``health``.
    """
    try:
        return _build_snapshot(app)
    except Exception as exc:  # noqa: BLE001 -- see the docstring: the route must never 500
        return _empty_snapshot(app, exc)


def _empty_snapshot(app: Any, exc: BaseException) -> dict[str, Any]:
    """The contract's shape with nothing in it, plus the reason. Must itself never raise."""
    now = time.time()
    workspace = _workspace_of(app)
    detail = f"the snapshot could not be assembled: {type(exc).__name__}: {exc}"
    return {
        "generated_at": now,
        "workspace": str(workspace),
        "health": {
            "overall": "error",
            "summary": "mission control could not read this machine's state",
            "items": [
                {"key": "snapshot", "label": "Snapshot", "kind": "local", "state": "error", "detail": detail}
            ],
        },
        "usage": {
            "requests": 0, "input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
            "sessions": 0, "turns": 0, "tools_called": 0, "errors": 0,
            "started_at": _as_float(getattr(app, "started_at", 0.0), 0.0) or _PROCESS_STARTED_AT,
            "by_model": [],
        },
        "runs": [],
        "approvals": [],
        "trace": [],
        "schedules": [],
        "memory": {"sessions_indexed": 0, "recent": []},
        "skills": [],
        "routing": [],
        "counts": {
            "loci": 0, "open_goals": 0, "sessions": 0, "skills": 0,
            "mcp_servers": 0, "plugins": 0, "rules": 0,
        },
    }


def _build_snapshot(app: Any) -> dict[str, Any]:
    """Assemble the payload. Every source below is individually guarded; see :class:`_Issues`."""
    generated_at = time.time()
    issues = _Issues()
    now = generated_at
    workspace = _workspace_of(app)
    config = getattr(app, "config", None)
    stored = _read_stored_config()
    started_at = _as_float(getattr(app, "started_at", 0.0), 0.0) or _PROCESS_STARTED_AT

    sessions, infos = _sessions(app, issues)
    loci = _guard("Loci", issues, [], _read_dots)
    loci_by_name = {str(getattr(d, "name", "")): d for d in loci}

    session_facts: list[dict] = []
    for info, session in zip(infos, sessions):
        facts = _guard(
            "Session detail",
            issues,
            None,
            lambda i=info, s=session: _session_facts(i, s, config, loci_by_name, now),
        )
        if facts is None:
            # The session still exists and still counts; only its detail was unreadable.
            facts = _session_facts(info, None, config, loci_by_name, now)
        session_facts.append(facts)

    usage_totals = {"requests": 0, "input_tokens": 0, "output_tokens": 0}
    per_model: dict[str, dict] = {}
    trace_rows: list[dict] = []
    for facts in session_facts:
        for key in usage_totals:
            usage_totals[key] += facts[key]
        model, requests = facts["model"], facts["requests"]
        row = per_model.setdefault(model, {"model": model, "requests": 0, "input_tokens": 0, "output_tokens": 0})
        row["requests"] += requests
        row["input_tokens"] += facts["input_tokens"]
        row["output_tokens"] += facts["output_tokens"]
        trace_rows.extend(facts["trace"])

    mcp_config = _guard("MCP servers", issues, None, load_mcp_config)
    messaging = None
    try:
        messaging = load_messaging_config(constants.MESSAGING_FILE)
    except Exception as exc:  # noqa: BLE001
        issues.add("Messaging", exc)
    memory = _guard(
        "Memory store", issues,
        _Memory(state="error", detail="memory store unavailable"),
        _read_memory,
    )
    audit = _guard(
        "Audit log", issues,
        _Audit(state="error", detail="audit log unavailable"),
        _read_audit,
    )
    skills = _guard("Skills", issues, [], lambda: _read_skills(workspace))
    rules = _guard("Permission rules", issues, 0, lambda: len(load_settings(workspace).permissions))
    plugins = _guard("Plugins", issues, None, lambda: _plugin_state(app))

    for locus in loci:
        name = str(getattr(locus, "name", "") or getattr(locus, "id", ""))
        for entry in (getattr(locus, "activity", []) or [])[-LOCUS_ACTIVITY_LIMIT:]:
            trace_rows.append(
                _trace_row(
                    _as_float(getattr(entry, "at", 0.0)),
                    name,
                    _DOT_ACTIVITY_KINDS.get(str(getattr(entry, "kind", "")), "ok"),
                    getattr(entry, "summary", ""),
                    name,
                )
            )
    trace_rows.extend(audit.entries)
    trace_rows.sort(key=lambda row: row["at"], reverse=True)

    by_model = [
        {
            "model": row["model"],
            "requests": row["requests"],
            "input_tokens": row["input_tokens"],
            "output_tokens": row["output_tokens"],
        }
        for row in sorted(per_model.values(), key=lambda r: (-r["requests"], r["model"]))
        if row["requests"] or row["input_tokens"] or row["output_tokens"]
    ]
    total_requests = usage_totals["requests"]
    routing = [
        {
            "model": row["model"],
            "requests": row["requests"],
            "share": round(row["requests"] / total_requests, 4) if total_requests else 0.0,
        }
        for row in sorted(per_model.values(), key=lambda r: (-r["requests"], r["model"]))
        if row["requests"]
    ]

    health = _health(app, workspace, stored, mcp_config, messaging, memory, audit, plugins, issues)

    mcp_servers = 0
    if isinstance(mcp_config, dict):
        mcp_servers = len(list(mcp_config.get("stdio_servers") or [])) + len(
            list(mcp_config.get("url_servers") or [])
        )

    return {
        "generated_at": generated_at,
        "workspace": str(workspace),
        "health": health,
        "usage": {
            "requests": usage_totals["requests"],
            "input_tokens": usage_totals["input_tokens"],
            "output_tokens": usage_totals["output_tokens"],
            "total_tokens": usage_totals["input_tokens"] + usage_totals["output_tokens"],
            "sessions": len(infos),
            "turns": sum(facts["run"]["turns"] for facts in session_facts),
            "tools_called": sum(facts["run"]["steps"] for facts in session_facts),
            "errors": sum(1 for facts in session_facts if facts["error"]),
            "started_at": started_at,
            "by_model": by_model,
        },
        "runs": [facts["run"] for facts in session_facts][:RUN_LIMIT],
        "approvals": _approvals(loci, session_facts, now),
        "trace": trace_rows[:TRACE_LIMIT],
        "schedules": _schedules(loci),
        "memory": {"sessions_indexed": memory.indexed, "recent": memory.recent},
        "skills": skills,
        "routing": routing,
        "counts": {
            "loci": len(loci),
            "open_goals": sum(len(list(getattr(d, "open_goals", list)() or [])) for d in loci),
            "sessions": len(infos),
            "skills": len(skills),
            "mcp_servers": mcp_servers,
            "plugins": (plugins or {}).get("count", 0),
            "rules": rules,
        },
    }


def _plugin_state(app: Any) -> dict:
    """Loaded plugin count plus any load failures, from the app's registry or the process one.

    A plugin that raised on import is recorded by ``plugins.PluginRegistry`` rather than
    crashing anything, so this is the only place that failure would ever be visible.
    """
    registry = getattr(app, "plugins", None)
    if registry is None:
        from .plugins import get_registry as get_plugin_registry

        registry = get_plugin_registry()
    describe = getattr(registry, "describe", None)
    loaded = list(describe() or []) if callable(describe) else []
    failed = [
        {"name": str(getattr(p, "name", "?")), "error": str(getattr(p, "error", ""))}
        for p in loaded
        if getattr(p, "error", "")
    ]
    return {"count": len(loaded), "failed": failed}


# ---------------------------------------------------------------------------------------
# Plain-text rendering (the same data, readable without a browser)
# ---------------------------------------------------------------------------------------
def render_lines(snapshot: dict[str, Any]) -> list[str]:
    """A ~20-line plain-text summary of a snapshot, for the CLI and for logs.

    Deliberately defensive: it is given whatever ``snapshot()`` produced, and a field a future
    version adds or drops must not turn a status readout into a traceback.
    """
    data = _as_dict(snapshot)
    health = _as_dict(data.get("health"))
    usage = _as_dict(data.get("usage"))
    counts = _as_dict(data.get("counts"))
    runs = _as_list(data.get("runs"))
    approvals = _as_list(data.get("approvals"))
    schedules = _as_list(data.get("schedules"))
    trace = _as_list(data.get("trace"))
    memory = _as_dict(data.get("memory"))
    routing = _as_list(data.get("routing"))

    when = time.strftime(
        "%Y-%m-%d %H:%M:%S", time.localtime(_as_float(data.get("generated_at"), time.time()))
    )
    lines = [f"Mission control - {when}"]
    lines.append(f"Workspace   {data.get('workspace') or '(unknown)'}")
    lines.append(f"Health      {health.get('overall', '?')} - {health.get('summary', '')}".rstrip(" -"))
    lines.append(
        "Usage       "
        f"{_as_int(usage.get('total_tokens')):,} tokens "
        f"({_as_int(usage.get('input_tokens')):,} in / {_as_int(usage.get('output_tokens')):,} out), "
        f"{_as_int(usage.get('requests'))} request(s), {_as_int(usage.get('turns'))} turn(s), "
        f"{_as_int(usage.get('tools_called'))} tool call(s), {_as_int(usage.get('errors'))} error(s)"
    )

    running = sum(1 for run in runs if _as_dict(run).get("busy"))
    lines.append(f"Runs        {len(runs)} session(s), {running} running")
    for entry in runs[:3]:
        run = _as_dict(entry)
        lines.append(
            f"  {'*' if run.get('busy') else '-'} {_clip_text(run.get('title'), 40)} "
            f"[{run.get('kind')}] {run.get('model') or '?'} "
            f"{_as_int(run.get('turns'))} turn(s) {_as_int(run.get('elapsed_s'))}s {run.get('status')}"
        )

    lines.append(f"Approvals   {len(approvals)} waiting")
    for entry in approvals[:3]:
        item = _as_dict(entry)
        lines.append(
            f"  ! {_clip_text(item.get('what'), 60)} "
            f"({item.get('kind')}: {_clip_text(item.get('why'), 60)})"
        )

    due = sum(1 for row in schedules if _as_dict(row).get("due_now"))
    lines.append(f"Schedules   {len(schedules)} locus(s) with open work, {due} due now")
    for entry in schedules[:3]:
        row = _as_dict(entry)
        # "in 0s" would be the literal truth for a locus whose every objective is blocked (it has
        # never run, so its own countdown reads zero) and a misleading one: it is waiting on a
        # person, not about to start. `next_objective` empty with open goals is that case.
        if row.get("due_now"):
            when_next = "due now"
        elif not row.get("proactive"):
            when_next = "on demand"
        elif not row.get("next_objective"):
            when_next = "waiting on a decision"
        else:
            when_next = f"in {_as_int(row.get('seconds_until_due'))}s"
        lines.append(
            f"  - {_clip_text(row.get('locus'), 30)} [{row.get('role')}] {when_next}, "
            f"{_as_int(row.get('open_goals'))} open"
        )

    lines.append(f"Trace       {len(trace)} entr{'y' if len(trace) == 1 else 'ies'} (newest first)")
    for entry in trace[:4]:
        row = _as_dict(entry)
        stamp = time.strftime("%H:%M:%S", time.localtime(_as_float(row.get("at"))))
        lines.append(
            f"  {stamp} {str(row.get('kind', '?')):<5} "
            f"{_clip_text(row.get('source'), 18)} {_clip_text(row.get('text'), 70)}"
        )

    lines.append(f"Memory      {_as_int(memory.get('sessions_indexed'))} transcript(s) indexed")
    lines.append(f"Skills      {len(_as_list(data.get('skills')))} available")
    if routing:
        shares = []
        for entry in routing[:4]:
            row = _as_dict(entry)
            shares.append(
                f"{row.get('model')} {round(_as_float(row.get('share')) * 100)}% "
                f"({_as_int(row.get('requests'))})"
            )
        lines.append("Routing     " + ", ".join(shares))
    else:
        lines.append("Routing     no requests yet this process")
    lines.append(
        "Counts      "
        f"loci {_as_int(counts.get('loci'))}, open goals {_as_int(counts.get('open_goals'))}, "
        f"sessions {_as_int(counts.get('sessions'))}, skills {_as_int(counts.get('skills'))}, "
        f"mcp {_as_int(counts.get('mcp_servers'))}, plugins {_as_int(counts.get('plugins'))}, "
        f"rules {_as_int(counts.get('rules'))}"
    )
    return lines


__all__ = ["snapshot", "render_lines", "TRACE_LIMIT"]
