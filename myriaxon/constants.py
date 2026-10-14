import os
from pathlib import Path

APP_NAME = "Myriaxon"
# The full product name, for places that need a name rather than a wordmark (the system prompt's
# self-description, package metadata, window titles). The previous value carried the former
# employer's name in parentheses, which was corporate attribution rather than branding and is
# not something a general-purpose harness should assert about itself.
APP_NAME_LONG = "Myriaxon"
VERSION = "0.0.1"
AUTHOR = "Thariq Arian Khalfani"

DEFAULT_BASE_URL = "https://api.anthropic.com"
DEFAULT_GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/"
DEFAULT_GEMINI_MODEL = "gemini-3.8-flash"
DEFAULT_MODEL = "claude-opus-5"
DEFAULT_MAX_TOKENS = 64000
# Lower ceiling for OpenAI-compatible providers: unlike Anthropic, many of them (OpenRouter
# included) check max_tokens against your available credit balance upfront and reject the whole
# request if the worst case wouldn't be affordable -- a high default blocks small accounts even
# for short replies.
#
# 4096 is therefore a deliberate floor, not an accident -- but it is fatal on a reasoning model.
# Measured in a Terminal-Bench task container: `deepseek-v4.1-flash` spent the whole 4096 on
# reasoning, produced no content and no tool call, and the turn ended with only
# "Response truncated at the model's max output length." The agent then had nothing to do and the
# task scored zero, with no error anywhere. A budget that a model can exhaust before it starts
# answering is not a conservative default, it is a broken one.
#
# So the floor applies to ordinary models and reasoning models get headroom. Detecting "is this a
# reasoning model" from the name is unreliable, so `REASONING_TOKEN_HEADROOM` is applied by the
# adaptive retry in the OpenAI agent instead: the first truncation raises the ceiling for that turn.
# This constant stays as the starting point and remains small enough not to trip a credit check.
DEFAULT_MAX_TOKENS_OPENAI_COMPAT = 4096
# What a truncated turn retries with. Chosen to comfortably fit a long reasoning trace plus the
# answer, and still small enough that a prepaid account is unlikely to be rejected for it.
REASONING_TOKEN_HEADROOM = 32_768
DEFAULT_BASH_TIMEOUT = 120

# --- background jobs ---------------------------------------------------------------------------
#
# The service layer over `run_bash(run_in_background=True)`. Opencode's `background-job.ts` offers
# wait/promote/cancel; these are the bounds that make the same operations safe here.

# How long a `wait` or `promote` blocks before reporting that the job is still running.
#
# Bounded because `run_budget.check()` runs at the STEP boundary: a tool call that never returns means a budget that
# is never consulted, so an unbounded wait would quietly defeat the ceiling above it.
DEFAULT_BACKGROUND_WAIT_SECONDS = 30.0

# How much of a job's output is carried into a transcript. A build or a server log would otherwise put megabytes
# into the conversation, and the tail is what a caller actually reads.
BACKGROUND_JOB_OUTPUT_TAIL_CHARS = 8_000

# How many jobs the registry keeps before the oldest FINISHED ones are dropped. Running jobs are never dropped:
# losing the handle would leave a process nothing can kill or wait for.
MAX_BACKGROUND_JOBS = 20

# Retained for a caller that wants a short tail, e.g. a status line.
BACKGROUND_JOB_KEEP = 5

# OpenRouter's catalog is intentionally open/freely-typed (hundreds of models, no restriction
# like DEEPSEEK_MODELS) -- this is a curated quick-pick shortlist shown alongside (never instead
# of) the full live /models catalog, not a restriction. Each id confirmed live against the real
# OpenRouter /models endpoint (2026-08-31) before being added here.
OPENROUTER_FAVORITE_MODELS = (
    "openai/gpt-oss-120b",
    "moonshotai/kimi-k3",
    "minimax/minimax-m3:free",
    "qwen/qwen3.8-27b",
    "z-ai/glm-5.3-flash",
    "deepseek/deepseek-v4-flash",
)

ENV_API_KEY = "MYRIAXON_API_KEY"
ENV_BASE_URL = "MYRIAXON_BASE_URL"
ENV_MODEL = "MYRIAXON_MODEL"
ENV_PROVIDER = "MYRIAXON_PROVIDER"
ENV_FALLBACK_MODEL = "MYRIAXON_FALLBACK_MODEL"

PROVIDER_ANTHROPIC = "anthropic"
PROVIDER_OPENAI_COMPAT = "openai_compat"
PROVIDER_GEMINI = "gemini"
PROVIDER_DEEPSEEK = "deepseek"
# OpenRouter used to be handled as just another PROVIDER_OPENAI_COMPAT endpoint (same protocol),
# but that made a fixed endpoint/API-key-env-fallback/favorites-list impossible to wire cleanly
# without stringly detecting "is this actually OpenRouter" from base_url every time (as
# _print_model_list briefly did). Split out as its own provider id, same precedent as
# PROVIDER_DEEPSEEK just above -- both still reuse OpenAIAgent (see openai_agent.py's `provider`
# param), they just get their own fixed endpoint/model defaults/env fallback.
PROVIDER_OPENROUTER = "openrouter"

# Netra (netraruntime) -- an OpenAI-compatible gateway. Given its own provider id rather than being
# lumped into the generic "openai_compat" bucket for the same reason DeepSeek and OpenRouter have
# one: a fixed endpoint and its own API-key env var mean the setup wizard does not have to ask the
# user to type an endpoint, and `MYRIAXON_PROVIDER=netra` alone is enough to reproduce a run.
#
# Verified live before wiring this in (2026-09-29): streaming works, reasoning_details arrive as
# structured entries, function calling works, and a tool-result round trip completes. Latency was
# markedly better than Vertex: ~2.1s median completion, 1.4s time-to-first-token.
PROVIDER_NETRA = "netra"
DEFAULT_NETRA_BASE_URL = "https://api.netraruntime.com/v1"
NETRA_MODELS = ("deepseek/deepseek-v4.1-flash", "deepseek/deepseek-v4-flash-0731")
DEFAULT_NETRA_MODEL = NETRA_MODELS[0]
NETRA_API_KEY_ENV_FALLBACK = "NETRA_API_KEY"
DEFAULT_PROVIDER = PROVIDER_ANTHROPIC

# Ollama runs on the user's own machine: no API key, no network, no bill. It speaks the
# OpenAI-compatible protocol at `<host>/v1`, so it reuses the existing openai_compat agent rather than
# needing a fourth agent implementation -- the only thing that makes it a distinct provider is that
# it must be *found*, and that its models' real capabilities have to be read rather than assumed.
#
# The key is a placeholder because the protocol requires the field to be non-empty and Ollama ignores
# it; an empty string makes the HTTP client reject the request before it is sent.
PROVIDER_OLLAMA = "ollama"
DEFAULT_OLLAMA_BASE_URL = "http://127.0.0.1:11434/v1"
OLLAMA_PLACEHOLDER_KEY = "ollama"

GEMINI_API_KEY_ENV_FALLBACK = "GEMINI_API_KEY"

# Vertex AI is not a separate provider -- it's the same Gemini provider/agent, just with the
# google-genai Client constructed against a GCP project/location (optionally via Application
# Default Credentials) instead of a plain API key + generativelanguage.googleapis.com endpoint.
# Env var names match gemini-cli's own, so a user with gcloud/Vertex already configured for that
# tool needs no extra setup here.
ENV_GOOGLE_GENAI_USE_VERTEXAI = "GOOGLE_GENAI_USE_VERTEXAI"
ENV_GOOGLE_CLOUD_PROJECT = "GOOGLE_CLOUD_PROJECT"
ENV_GOOGLE_CLOUD_LOCATION = "GOOGLE_CLOUD_LOCATION"
DEFAULT_VERTEX_LOCATION = "us-central1"

# Region that actually serves the newest model line. Measured on this project:
#   gemini-3.8-flash  -> 404 on us-central1, works on global
#   gemini-2.5-flash  -> works on both
# A 404 for a model that demonstrably exists is the worst kind of failure to debug, so the
# multi-region `global` endpoint is the default when Vertex is selected without an explicit location;
# a specific region can still be pinned with --vertex-location / GOOGLE_CLOUD_LOCATION.
DEFAULT_VERTEX_GLOBAL_LOCATION = "global"

# DeepSeek speaks the OpenAI-compatible chat completions protocol (same as OpenRouter), so it
# reuses OpenAIAgent -- it just gets its own provider id/fixed endpoint/restricted model choices,
# rather than being lumped into the generic "openai_compat" bucket where the endpoint and model
# are freely typed.
DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_MODELS = ("deepseek-v4-flash", "deepseek-v4-pro")
DEFAULT_DEEPSEEK_MODEL = DEEPSEEK_MODELS[0]
DEEPSEEK_API_KEY_ENV_FALLBACK = "DEEPSEEK_API_KEY"

# OpenRouter: same reasoning as DeepSeek just above -- its own provider id with a fixed endpoint
# (confirmed: the openai SDK appends /chat/completions itself, so base_url is the API root, not
# the full completions URL) and its own .env API-key fallback, instead of being just another
# freely-typed "openai_compat" endpoint.
DEFAULT_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
OPENROUTER_API_KEY_ENV_FALLBACK = "OPENROUTER_API_KEY"
# Confirmed live (2026-08-27) that DeepSeek's API never publishes context-window size anywhere
# (no context_length field on GET /v1/models, and GET /v1/models/{id} 404s) -- this is a manually
# maintained fallback from DeepSeek's own published docs, not something fetch_model_limits can
# verify against the API itself. Update this if DeepSeek's real published limit ever changes.
DEEPSEEK_MAX_INPUT_TOKENS = 1_000_000

# Where Myriaxon keeps machine-level state (config.json, prompt history, mcp_servers.json, skills,
# shadow history). Overridable with MYRIAXON_CONFIG_DIR because the default is not always writable:
# measured on a managed Windows machine where ~/.myriaxon denied BOTH creating a new file and
# modifying the existing config.json (ACLs granting delete but not modify), which made it impossible
# to save a provider selection at all. Any location the user controls works.
#
# The override is explicit rather than silent: a tool that quietly relocates a user's credentials to
# a directory they did not choose is worse than one that explains where it is looking.
# The previous product name's state directory. Read as a fallback and migrated on first run, so an
# existing installation keeps its provider config, API keys, prompt history, skills, MCP servers and
# memory database instead of silently starting over as a blank setup wizard. Renaming a state
# directory without this would be a data-loss bug wearing a rebrand's clothes.
LEGACY_CONFIG_DIR_NAME = ".iohcode"
LEGACY_INSTRUCTIONS_FILENAME = "IOH.md"
LEGACY_IGNORE_FILENAME = ".iohignore"


def migrate_legacy_state(legacy: Path, current: Path) -> bool:
    """Adopt a pre-rebrand state directory so an existing install keeps its credentials.

    Returns True when state was moved.

    Two shapes have to be handled, because the obvious implementation only handles one and
    silently strands the user's API key in the other:

    1. **The new directory does not exist.** Rename the old one into place -- atomic on the same
       filesystem, and it leaves nothing behind to reconcile.
    2. **The new directory exists but holds no state.** This is the case that actually bites:
       anything that touched the config directory first (a log write, a probe, an earlier launch
       of a half-migrated build) creates it, and a "does the destination exist?" guard then reads
       that empty directory as "the user has already moved on" and abandons the old state. The
       result is a live ``config.json`` with a real API key left behind while every launch shows
       the first-run setup wizard -- which looks exactly like a broken install, and is how this
       was found. Files present in both are left alone; only files missing from the destination
       are moved.

    A *move* rather than a copy in both cases: the directory holds API keys, an audit log and a
    SQLite database, and leaving a second live copy behind means two divergent states plus a
    credential file the user believes has been superseded.

    Never fatal. A failed migration is reported as False, and the worst acceptable outcome is
    that the user re-runs setup -- not that the tool refuses to start.
    """
    try:
        if not legacy.is_dir():
            return False

        if not current.exists():
            current.parent.mkdir(parents=True, exist_ok=True)
            legacy.rename(current)
            return True

        if not current.is_dir():
            return False  # a file is in the way; not ours to resolve

        # Only migrate into a destination that has no state of its own. A destination that
        # already has state is either a completed migration or a newer install, and merging
        # stale credentials into it would be worse than ignoring the old copy.
        if _has_state(current):
            return False
        if not _has_state(legacy):
            return False

        moved = False
        # `list()` is load-bearing: this loop moves entries out of `legacy`, and mutating a
        # directory while iterating it skips entries on some filesystems.
        for entry in sorted(list(legacy.iterdir())):
            target = current / entry.name
            if target.exists():
                continue
            entry.rename(target)
            moved = True
        return moved
    except OSError:
        return False


def _has_state(directory: Path) -> bool:
    """Whether a config directory holds anything the user would miss.

    Deliberately narrower than "is not empty": ``logs/`` and the write probes are regenerable,
    and treating a stray log directory as proof of a completed migration is precisely the
    mistake that stranded a working config behind a setup wizard.
    """
    if not directory.is_dir():
        return False
    return any((directory / name).exists() for name in _STATE_FILENAMES)


# Everything a fresh install would not have. Deliberately a list rather than a directory scan:
# a new entry added here later is a conscious decision about whether it is worth migrating.
_STATE_FILENAMES = (
    "config.json",
    "settings.json",
    "mcp_servers.json",
    "whatsapp.json",
    "custom_endpoints.json",
    "dashboard_settings.json",
    "dashboard_projects.json",
    "prompt_history.txt",
    "audit.jsonl",
    "memory.db",
    "session.json",
    "history",
    "skills",
    "agents",
    "commands",
    "checkpoints",
    "uploads",
    LEGACY_INSTRUCTIONS_FILENAME,
    "MYRIAXON.md",
)


def _dir_is_writable(directory: Path) -> bool:
    """Whether ``directory`` exists or can be created.

    Unlike ``_is_writable`` below this writes no probe file, which matters because it runs
    *before* :func:`migrate_legacy_state`. Testing writability by creating the directory is
    what made the migration see a destination that already existed and skip itself.
    """
    try:
        directory.mkdir(parents=True, exist_ok=True)
        return True
    except OSError:
        return False


def _resolve_config_dir() -> Path:
    """Pick the state directory, preferring one that is actually writable.

    Order:
      1. $MYRIAXON_CONFIG_DIR, when set and usable -- an explicit choice always wins.
      2. ~/.myriaxon, when a file can really be created there.
      3. ./.myriaxon next to the code, when ~/.myriaxon is read-only.

    Step 3 exists because the default is not universally writable. Measured on a managed Windows
    machine: ~/.myriaxon denied both creating a new file AND modifying the existing config.json (ACLs
    that allow delete but not modify), so nothing could ever be saved there and every launch fell
    through to the setup wizard. Requiring the user to discover and export an environment variable
    before the tool can remember their settings is a poor trade, so the fallback is automatic.

    Writability is established by actually creating and removing a probe file, not by os.access --
    on Windows os.access(W_OK) consults the ACL and reports True for the very file that cannot be
    written, which is the exact mistake that made an earlier diagnosis wrong.

    A pre-rebrand ``~/.iohcode`` is adopted rather than ignored: if the new directory does not exist
    yet and the old one does, the old one is renamed into place. See :func:`migrate_legacy_state`.
    """
    override = os.environ.get("MYRIAXON_CONFIG_DIR", "").strip()
    if override:
        try:
            expanded = Path(override).expanduser()
            expanded.mkdir(parents=True, exist_ok=True)
            return expanded
        except OSError:
            pass  # a bad override must not break startup; fall through to the probes

    home = Path.home() / ".myriaxon"
    # Order matters: confirm the location is creatable WITHOUT creating it, migrate into a
    # destination that therefore does not exist yet, and only then probe for real. Creating the
    # directory in order to test it is what defeated the migration -- see migrate_legacy_state.
    if _dir_is_writable(home.parent):
        migrate_legacy_state(Path.home() / LEGACY_CONFIG_DIR_NAME, home)
        if _is_writable(home):
            return home

    # Fallback lives next to the CODE, not next to the current working directory. Measured trap:
    # resolving it from cwd meant running `python -m myriaxon` from any other folder found no config
    # and dropped into the setup wizard, so the saved provider appeared to be ignored. The package
    # location is stable regardless of where the user happens to be working.
    repo_root = Path(__file__).resolve().parent.parent
    local = repo_root / ".myriaxon"
    if _dir_is_writable(repo_root):
        migrate_legacy_state(repo_root / LEGACY_CONFIG_DIR_NAME, local)
        if _is_writable(local):
            return local

    # Nothing is writable: return the conventional path so the failure surfaces at the save site
    # with a clear message, rather than silently redirecting somewhere unexpected.
    return home


def _is_writable(directory: Path) -> bool:
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / ".myriaxon_write_probe"
        probe.write_text("", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return True
    except OSError:
        return False


CONFIG_DIR = _resolve_config_dir()
CONFIG_FILE = CONFIG_DIR / "config.json"
# Persisted across launches (previously prompt_toolkit's InMemoryHistory, which reset to empty
# every session) -- a plain filename, not a directory, so it can't collide with
# SHADOW_HISTORY_DIR ("history/") below despite both loosely being "history".
PROMPT_HISTORY_FILE = CONFIG_DIR / "prompt_history.txt"

USER_SKILLS_DIR = CONFIG_DIR / "skills"
PROJECT_SKILLS_SUBDIR = ".myriaxon/skills"

MCP_SERVERS_FILE = CONFIG_DIR / "mcp_servers.json"
# Messaging (WhatsApp via Twilio/Meta, or a generic webhook). Holds credentials, so it lives beside
# mcp_servers.json and is written with the same permissions rather than inside a repository.
MESSAGING_FILE = CONFIG_DIR / "whatsapp.json"
MCP_BETA_HEADER = "mcp-client-2025-11-20"
MCP_STDIO_CONNECT_TIMEOUT = 30
MCP_STDIO_CALL_TIMEOUT = 60

WEB_SEARCH_TOOL_TYPE = "web_search_20260209"
WEB_FETCH_TOOL_TYPE = "web_fetch_20260209"

TASK_RESULT_CHAR_CAP = 100_000
NOTEBOOK_SOURCE_CHAR_CAP = 20_000
MAX_PAUSE_TURN_RESTARTS = 5

# Consecutive identical (tool name, arguments) calls before dispatch_tool refuses to run it again
# and tells the model to change approach -- a cheap, provider-agnostic stuck-in-a-loop guard.
LOOP_DETECTION_THRESHOLD = 4

# A completed OpenAI-compatible stream with no text and no tool calls is a degenerate provider
# completion (documented DeepSeek behavior) worth a bounded retry rather than accepting silently.
MAX_EMPTY_RESPONSE_RETRIES = 2

# Confirmed live against OpenRouter (google/gemini-3.8-flash, a model released the day before
# this was written): a request that would otherwise succeed can fail non-deterministically with
# a real 5xx, a dropped connection, or a mid-stream SSE event carrying an embedded error object
# with no HTTP status at all -- three different shapes of the same underlying problem (an
# unstable upstream inference backend), none of which are the model or this codebase being
# wrong. Worth a short bounded retry before giving up, same rationale as MAX_EMPTY_RESPONSE_RETRIES
# just above -- never applied to permanent errors (auth/rate-limit/bad-request/not-found).
TRANSIENT_ERROR_RETRIES = 3
TRANSIENT_ERROR_BACKOFF_SECONDS = 2.0

EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
CONTEXT_WARNING_THRESHOLD = 0.8
# Past this fraction of the context window, OpenAI-compat/Gemini auto-compact instead of just
# warning (Anthropic already gets real server-side auto-compaction and doesn't need this).
AUTO_COMPACT_THRESHOLD = 0.92
# Tool results older than this many turns, and larger than this many characters, get pruned to
# a short placeholder before a full /compact is ever needed -- see compaction.py.
STALE_TOOL_RESULT_MAX_AGE_TURNS = 6
STALE_TOOL_RESULT_MIN_CHARS = 2000
# Rolling-window /compact: summarize everything except the last N turns, kept verbatim, instead
# of flattening the entire conversation into one summary every time.
COMPACT_KEEP_LAST_TURNS = 4

MODE_MANUAL = "manual"
MODE_AUTO_EDIT = "auto_edit"
MODE_PLAN = "plan"
MODE_AUTO = "auto"
MODES = (MODE_MANUAL, MODE_AUTO_EDIT, MODE_PLAN, MODE_AUTO)
DEFAULT_MODE = MODE_MANUAL

# Best-effort heuristic for Auto mode's safety check -- NOT a security boundary. Auto mode still
# confirms run_bash when the command matches one of these; everything else auto-approves.
RISKY_BASH_PATTERNS = (
    r"rm\s+-rf",
    r"Remove-Item\b.*-Recurse.*-Force|Remove-Item\b.*-Force.*-Recurse",
    r"git\s+push\s+.*--force",
    r"\bshutdown\b",
    r"\brestart-computer\b",
    r"del\s+/s\s*/q|del\s+/q\s*/s",
    r"format\s+[a-zA-Z]:",
    r"diskpart",
    r"\bmkfs\.",
)

ATTACHMENT_SIZE_CAP_BYTES = 32 * 1024 * 1024  # 32MB, matching Anthropic's own document limit
COMPACTION_BETA_HEADER = "compact-2026-01-12"

PROJECT_SESSION_DIR = ".myriaxon"
SESSION_FILENAME = "session.json"

USER_COMMANDS_DIR = CONFIG_DIR / "commands"
PROJECT_COMMANDS_SUBDIR = ".myriaxon/commands"

USER_AGENTS_DIR = CONFIG_DIR / "agents"
PROJECT_AGENTS_SUBDIR = ".myriaxon/agents"

USER_HOOKS_FILE = CONFIG_DIR / "hooks.json"
PROJECT_HOOKS_SUBPATH = ".myriaxon/hooks.json"
HOOK_TIMEOUT_SECONDS = 30

PROJECT_CHECKPOINTS_SUBDIR = ".myriaxon/checkpoints"
# Whole-project "shadow" git history for /rewind -- lives outside the project entirely (unlike
# PROJECT_CHECKPOINTS_SUBDIR's per-file .bak files) so it never collides with a project's own
# .git, and survives even in a project that isn't a git repo at all. Keyed per-project via a
# hash of the resolved project root (see checkpoints._shadow_repo_dir).
SHADOW_HISTORY_DIR = CONFIG_DIR / "history"

# Project instructions file -- lives at the repo root (not inside .myriaxon/), matching
# CLAUDE.md's own convention. USER_INSTRUCTIONS_FILE is the user-global equivalent.
USER_INSTRUCTIONS_FILE = CONFIG_DIR / "MYRIAXON.md"
PROJECT_INSTRUCTIONS_FILENAME = "MYRIAXON.md"
# Open cross-tool convention (Codex and others) -- read as an additional source, never written.
AGENTS_MD_FILENAME = "AGENTS.md"

# glob_search/grep_search exclusions -- always-on directories no one wants an agent crawling,
# plus an optional project-local .myriaxonignore (gitignore-style patterns) and .gitignore itself.
MYRIAXON_IGNORE_FILENAME = ".myriaxonignore"
DEFAULT_IGNORE_PATTERNS = (".git", "__pycache__", ".venv", "venv", "node_modules", ".myriaxon", "*.pyc")

# Layered governance settings -- User -> Project -> System, System wins (an admin/AI-governance
# override), matching gemini-cli's own System-settings-as-final-authority design. Distinct from
# config.json (auth/provider secrets): this file carries policy (tool permission rules, an
# enforced provider/mode lock), and is safe to check into a repo (project layer) or hand out
# read-only via IT-managed machine state (system layer).
# The USER settings file is the machine owner's policy layer. It is kept separate from the PROJECT
# layer so "system > project > user" means something, and it prefers a location that is actually
# writable: measured, `~/.myriaxon` denies file creation on a managed machine, and pointing the user
# layer at an unwritable path makes the dashboard's rule editor fail for a reason unrelated to rules.
# The resolved path is exported so writers and readers cannot disagree about which file is the user
# layer -- they did once, and the same rules were loaded twice as two different layers.
def _resolve_user_settings_file() -> Path:
    candidates = [Path.home() / ".myriaxon"]
    override = os.environ.get("MYRIAXON_USER_CONFIG_DIR", "").strip()
    if override:
        candidates.insert(0, Path(override).expanduser())
    # CONFIG_DIR is already known-writable (it was probed at import), so it is the fallback.
    candidates.append(CONFIG_DIR)

    for directory in candidates:
        settings = directory / "settings.json"
        # Writability is tested FIRST. Checking is_file() first was a bug: this machine has a
        # read-only ~/.myriaxon/settings.json, so the file existed, the function returned it, and the
        # rule editor then failed to write it -- the opposite of the intent, since an existing file
        # that cannot be modified is exactly the case the fallback exists for.
        try:
            directory.mkdir(parents=True, exist_ok=True)
            probe = directory / ".myriaxon_settings_probe"
            probe.write_text("", encoding="utf-8")
            probe.unlink(missing_ok=True)
        except OSError:
            continue
        return settings
    return candidates[-1] / "settings.json"


USER_SETTINGS_FILE = _resolve_user_settings_file()
PROJECT_SETTINGS_SUBPATH = ".myriaxon/settings.json"
SYSTEM_SETTINGS_FILE = Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "Myriaxon" / "settings.json"

# Lightweight always-on governance audit trail (JSONL) -- see audit.py.
AUDIT_LOG_FILE = CONFIG_DIR / "audit.jsonl"

# Per-run token and cost ledger (JSONL) -- see usage.py.
#
# Separate from AUDIT_LOG_FILE on purpose. The audit trail answers "what did the agent DO" and must
# be tamper-evident; this answers "what did it COST" and is routinely rotated, summed and rewritten.
# One file serving both purposes would force the rotation policy of one onto the integrity
# requirements of the other.
USAGE_LEDGER_FILE = CONFIG_DIR / "usage.jsonl"

# Cross-session searchable memory (SQLite + FTS5) -- see memory_store.py.
MEMORY_DB_FILE = CONFIG_DIR / "memory.db"

# Slash-command names already used by cli.py's built-in REPL commands -- a discovered custom
# command with one of these names would silently never fire (built-ins are checked first), so
# discover_commands() skips and warns on a collision instead.
RESERVED_COMMAND_NAMES = {
    "clear", "compact", "yolo", "thinking", "mode", "effort", "max-tokens",
    "model", "reauth", "provider", "exit", "quit", "commands", "cost", "models",
    "fallback-model", "policy", "profile", "theme", "export", "memory", "sandbox", "context",
    "help", "?", "undo", "retry", "trace", "diff", "doctor", "mcp", "vim", "shells", "rewind",
    "ground", "todos", "plugins", "loci",
}
