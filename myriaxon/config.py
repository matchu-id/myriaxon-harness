import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

from rich.console import Console
from rich.markup import escape
from rich.prompt import Prompt

from .atomic_io import atomic_write_text
from .dotenv_support import load_dotenv_into_environ
from .constants import (
    CONFIG_FILE,
    DEEPSEEK_API_KEY_ENV_FALLBACK,
    DEEPSEEK_MODELS,
    DEFAULT_BASE_URL,
    DEFAULT_DEEPSEEK_BASE_URL,
    DEFAULT_DEEPSEEK_MODEL,
    DEFAULT_GEMINI_BASE_URL,
    DEFAULT_GEMINI_MODEL,
    DEFAULT_MODEL,
    DEFAULT_NETRA_BASE_URL,
    DEFAULT_NETRA_MODEL,
    DEFAULT_OLLAMA_BASE_URL,
    DEFAULT_OPENROUTER_BASE_URL,
    DEFAULT_PROVIDER,
    DEFAULT_VERTEX_GLOBAL_LOCATION,
    DEFAULT_VERTEX_LOCATION,
    ENV_API_KEY,
    ENV_BASE_URL,
    ENV_FALLBACK_MODEL,
    ENV_GOOGLE_CLOUD_LOCATION,
    ENV_GOOGLE_CLOUD_PROJECT,
    ENV_GOOGLE_GENAI_USE_VERTEXAI,
    ENV_MODEL,
    ENV_PROVIDER,
    GEMINI_API_KEY_ENV_FALLBACK,
    NETRA_API_KEY_ENV_FALLBACK,
    NETRA_MODELS,
    OLLAMA_PLACEHOLDER_KEY,
    OPENROUTER_API_KEY_ENV_FALLBACK,
    OPENROUTER_FAVORITE_MODELS,
    PROVIDER_ANTHROPIC,
    PROVIDER_DEEPSEEK,
    PROVIDER_NETRA,
    PROVIDER_GEMINI,
    PROVIDER_OLLAMA,
    PROVIDER_OPENAI_COMPAT,
    PROVIDER_OPENROUTER,
)


@dataclass
class Config:
    api_key: str
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    provider: str = DEFAULT_PROVIDER
    # Only meaningful when provider == PROVIDER_GEMINI -- a non-empty vertex_project switches the
    # Gemini client to Vertex AI instead of the direct Gemini API. api_key may legitimately be
    # empty in this mode (Application Default Credentials).
    vertex_project: str = ""
    vertex_location: str = ""
    # Which Google protocol the gemini provider speaks: "auto" resolves per model in the agent
    # (generateContent for the 2.x line and everything Vertex serves, Interactions for gemini-3.x).
    gemini_protocol: str = "auto"
    # Optional -- if set, a rate-limit/quota error on `model` triggers exactly one retry against
    # this model instead of failing the turn outright.
    fallback_model: str = ""


# OS keychain credential storage -- optional (`pip install -e .[keychain]`), lazily imported
# like the openai/google-genai SDKs elsewhere in this codebase. When available, save_config
# stores the real secret in the OS keychain (Windows Credential Manager via the keyring
# package's backend) and writes only a sentinel to config.json; load_config resolves the
# sentinel back to the real key. Falls back to today's plaintext-file behavior transparently
# when the package isn't installed -- existing plaintext keys keep working either way.
_KEYRING_SENTINEL = "<keyring>"
_KEYCHAIN_SERVICE = "myriaxon"
_keyring_module = None
_keyring_checked = False


def _get_keyring():
    global _keyring_module, _keyring_checked
    if not _keyring_checked:
        _keyring_checked = True
        try:
            import keyring

            _keyring_module = keyring
        except ImportError:
            _keyring_module = None
    return _keyring_module


def _keychain_set(provider: str, api_key: str) -> bool:
    kr = _get_keyring()
    if kr is None or not api_key:
        return False
    try:
        kr.set_password(_KEYCHAIN_SERVICE, provider, api_key)
        return True
    except Exception:
        return False


def _keychain_get(provider: str) -> str | None:
    kr = _get_keyring()
    if kr is None:
        return None
    try:
        return kr.get_password(_KEYCHAIN_SERVICE, provider)
    except Exception:
        return None


def load_config() -> Config | None:
    # Load `.env` here as well as on the CLI entry path, because this is the function everything
    # reaches config through. Measured: a fresh deployment whose `.env` sat beside the source worked
    # when launched from that directory and silently fell back to the interactive Anthropic prompt
    # when launched from anywhere else -- because `load_config` reads MYRIAXON_* straight out of the
    # environment and nothing had put them there. The loader never overwrites an already-set
    # variable, so calling it on both paths is safe and idempotent.
    load_dotenv_into_environ()

    if not CONFIG_FILE.exists():
        return None
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        provider = data.get("provider", DEFAULT_PROVIDER)
        api_key = data.get("api_key", "")
        if api_key == _KEYRING_SENTINEL:
            api_key = _keychain_get(provider) or ""
        return Config(
            api_key=api_key,
            base_url=data.get("base_url", DEFAULT_BASE_URL),
            model=data.get("model", DEFAULT_MODEL),
            provider=provider,
            vertex_project=data.get("vertex_project", ""),
            vertex_location=data.get("vertex_location", ""),
            fallback_model=data.get("fallback_model", ""),
        )
    except (json.JSONDecodeError, OSError):
        return None


def save_config(config: Config) -> None:
    data = asdict(config)
    # Ollama's "key" is a placeholder the client library insists on, not a secret: nothing to put in a keychain.
    if config.provider != PROVIDER_OLLAMA and _keychain_set(config.provider, config.api_key):
        data["api_key"] = _KEYRING_SENTINEL
    atomic_write_text(CONFIG_FILE, json.dumps(data, indent=2))
    try:
        os.chmod(CONFIG_FILE, 0o600)
    except OSError:
        pass  # NTFS ACLs already restrict the user profile directory on Windows


def _ask(console: Console, prompt: str, default: str | None, **kwargs) -> str:
    """Prompt.ask, but omit `default` entirely when there isn't one — passing default=None
    would make None itself the accepted empty-Enter answer, not force a real one."""
    if default is not None:
        return Prompt.ask(prompt, default=default, console=console, **kwargs)
    return Prompt.ask(prompt, console=console, **kwargs)


def select_option(
    console: Console,
    message: str,
    options: list[tuple[str, str]],
    default: str | None = None,
    *,
    cancellable: bool = False,
) -> str | None:
    """Arrow-key-navigable single-select menu (Up/Down to move, Enter to confirm) instead of
    typing a choice out of a bracketed list -- confirmed live this was the actual complaint:
    Prompt.ask(choices=[...]) only *validates* typed text against a list, it was never a real
    menu. options is [(value, label), ...]; returns the selected value.

    prompt_toolkit.shortcuts.radiolist_dialog is imported lazily, inside this function, not at
    module load -- prompt_toolkit costs real measured startup time (~6.5s, confirmed via
    `python -X importtime` elsewhere in this codebase) and config.py's auth wizard sits on the
    --reauth/first-run startup path, which should stay fast for anyone who never actually hits
    this function (e.g. -p/--prompt one-shot mode never reaches the auth wizard at all).

    Falls back to a plain typed Prompt.ask if the dialog can't run (no real console attached,
    e.g. some CI/non-interactive contexts) -- same never-crash-over-an-optional-UX-enhancement
    discipline as completion.build_prompt_session.

    **Cancelling.** With `cancellable=True` the user can back out: Escape or Ctrl-C in the menu, or Ctrl-C / end of input
    at the typed fallback, returns None and the caller decides what "nothing chosen" means. Without it (the setup wizard,
    where an answer is required) a cancelled menu falls through to the typed prompt, as it always did. Before this flag
    existed EVERY caller got the wizard behaviour, so pressing Escape in `/mode` or `/theme` did not cancel: it showed a
    second, typed prompt that offered the first option as its default, and closing the input there ended the whole
    session with "Unexpected error"."""
    valid_values = [value for value, _label in options]
    fallback_default = default if default in valid_values else valid_values[0]

    # The menu shows about five rows and scrolls the rest, and nothing in it says so -- which is how a
    # list containing Ollama was read as a list without Ollama. The count is stated when it does not
    # all fit, so a hidden entry is a known quantity rather than a missing one.
    hint = ""
    if len(options) > 5:
        hint = f"{message}   ({len(options)} options — scroll for the rest)"

    try:
        from prompt_toolkit.shortcuts import radiolist_dialog

        result = radiolist_dialog(
            title="Myriaxon", text=hint or message, values=options, default=fallback_default
        ).run()
        if result is not None:
            return result
        if cancellable:
            return None  # Escape or Ctrl-C is an answer ("never mind"), not a reason to ask again
    except Exception:
        pass

    if cancellable:
        try:
            return Prompt.ask(message, choices=valid_values, default=fallback_default, console=console)
        except (KeyboardInterrupt, EOFError):
            return None
    return Prompt.ask(message, choices=valid_values, default=fallback_default, console=console)


# Provider -> the .env var that should auto-fill the API key if present, and a friendly label
# for the provider-selection menu. Anthropic/openai_compat have neither (no universal env var
# convention for the former beyond ANTHROPIC_API_KEY which ENV_API_KEY already covers generically,
# and no fixed identity at all for the latter).
PROVIDER_ENV_KEY_FALLBACK = {
    PROVIDER_GEMINI: GEMINI_API_KEY_ENV_FALLBACK,
    PROVIDER_DEEPSEEK: DEEPSEEK_API_KEY_ENV_FALLBACK,
    PROVIDER_OPENROUTER: OPENROUTER_API_KEY_ENV_FALLBACK,
    PROVIDER_NETRA: NETRA_API_KEY_ENV_FALLBACK,
}
# Order IS the menu, and it matters more than it looks: prompt_toolkit's chooser fits about five rows
# in a normal terminal and scrolls the rest, so whichever entries are last are the ones a user
# scrolls to find -- and scrolling is not something the list announces.
#
# Measured: with the previous order (Anthropic, OpenAI-compatible, OpenRouter, Gemini, DeepSeek,
# Netra, Ollama) a user asked "why is Ollama not listed?". It WAS listed, seventh of seven, one row
# below the fold, together with Netra -- the two providers most likely to be wanted, hidden by the
# order they happened to be written in.
#
# Ordered by "what would someone set up on a fresh machine": the two DeepSeek gateways first (they
# serve the same models and are this build's target), then the local option that needs no account at
# all, then the rest. Nothing depends on the previous order -- the one test that reads this list
# checks membership, not position.
_PROVIDER_LABELS = [
    (PROVIDER_DEEPSEEK, "DeepSeek (api.deepseek.com)"),
    (PROVIDER_NETRA, "Netra (netraruntime)"),
    (PROVIDER_OLLAMA, "Ollama (a model on this computer, no key)"),
    (PROVIDER_ANTHROPIC, "Anthropic"),
    (PROVIDER_GEMINI, "Gemini"),
    (PROVIDER_OPENROUTER, "OpenRouter"),
    (PROVIDER_OPENAI_COMPAT, "OpenAI-compatible (custom endpoint)"),
]
_OTHER_MODEL_SENTINEL = "__other__"


def _ollama_config(console: Console, stored: Config | None, model: str = "") -> Config | None:
    """Find Ollama and say what Myriaxon will use, or print why it cannot and return None.

    A model named `model` is used as it is. Otherwise the model the saved configuration already names (the user chose it
    with `myriaxon ollama --use`), otherwise `wire` picks: the `myriaxon` model if Ollama has one, else it creates it.
    """
    from . import ollama

    here = stored if (stored and stored.provider == PROVIDER_OLLAMA) else None
    if not model and here and here.model:
        return Config(api_key=OLLAMA_PLACEHOLDER_KEY, base_url=here.base_url or DEFAULT_OLLAMA_BASE_URL,
                      model=here.model, provider=PROVIDER_OLLAMA, fallback_model=here.fallback_model)

    def progress(status: str) -> None:
        console.print(f"[dim]  ollama: {escape(status)}[/]")

    wiring, why = ollama.wire(model=model, progress=progress)
    if wiring is None:
        console.print(f"[error]{escape(why)}[/]")
        return None
    for note in wiring.notes:
        console.print(f"[dim]{escape(note)}[/]")
    return Config(api_key=OLLAMA_PLACEHOLDER_KEY, base_url=wiring.base_url, model=wiring.model, provider=PROVIDER_OLLAMA,
                  fallback_model=here.fallback_model if here else "")


def _has_any_key() -> bool:
    """Whether the environment already names a provider key, which means the user has chosen a cloud provider."""
    names = [ENV_API_KEY, *PROVIDER_ENV_KEY_FALLBACK.values()]
    return any((os.environ.get(name) or "").strip() for name in names)


def switch_vendor(current: Config | None, provider: str, model: str = "") -> tuple[Config | None, str]:
    """Point a configuration at one of the three gateways this build targets.

    Returns (config, problem): `config` is None when the switch cannot be made, and `problem` then
    says why in words the caller can print. Exactly one of the two is set.

    Why this is shared rather than written twice: the dashboard had its own `_switch_provider`, and
    the CLI had `/provider`, and they disagreed -- the CLI's list omitted `ollama` and `netra`, and the
    dashboard's read the environment's endpoint for the WRONG provider, so `/provider ollama` with a
    Netra `.env` sent ollama requests to Netra and failed 401. One function cannot disagree with
    itself.

    The three differ in what a switch must remap, which is the whole reason this is a function rather
    than a dict lookup:
      * `deepseek` and `netra` are keyed gateways: endpoint, model and credential all change together,
        and the key is looked for in the environment (how a `.env` supplies it) and then in the saved
        configuration (how a key typed into a surface is remembered). A missing key is reported, not
        guessed at, because the alternative is a 401 during the user's first task.
      * `ollama` has no key at all and its model must come from what is installed on this machine --
        an empty model name is not a usable state (`404 model not found` on the next turn), so one is
        chosen from what Ollama actually has, preferring the model `myriaxon ollama --create` built.
    """
    wanted = (provider or "").strip().lower()
    if wanted not in (PROVIDER_DEEPSEEK, PROVIDER_NETRA, PROVIDER_OLLAMA, PROVIDER_GEMINI):
        return None, (f"unknown vendor {provider!r}: choose {PROVIDER_DEEPSEEK}, {PROVIDER_NETRA}, "
                      f"{PROVIDER_GEMINI} or {PROVIDER_OLLAMA}")

    stored = load_config()
    same = stored if (stored is not None and stored.provider == wanted) else None
    # The environment only speaks for the provider it names. A `.env` that configures netra must not
    # supply netra's host to an ollama switch -- measured as a 401, and the defect this rule fixes.
    same_environment = (os.environ.get(ENV_PROVIDER) or "").strip().lower() in ("", wanted)

    if wanted == PROVIDER_GEMINI:
        # Gemini is switchable WITHOUT an API key when Vertex AI is configured, because the credential
        # is the operator's own gcloud login (`gcloud auth print-access-token`, refreshed by
        # `vertex_auth`). Verified live: `gemini-3.8-flash` on the `global` endpoint, no key on the
        # machine.
        #
        # This matters because the gateway rule below -- "a missing key is reported, not guessed at" --
        # would otherwise refuse the switch for exactly the setup that works, and the refusal would
        # read as "Gemini is unavailable" rather than "Gemini needs a project".
        project = (os.environ.get(ENV_GOOGLE_CLOUD_PROJECT) or "").strip()
        if not project and same is not None:
            project = (same.vertex_project or "").strip()
        wants_vertex = (os.environ.get(ENV_GOOGLE_GENAI_USE_VERTEXAI, "").strip().lower() == "true"
                        or bool(project))
        if wants_vertex:
            if not project:
                return None, (
                    "Gemini over Vertex AI needs a GCP project. Set GOOGLE_CLOUD_PROJECT=<project> "
                    "and GOOGLE_GENAI_USE_VERTEXAI=true in .env, then sign in with `gcloud auth login`."
                )
            location = ((os.environ.get(ENV_GOOGLE_CLOUD_LOCATION) or "").strip()
                        or (same.vertex_location if same else "")
                        or DEFAULT_VERTEX_GLOBAL_LOCATION)
            return Config(
                api_key="",                      # legitimate in Vertex mode: the credential is gcloud
                base_url=DEFAULT_GEMINI_BASE_URL,
                model=(model.strip() or (same.model if same else "") or DEFAULT_GEMINI_MODEL),
                provider=PROVIDER_GEMINI,
                vertex_project=project,
                vertex_location=location,
            ), ""

        # No Vertex: the direct Gemini API needs a real key.
        key = (os.environ.get(GEMINI_API_KEY_ENV_FALLBACK) or "").strip()
        if not key and same is not None:
            key = (same.api_key or "").strip()
        if not key:
            return None, (
                f"Gemini needs either an API key ({GEMINI_API_KEY_ENV_FALLBACK}) or Vertex AI. "
                "For Vertex -- which needs no key, only your gcloud login -- set "
                "GOOGLE_CLOUD_PROJECT and GOOGLE_GENAI_USE_VERTEXAI=true in .env."
            )
        return Config(
            api_key=key,
            base_url=DEFAULT_GEMINI_BASE_URL,
            model=model.strip() or (same.model if same else "") or DEFAULT_GEMINI_MODEL,
            provider=PROVIDER_GEMINI,
        ), ""

    stored = load_config()
    same = stored if (stored is not None and stored.provider == wanted) else None
    # The environment only speaks for the provider it names. A `.env` that configures netra must not
    # supply netra's host to an ollama switch -- measured as a 401, and the defect this rule fixes.
    same_environment = (os.environ.get(ENV_PROVIDER) or "").strip().lower() in ("", wanted)

    if wanted == PROVIDER_OLLAMA:
        from . import ollama as ollama_module

        try:
            probe = ollama_module.detect(with_details=True, timeout=3.0)
        except Exception as exc:  # noqa: BLE001 - a switch must still work if the probe fails
            return None, f"could not ask Ollama what it has ({type(exc).__name__}: {exc})"
        if not probe.available:
            return None, probe.explain()
        chosen = model.strip()
        if not chosen:
            made = probe.model(ollama_module.MYRIAXON_MODEL_NAME)
            if made is not None and ollama_module.tool_capable(made):
                chosen = made.name
            else:
                chosen = ollama_module.default_model(probe.models)
        if not chosen:
            return None, ("Ollama is running but has no model that can call tools. Pull one with:  "
                          "ollama pull qwen2.5-coder:7b")
        return Config(
            api_key=OLLAMA_PLACEHOLDER_KEY,
            base_url=DEFAULT_OLLAMA_BASE_URL,
            model=chosen,
            provider=PROVIDER_OLLAMA,
        ), ""

    default_base, default_model, key_env = (
        (DEFAULT_DEEPSEEK_BASE_URL, DEFAULT_DEEPSEEK_MODEL, DEEPSEEK_API_KEY_ENV_FALLBACK)
        if wanted == PROVIDER_DEEPSEEK
        else (DEFAULT_NETRA_BASE_URL, DEFAULT_NETRA_MODEL, NETRA_API_KEY_ENV_FALLBACK)
    )
    key = (
        (os.environ.get(key_env) or "").strip()
        or (same.api_key if same else "").strip()
        or (os.environ.get(ENV_API_KEY) or "").strip()
    )
    if not key:
        return None, (f"{wanted} needs an API key. Put {key_env}=... in .env, run "
                      f"`myriaxon --reauth`, or set it in the dashboard's vendor picker.")

    base_url = default_base
    if same_environment and os.environ.get(ENV_BASE_URL) and not (same and same.base_url):
        base_url = os.environ[ENV_BASE_URL]
    elif same and same.base_url:
        base_url = same.base_url
    # Precedence, highest first: a model named in the call, the environment's model (only when the
    # environment is talking about this provider), the model last saved for it, the default. The
    # first version of this line read `model.strip() or environ if same_environment else model`,
    # which binds as `(model or environ) if same_environment else model` -- so a model the caller
    # named was DISCARDED whenever the environment was talking about another provider.
    chosen_model = model.strip()
    if not chosen_model and same_environment:
        chosen_model = (os.environ.get(ENV_MODEL) or "").strip()
    if not chosen_model and same and same.model:
        chosen_model = same.model
    chosen_model = chosen_model or default_model
    return Config(api_key=key, base_url=base_url, model=chosen_model, provider=wanted), ""


def run_auth_wizard(console: Console, existing: Config | None, provider: str = "") -> Config:
    """Ask for an endpoint and a key. `provider` pre-selects one and skips the menu.

    The parameter exists for `--vendor`: when the user has already named the gateway on the command
    line, asking them to pick it again from a list is a question with a known answer.
    """
    console.print("\n[banner]Myriaxon setup[/] — no sign-in required, just an endpoint and an API key.\n")

    if not provider:
        provider = select_option(
            console,
            "Select a provider",
            _PROVIDER_LABELS,
            default=existing.provider if existing else DEFAULT_PROVIDER,
        )

    if provider == PROVIDER_OLLAMA:
        config = _ollama_config(console, existing)
        if config is None:
            console.print("[dim]Pick another provider, or start Ollama and choose this one again.[/]\n")
            return run_auth_wizard(console, existing)
        save_config(config)
        console.print(f"[success]Saved to {escape(str(CONFIG_FILE))}[/]\n")
        return config

    same_provider = existing if existing and existing.provider == provider else None
    vertex_project = ""
    vertex_location = ""

    if provider == PROVIDER_ANTHROPIC:
        base_url_default = same_provider.base_url if same_provider else DEFAULT_BASE_URL
        model_default = same_provider.model if same_provider else DEFAULT_MODEL
        api_key_default = None
    elif provider == PROVIDER_GEMINI:
        base_url_default = same_provider.base_url if same_provider else DEFAULT_GEMINI_BASE_URL
        model_default = same_provider.model if same_provider else DEFAULT_GEMINI_MODEL
        # honor a GEMINI_API_KEY already sitting in the environment (e.g. loaded from .env)
        api_key_default = os.environ.get(GEMINI_API_KEY_ENV_FALLBACK)
    elif provider == PROVIDER_DEEPSEEK:
        base_url_default = DEFAULT_DEEPSEEK_BASE_URL
        model_default = same_provider.model if same_provider else DEFAULT_DEEPSEEK_MODEL
        # honor a DEEPSEEK_API_KEY already sitting in the environment (e.g. loaded from .env)
        api_key_default = os.environ.get(DEEPSEEK_API_KEY_ENV_FALLBACK)
    elif provider == PROVIDER_NETRA:
        base_url_default = DEFAULT_NETRA_BASE_URL
        model_default = same_provider.model if same_provider else DEFAULT_NETRA_MODEL
        # Same fallback pattern as DeepSeek/OpenRouter -- NETRA_API_KEY is normally supplied via .env.
        api_key_default = os.environ.get(NETRA_API_KEY_ENV_FALLBACK)
    elif provider == PROVIDER_OPENROUTER:
        base_url_default = DEFAULT_OPENROUTER_BASE_URL
        model_default = same_provider.model if same_provider else OPENROUTER_FAVORITE_MODELS[0]
        # honor an OPENROUTER_API_KEY already sitting in the environment (e.g. loaded from .env)
        api_key_default = os.environ.get(OPENROUTER_API_KEY_ENV_FALLBACK)
    else:
        # No universal default endpoint/model for a generic OpenAI-compatible provider -- only
        # offer a default if re-configuring the same provider.
        base_url_default = same_provider.base_url if same_provider else None
        model_default = same_provider.model if same_provider else None
        api_key_default = None

    use_vertex = False
    if provider == PROVIDER_GEMINI:
        vertex_default = "y" if (
            (same_provider and same_provider.vertex_project)
            or os.environ.get(ENV_GOOGLE_GENAI_USE_VERTEXAI, "").lower() == "true"
        ) else "n"
        use_vertex = select_option(
            console,
            "Use Vertex AI (a GCP project) instead of the direct Gemini API endpoint?",
            [("y", "Yes"), ("n", "No")],
            default=vertex_default,
        ) == "y"

    if use_vertex:
        base_url = DEFAULT_GEMINI_BASE_URL  # unused in Vertex mode; kept so the field is never blank
        vertex_project = _ask(
            console,
            "GCP project id",
            (same_provider.vertex_project if same_provider else "") or os.environ.get(ENV_GOOGLE_CLOUD_PROJECT),
        )
        vertex_location = _ask(
            console,
            "GCP location/region",
            (same_provider.vertex_location if same_provider else "")
            or os.environ.get(ENV_GOOGLE_CLOUD_LOCATION)
            # `global`, not us-central1: the newest model line (gemini-3.x) is served ONLY on the
            # multi-region global endpoint, so a regional default turns a working model into a 404
            # the moment the user picks it. Anyone who needs a specific region can type it here.
            or DEFAULT_VERTEX_GLOBAL_LOCATION,
        )
        console.print(
            "[dim]Leave the API key blank to use Application Default Credentials instead "
            "(gcloud auth application-default login, GOOGLE_APPLICATION_CREDENTIALS, or "
            "GCE/Cloud Run metadata credentials) rather than Vertex AI express-mode API keys.[/]"
        )
        api_key = Prompt.ask("API key (optional)", password=True, default="", console=console)
    else:
        if provider in (PROVIDER_DEEPSEEK, PROVIDER_OPENROUTER, PROVIDER_NETRA):
            # Fixed endpoint -- don't ask, just use it. Netra was missing from this tuple, so the
            # wizard prompted for an endpoint it already knew, pre-filled with the right answer.
            # A question whose only correct response is the value already showing is noise.
            base_url = base_url_default
        else:
            base_url = _ask(console, "API endpoint", base_url_default)

        env_var_name = PROVIDER_ENV_KEY_FALLBACK.get(provider)
        if api_key_default and select_option(
            console, f"Found {env_var_name} in the environment. Use it?", [("y", "Yes"), ("n", "No")], default="y"
        ) == "y":
            api_key = api_key_default
        else:
            # Confirmed live this was a real gap: a blank Enter here used to save an empty
            # api_key straight to config.json, only surfacing as a confusing "Missing
            # credentials" crash deep inside the SDK client constructor much later, on the very
            # next launch -- not at the moment of the actual mistake. Loop until non-blank
            # instead (Vertex AI's "API key (optional)" prompt above is the one legitimate case
            # where blank is valid, since ADC doesn't need a key at all).
            api_key = Prompt.ask("API key", password=True, console=console)
            while not api_key:
                console.print("[warning]API key cannot be blank for this provider.[/]")
                api_key = Prompt.ask("API key", password=True, console=console)

    if provider == PROVIDER_DEEPSEEK:
        model = select_option(
            console, "Select a model", [(m, m) for m in DEEPSEEK_MODELS], default=model_default
        )
    elif provider == PROVIDER_NETRA:
        # Small, known model set -- same shortlist treatment as DeepSeek rather than free text, so a
        # typo in a vendor-namespaced id cannot silently produce a 404 on the first request.
        model = select_option(
            console, "Select a model", [(m, m) for m in NETRA_MODELS], default=model_default
        )
    elif provider == PROVIDER_OPENROUTER:
        model_choice = select_option(
            console,
            "Select a model (favorites shown -- OpenRouter has hundreds more)",
            [(m, m) for m in OPENROUTER_FAVORITE_MODELS] + [(_OTHER_MODEL_SENTINEL, "Other (type manually)")],
            default=model_default if model_default in OPENROUTER_FAVORITE_MODELS else OPENROUTER_FAVORITE_MODELS[0],
        )
        model = _ask(console, "Model", model_default) if model_choice == _OTHER_MODEL_SENTINEL else model_choice
    else:
        model = _ask(console, "Model", model_default)

    fallback_model = Prompt.ask(
        "Fallback model on rate-limit/quota errors (optional, leave blank to disable)",
        default=(same_provider.fallback_model if same_provider else ""),
        console=console,
    )

    config = Config(
        api_key=api_key.strip(),
        base_url=base_url.strip(),
        model=model.strip(),
        provider=provider,
        vertex_project=vertex_project.strip(),
        vertex_location=vertex_location.strip(),
        fallback_model=fallback_model.strip(),
    )
    save_config(config)
    console.print(f"[success]Saved to {escape(str(CONFIG_FILE))}[/]\n")
    return config


def _adc_quota_project() -> str | None:
    """The quota project recorded in the Application Default Credentials file, if any.

    Lets `GOOGLE_GENAI_USE_VERTEXAI=true` alone configure a machine that is already signed in with
    gcloud, without the user also having to export the project id. Never raises: a machine with no
    ADC simply gets no project and the caller falls back to API-key mode.
    """
    import json

    path = Path(os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")) if os.environ.get(
        "GOOGLE_APPLICATION_CREDENTIALS"
    ) else Path(os.environ.get("APPDATA", "")) / "gcloud" / "application_default_credentials.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    return data.get("quota_project_id") or data.get("project_id") or None


def resolve_config(
    console: Console,
    reauth: bool = False,
    ollama: bool = False,
    model: str = "",
    vendor: str = "",
) -> Config:
    """The configuration to run with.

    `ollama=True` (the `--ollama` flag) and `MYRIAXON_PROVIDER=ollama` use the Ollama on this computer, with `model` if one
    was named. A machine with nothing saved and no key in the environment tries Ollama before it asks for a key: that is
    the machine where the answer to "which provider?" is already on the disk. When Ollama was asked for and cannot be
    used, the reason is printed and the process exits: falling through to the key prompt would be answering a different
    question.

    `vendor` is the `--vendor` flag: "deepseek" or "netra", the two gateways that serve the same models.
    It is resolved FIRST, because an explicit flag is the user saying which one they want now, and a
    saved choice for the other one must not silently win. When the chosen gateway has no key, the
    setup runs at that moment -- so the missing credential is asked for at startup rather than
    surfacing as a 401 in the middle of the first real task.
    """
    stored = load_config()

    if reauth:
        return run_auth_wizard(console, stored)

    if vendor:
        wanted = vendor.strip().lower()
        # THE FOUR VENDORS ARGPARSE ADVERTISES ARE THE FOUR THAT WORK HERE.
        #
        # This guard used to accept only `deepseek` and `netra` and reject the rest with
        # "--vendor must be 'deepseek' or 'netra'", while `argparse_cli` declared
        # `choices=["deepseek", "netra", "gemini", "ollama"]` and its help text documented gemini's
        # Vertex mode in detail. Measured: `--vendor gemini` exited 2 with that message, so the
        # documented way to select Gemini on the command line could never have worked.
        #
        # The block below is also why this drifted: it re-implements provider resolution for the two
        # keyed gateways only, while `switch_vendor` already handles all four -- and the docstring on
        # `switch_vendor` records that this duplication has caused a disagreement between surfaces
        # before ("one function cannot disagree with itself"). So the providers this block does NOT
        # model are delegated rather than rejected.
        if wanted in (PROVIDER_GEMINI, PROVIDER_OLLAMA):
            switched, problem = switch_vendor(stored, wanted, model)
            if switched is None:
                console.print(f"[error]{problem}[/]")
                raise SystemExit(2)
            save_config(switched)
            return switched
        if wanted not in (PROVIDER_DEEPSEEK, PROVIDER_NETRA):
            # Still named rather than dumped: a typo should say what IS accepted.
            console.print(
                f"[error]--vendor must be one of {PROVIDER_DEEPSEEK!r}, {PROVIDER_NETRA!r}, "
                f"{PROVIDER_GEMINI!r} or {PROVIDER_OLLAMA!r}, not {vendor!r}[/]"
            )
            raise SystemExit(2)
        same = stored if (stored is not None and stored.provider == wanted) else None
        key_env = PROVIDER_ENV_KEY_FALLBACK.get(wanted, "")
        key = (
            (os.environ.get(key_env) or "").strip()
            or (same.api_key if same else "").strip()
            or (os.environ.get(ENV_API_KEY) or "").strip()
        )
        if not key:
            console.print(f"[dim]{wanted} has no API key yet -- one is needed before the first turn.[/]")
            chosen = run_auth_wizard(console, stored, provider=wanted)
            if chosen.provider == wanted and (chosen.api_key or "").strip():
                save_config(chosen)
            return chosen
        default_base, default_model = (
            (DEFAULT_DEEPSEEK_BASE_URL, DEFAULT_DEEPSEEK_MODEL)
            if wanted == PROVIDER_DEEPSEEK
            else (DEFAULT_NETRA_BASE_URL, DEFAULT_NETRA_MODEL)
        )
        return Config(
            api_key=key,
            base_url=(same.base_url if same and same.base_url else default_base),
            model=(model or (same.model if same and same.model else "") or default_model),
            provider=wanted,
        )

    named = os.environ.get(ENV_PROVIDER)
    if ollama or named == PROVIDER_OLLAMA or (not named and stored and stored.provider == PROVIDER_OLLAMA):
        config = _ollama_config(console, stored, model or os.environ.get(ENV_MODEL) or "")
        if config is None:
            console.print("[dim]Start Ollama (`ollama serve`, or open the Ollama app) and pull a model, or run "
                          "`myriaxon --reauth` to use a cloud provider instead.[/]")
            raise SystemExit(1)
        return config
    if not named and not stored and not _has_any_key():
        # Nothing chosen anywhere. Offer what is already on this computer before asking for an account.
        from . import ollama as ollama_module

        if ollama_module.detect(with_details=False, timeout=1.0).available:
            config = _ollama_config(console, None, model)
            if config is not None:
                console.print("[dim]No API key is set, so Myriaxon is using the Ollama model on this computer. "
                              "`myriaxon --reauth` chooses a cloud provider instead.[/]")
                return config

    provider = named or (stored.provider if stored else DEFAULT_PROVIDER)

    # When the environment names a DIFFERENT provider than the stored one, the stored
    # provider-specific values must not be reused -- they belong to the other provider. Measured
    # failure this prevents: `MYRIAXON_PROVIDER=netra` with a saved Gemini config produced
    # provider=netra but base_url=https://generativelanguage.googleapis.com/, so every request went
    # to Google and returned 404. The same leak would send Netra/DeepSeek/OpenRouter traffic to
    # whatever endpoint happened to be saved, and would apply a saved GCP project to a non-Gemini
    # provider.
    #
    # Stored values are still used when the provider matches, and an explicit env var always wins.
    stored_for_provider = stored if (stored and stored.provider == provider) else None

    api_key = os.environ.get(ENV_API_KEY) or (stored_for_provider.api_key if stored_for_provider else "")
    if provider == PROVIDER_NETRA and not api_key:
        api_key = os.environ.get(NETRA_API_KEY_ENV_FALLBACK) or ""
    vertex_project = ""
    vertex_location = ""

    if provider == PROVIDER_ANTHROPIC:
        base_url = os.environ.get(ENV_BASE_URL) or (
            stored_for_provider.base_url if stored_for_provider else DEFAULT_BASE_URL
        )
        model = os.environ.get(ENV_MODEL) or (
            stored_for_provider.model if stored_for_provider else DEFAULT_MODEL
        )
    elif provider == PROVIDER_GEMINI:
        # Vertex mode can be requested either by naming a project (GOOGLE_CLOUD_PROJECT /
        # stored config) or by the standard GOOGLE_GENAI_USE_VERTEXAI=true switch that the Google
        # SDKs and gemini-cli use. The flag previously existed as a constant but was never read, so
        # setting it alone silently did nothing and the run fell back to API-key auth -- which is a
        # confusing failure for exactly the corporate-credential setup the flag exists for.
        wants_vertex = os.environ.get(ENV_GOOGLE_GENAI_USE_VERTEXAI, "").strip().lower() == "true"
        vertex_project = (
            os.environ.get(ENV_GOOGLE_CLOUD_PROJECT)
            or (stored.vertex_project if stored else "")
            or (os.environ.get("GCLOUD_PROJECT") if wants_vertex else "")
            or (os.environ.get("GOOGLE_CLOUD_PROJECT") if wants_vertex else "")
        )
        if wants_vertex and not vertex_project:
            # The flag is on but no project is resolvable; try the credential file's own quota
            # project so `GOOGLE_GENAI_USE_VERTEXAI=true` alone is enough for a signed-in machine.
            vertex_project = _adc_quota_project() or ""
        if vertex_project:
            vertex_location = (
                os.environ.get(ENV_GOOGLE_CLOUD_LOCATION)
                or (stored.vertex_location if stored else "")
                # `global` rather than a single region: the newest model line (gemini-3.x) is served
                # only there, and a region default turns a working model into a 404. A user who wants
                # a specific region can still set it explicitly, or store it in config.
                or DEFAULT_VERTEX_GLOBAL_LOCATION
            )
            # Vertex authenticates with OAuth (ADC / workforce identity), never an API key. A key
            # left over from another provider would make the SDK prefer key auth and fail with
            # "API keys are not supported by this API" -- measured live. Cleared here so the
            # credential is unambiguous, and the CLI prints what it did.
            api_key = ""
        elif not api_key:
            api_key = os.environ.get(GEMINI_API_KEY_ENV_FALLBACK) or ""
        base_url = os.environ.get(ENV_BASE_URL) or (
            stored_for_provider.base_url if stored_for_provider else DEFAULT_GEMINI_BASE_URL
        )
        model = os.environ.get(ENV_MODEL) or (
            stored_for_provider.model if stored_for_provider else DEFAULT_GEMINI_MODEL
        )
    elif provider == PROVIDER_DEEPSEEK:
        if not api_key:
            api_key = os.environ.get(DEEPSEEK_API_KEY_ENV_FALLBACK) or ""
        base_url = os.environ.get(ENV_BASE_URL) or (
            stored_for_provider.base_url if stored_for_provider else DEFAULT_DEEPSEEK_BASE_URL
        )
        model = os.environ.get(ENV_MODEL) or (
            stored_for_provider.model if stored_for_provider else DEFAULT_DEEPSEEK_MODEL
        )
    elif provider == PROVIDER_NETRA:
        if not api_key:
            api_key = os.environ.get(NETRA_API_KEY_ENV_FALLBACK) or ""
        base_url = os.environ.get(ENV_BASE_URL) or (
            stored_for_provider.base_url if stored_for_provider else DEFAULT_NETRA_BASE_URL
        )
        model = os.environ.get(ENV_MODEL) or (
            stored_for_provider.model if stored_for_provider else DEFAULT_NETRA_MODEL
        )
    elif provider == PROVIDER_OPENROUTER:
        # If no key was manually configured/passed, fall back to OPENROUTER_API_KEY from the
        # environment (e.g. loaded from .env via load_dotenv_into_environ) -- same pattern as
        # Gemini/DeepSeek above.
        if not api_key:
            api_key = os.environ.get(OPENROUTER_API_KEY_ENV_FALLBACK) or ""
        base_url = os.environ.get(ENV_BASE_URL) or (
            stored_for_provider.base_url if stored_for_provider else DEFAULT_OPENROUTER_BASE_URL
        )
        model = os.environ.get(ENV_MODEL) or (
            stored_for_provider.model if stored_for_provider else OPENROUTER_FAVORITE_MODELS[0]
        )
    else:
        base_url = os.environ.get(ENV_BASE_URL) or (
            stored_for_provider.base_url if stored_for_provider else ""
        )
        model = os.environ.get(ENV_MODEL) or (stored_for_provider.model if stored_for_provider else "")

    # Vertex AI's Application Default Credentials mode legitimately has no api_key -- only
    # require one when Vertex isn't in play.
    if (not api_key and not vertex_project) or not base_url or not model:
        return run_auth_wizard(console, stored)

    fallback_model = os.environ.get(ENV_FALLBACK_MODEL) or (
        stored_for_provider.fallback_model if stored_for_provider else ""
    )

    # Which Google protocol to speak. Env override wins so a user can switch without editing config.
    gemini_protocol = os.environ.get("MYRIAXON_GEMINI_PROTOCOL", "").strip().lower() or "auto"
    if gemini_protocol not in ("auto", "generate", "interactions"):
        gemini_protocol = "auto"

    return Config(
        api_key=api_key,
        base_url=base_url,
        model=model,
        provider=provider,
        vertex_project=vertex_project,
        vertex_location=vertex_location,
        gemini_protocol=gemini_protocol,
        fallback_model=fallback_model,
    )
