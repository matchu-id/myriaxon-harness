from dataclasses import dataclass

from .constants import DEEPSEEK_MAX_INPUT_TOKENS, DEEPSEEK_MODELS, PROVIDER_ANTHROPIC, PROVIDER_DEEPSEEK, PROVIDER_GEMINI

# Best-effort model-info lookup at every startup -- confirmed live this was taking ~14s against
# DeepSeek's real /models endpoint with no timeout at all, blocking the whole launch. Bounded
# here so a slow/unresponsive endpoint degrades to the caller's hardcoded defaults quickly
# instead of stalling startup; all three SDKs support a per-call timeout override (confirmed via
# signature/type inspection of the installed anthropic/openai/google-genai packages).
MODEL_LIMITS_FETCH_TIMEOUT_SECONDS = 3.0


@dataclass
class ModelLimits:
    max_input_tokens: int | None
    max_output_tokens: int | None


def fetch_model_limits(provider: str, client, model: str) -> ModelLimits:
    """Best-effort lookup of a model's real context window / output cap from the provider's
    own models endpoint. Never raises -- any failure (unsupported endpoint, unknown model,
    network error, or timeout) just means the caller falls back to its own hardcoded defaults."""
    # The user's declared window outranks everything below, for the same reason it outranks the
    # table: they know what their endpoint serves, and neither a hardcoded entry nor a metadata field
    # is more authoritative than that. Checked first so this path cannot return a smaller number.
    from .model_profiles import context_window_override

    declared = context_window_override()
    if declared:
        return ModelLimits(max_input_tokens=declared, max_output_tokens=None)

    if provider == PROVIDER_DEEPSEEK and model in DEEPSEEK_MODELS:
        # Short-circuits BEFORE the network call, deliberately -- confirmed live twice that
        # DeepSeek's /models endpoint takes 12-15s for reasons unrelated to network latency (a
        # raw httpx request to the same endpoint completes in under 3s), which is well past
        # _fetch_model_limits_bounded's 3s hard ceiling in cli.py. Calling the API here first and
        # only falling back afterward was USELESS in production: the bounded wrapper had already
        # given up and discarded this function's eventual return value long before it arrived.
        # DeepSeek never publishes this via its API anyway (no context_length field on /models,
        # /models/{id} 404s) -- there's nothing the network call could add for these two models,
        # so skip it and return the hardcoded value immediately.
        return ModelLimits(max_input_tokens=DEEPSEEK_MAX_INPUT_TOKENS, max_output_tokens=None)

    # Same reasoning, generalised: if the model's context window is already known from the local
    # table, the lookup cannot add anything and costs a network round trip on every single launch.
    # Measured on Vertex: `client.models.get` took 33.8 SECONDS and returned None for both limits --
    # the Gemini path never wrapped the call in `with_options(max_retries=0, ...)` the way the
    # OpenAI path below does, so the nominal 3s ceiling was compounded by the SDK's own retry and
    # connection machinery. Measured again on Netra: `models.list()` with a VALID key took 31.9s
    # (a bad key is fast, because it fails before the listing is generated), so this was costing a
    # second ~32s on every startup. Both are pure waste when the window is already known.
    from .model_profiles import KNOWN_WINDOWS

    key = (model or "").lower()
    for pattern in sorted(KNOWN_WINDOWS, key=len, reverse=True):
        if pattern in key:
            return ModelLimits(max_input_tokens=KNOWN_WINDOWS[pattern], max_output_tokens=None)

    return _fetch_model_limits_from_api(provider, client, model)


def _fetch_model_limits_from_api(provider: str, client, model: str) -> ModelLimits:
    try:
        if provider == PROVIDER_GEMINI:
            from google.genai.types import GetModelConfig, HttpOptions

            info = client.models.get(
                model=model,
                config=GetModelConfig(
                    http_options=HttpOptions(timeout=int(MODEL_LIMITS_FETCH_TIMEOUT_SECONDS * 1000))
                ),
            )
            return ModelLimits(
                max_input_tokens=getattr(info, "input_token_limit", None),
                max_output_tokens=getattr(info, "output_token_limit", None),
            )

        # timeout alone isn't enough -- confirmed live that the openai SDK's default
        # max_retries=2 meant a single slow/unresponsive endpoint still cost ~3x the timeout
        # (observed 15s against a nominal 3s timeout). with_options(max_retries=0, ...) disables
        # that retry compounding for this specific best-effort, safe-to-just-skip call.
        scoped_client = client.with_options(max_retries=0, timeout=MODEL_LIMITS_FETCH_TIMEOUT_SECONDS)

        if provider == PROVIDER_ANTHROPIC:
            info = scoped_client.models.retrieve(model)
            return ModelLimits(
                max_input_tokens=getattr(info, "max_input_tokens", None),
                max_output_tokens=getattr(info, "max_tokens", None),
            )

        # OpenAI-compat (OpenRouter, DeepSeek, etc.): confirmed live via raw HTTP against both
        # real APIs that GET /models/{id} (what client.models.retrieve(model) calls) 404s on
        # BOTH OpenRouter and DeepSeek -- neither actually implements a single-model retrieve
        # endpoint, even though the openai SDK exposes .retrieve() generically for any
        # OpenAI-compatible base_url. This function used to call .retrieve() here and silently
        # swallow the resulting 404 as "no limits available" for every openai-compat provider,
        # including OpenRouter, which DOES publish context_length -- just only on the LIST
        # endpoint. DeepSeek's list entries are confirmed minimal (id/object/owned_by only, no
        # context_length anywhere) -- it genuinely never publishes this, so the loop below
        # correctly falls through to (None, None) for DeepSeek, not a bug in this code.
        for entry in scoped_client.models.list():
            if getattr(entry, "id", None) != model:
                continue
            top_provider = getattr(entry, "top_provider", None) or {}
            max_output = top_provider.get("max_completion_tokens") if isinstance(top_provider, dict) else None
            max_input = (
                (top_provider.get("context_length") if isinstance(top_provider, dict) else None)
                or getattr(entry, "context_length", None)
            )
            return ModelLimits(max_input_tokens=max_input, max_output_tokens=max_output)
        return ModelLimits(None, None)
    except Exception:
        return ModelLimits(None, None)


def list_models(provider: str, client) -> list[dict]:
    """Best-effort listing of models available to this client/key, for the /models command.
    Never raises -- same discipline as fetch_model_limits. Returns a uniform
    [{"id": str, "display_name": str | None}, ...] regardless of provider."""
    try:
        if provider == PROVIDER_GEMINI:
            # Nano Banana is Gemini's image-generation family, not a text/coding model --
            # excluded here. Confirmed live that `supported_actions` doesn't distinguish it from
            # text models (both list "generateContent"), and that the "Nano Banana" branding
            # only appears in display_name (e.g. "gemini-2.5-flash-image" -> "Nano Banana") --
            # filtering on the machine `name`/id instead would miss nearly all of them.
            return [
                {"id": getattr(m, "name", None), "display_name": getattr(m, "display_name", None)}
                for m in client.models.list()
                if "nano banana" not in (getattr(m, "display_name", None) or "").lower()
            ]

        models = list(client.models.list())
    except Exception:
        return []

    if provider == PROVIDER_ANTHROPIC:
        return [{"id": m.id, "display_name": getattr(m, "display_name", None)} for m in models]

    # OpenAI-compat's base Model schema has no display_name -- id is the only guaranteed field.
    return [{"id": m.id, "display_name": None} for m in models]
