"""Model profiles: how Myriaxon adapts to the model it is actually talking to.

The premise is that a harness should not hand a 32k-window model the same instructions, the same
tool results and the same delegation powers it hands a 200k-window frontier model. Every difference
here exists because of a specific, observable failure mode of small or weak models:

* **Prompt verbosity.** Long instructions get ignored or half-followed by small models, and the
  system prompt is charged against their window on every single request. Verbosity tiers exist so
  the prompt can be shortened without losing the rules that actually prevent failures.
* **Tool-result budget.** A weak model cannot recover from a 40k-character directory listing
  dominating its window; it stops reasoning about the task. Results are truncated at the source and
  the truncation is *stated in the result text* so the model knows to narrow its query instead of
  assuming it saw everything.
* **Delegation.** `task`/`parallel_tasks` are the single biggest context sink available, and a weak
  model uses them badly (spawning a sub-agent to do something it could have done in one call).
  Profiles can disable them; the default single-agent mode does.
* **Per-request output ceiling.** A model that streams 40k tokens of rambling burns the user's
  window and money; profiles cap it independently of the provider's technical maximum.

Resolution order (documented because a benchmark result is only meaningful if you know which
profile produced it): explicit `--profile` > `--max-input-tokens` override > auto-detection from the
model id > the conservative default. Auto-detection reports its confidence, and an unrecognized
model is treated as *small*, not as frontier -- guessing generously is how a harness silently
overflows someone's window.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

Verbosity = Literal["terse", "normal", "full"]
AgentMode = Literal["single", "multi"]

# Context windows used when a model cannot be resolved. Kept deliberately small: under-estimating
# the window costs a little extra compaction, over-estimating it costs a hard API failure in the
# middle of the user's task with no recovery.
FALLBACK_CONTEXT_WINDOW = 32_000

# Rough character-per-token ratios by content kind. Latin prose/code measures close to 3.6-4.0
# chars/token on the tokenizers used by all three providers; CJK is far denser (about 1.5-2.0).
CHARS_PER_TOKEN_TEXT = 3.7
CHARS_PER_TOKEN_CJK = 1.7

KNOWN_WINDOWS: dict[str, int] = {
    # DeepSeek (see constants.DEEPSEEK_MAX_INPUT_TOKENS for the live-unverifiable caveat)
    "deepseek-v4-flash": 1_000_000,
    "deepseek-v4-pro": 1_000_000,
    "deepseek": 1_000_000,
    # Anthropic
    "claude-opus": 200_000,
    "claude-sonnet": 200_000,
    "claude-haiku": 200_000,
    "claude": 200_000,
    # Google
    "gemini": 1_000_000,
    # OpenAI
    "gpt-4o": 128_000,
    "gpt-4.1": 1_000_000,
    "o3": 200_000,
    "o4": 200_000,
    # Open-weights models commonly served through OpenRouter / local vLLM / Ollama. These are the
    # ones people actually run when they say "a small model", so they get realistic small windows.
    "gpt-oss-120b": 131_072,
    "gpt-oss-20b": 131_072,
    "llama-3.3-70b": 128_000,
    "llama-3.1-8b": 128_000,
    "llama-3.2-3b": 128_000,
    "llama-3.2-1b": 128_000,
    "qwen2.5-coder-7b": 32_768,
    "qwen2.5-7b": 32_768,
    "qwen2.5-3b": 32_768,
    "qwen3": 40_960,
    # Netra (netraruntime) gateway, measured directly rather than assumed. deepseek-v4.1-flash
    # answered in ~2.1s median with 1.4s time-to-first-token and handled tool calls correctly, so it
    # gets a generous-but-honest window instead of the conservative fallback an unknown id would get.
    "deepseek/deepseek-v4.1-flash": 128_000,
    "deepseek/deepseek-v4-flash-0731": 128_000,
    "deepseek/deepseek": 128_000,
    "mistral-7b": 32_768,
    "mistral-nemo": 128_000,
    "gemma-2-9b": 8_192,
    "gemma-2-2b": 8_192,
    "phi-3": 4_096,
    "phi-4": 16_384,
    "tinyllama": 2_048,
    "nemotron": 128_000,
    "ling-": 32_768,
    "glm-4": 128_000,
    "kimi": 128_000,
}

# Model families that are known to handle a long system prompt and heavy tool use well.
STRONG_HINTS = (
    "claude-opus",
    "claude-sonnet",
    "gpt-4.1",
    "gpt-4o",
    "o3",
    "o4",
    "deepseek-v4-pro",
    "gemini-2.5-pro",
    "gemini-3",
    "gpt-oss-120b",
    "llama-3.3-70b",
    "qwen3-235b",
    "glm-4",
    "kimi",
    "nemotron",
)
WEAK_HINTS = (
    "1b",
    "2b",
    "3b",
    "7b",
    "8b",
    "9b",
    "mini",
    "small",
    "tiny",
    "phi-3",
    "gemma-2-2b",
    "gemma-2-9b",
    "flash-lite",
    "ling-",
)


def _profile_from_window(window: int) -> ModelProfile:
    """Bucket by usable window.

    Note the asymmetry: an unrecognized model with an *assumed* window is bucketed by that assumed
    window, but `detect_profile` downgrades the result, because a guess that happens to land in the
    "medium" bucket is still a guess. An explicitly declared window (--max-input-tokens) is trusted
    as-is.
    """
    if window <= 16_000:
        return PROFILES["small"]
    if window <= 100_000:
        return PROFILES["balanced"]
    return PROFILES["large"]


def _downgrade(profile: ModelProfile) -> ModelProfile:
    """Shift one tier down. Used when nothing about the model is actually known.

    Guessing generously is how a harness silently overflows someone's window: an unknown model id
    treated as frontier gets big tool results and late compaction, and fails in the middle of the
    user's task. Guessing small costs a little extra compaction and is recoverable.
    """
    order = ["small", "balanced", "large"]
    try:
        index = order.index(profile.name)
    except ValueError:  # pragma: no cover - named profiles are always in the order list
        return profile
    return PROFILES[order[max(0, index - 1)]]


@dataclass(frozen=True)
class ModelProfile:
    """Everything the harness needs to know to behave sanely with one model."""

    name: str
    context_window: int
    max_output_tokens: int
    verbosity: Verbosity
    # Characters of a single tool result before it is truncated, and the hard ceiling regardless of
    # profile (guards against a tool returning a multi-megabyte blob).
    tool_result_chars: int
    tool_result_hard_cap: int
    # Whether `task`/`parallel_tasks` are offered at all.
    allow_delegation: bool
    # How much of the window may be filled before action is taken.
    warn_at: float
    prune_at: float
    compact_at: float
    refuse_at: float
    # Whether the model reliably emits well-formed tool arguments. Weak models get corrective
    # feedback with the exact expected shape instead of a bare validation error.
    needs_argument_coaching: bool
    # System-prompt cap in characters. Enforced by dropping the least critical prompt sections.
    system_prompt_chars: int
    # Tools withheld from this profile. Tool *schemas* are charged on every request, and measured
    # against the real seed prompt they are the single largest fixed cost: 2,163 of the 4,341 tokens
    # a small-profile session spends before its first word. Every entry here is a tool a weak model
    # demonstrably uses badly or not at all.
    disabled_tools: tuple[str, ...] = field(default_factory=tuple)
    notes: str = ""
    detection: str = "assumed"  # "known" | "heuristic" | "assumed"
    tags: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "context_window": self.context_window,
            "max_output_tokens": self.max_output_tokens,
            "verbosity": self.verbosity,
            "tool_result_chars": self.tool_result_chars,
            "tool_result_hard_cap": self.tool_result_hard_cap,
            "allow_delegation": self.allow_delegation,
            "disabled_tools": list(self.disabled_tools),
            "thresholds": {
                "warn_at": self.warn_at,
                "prune_at": self.prune_at,
                "compact_at": self.compact_at,
                "refuse_at": self.refuse_at,
            },
            "needs_argument_coaching": self.needs_argument_coaching,
            "system_prompt_chars": self.system_prompt_chars,
            "detection": self.detection,
            "tags": list(self.tags),
            "notes": self.notes,
        }


# Tools that cost more context (schema) than they return for a model that cannot use them well.
# `save_skill` and `search_memory` are long-horizon conveniences; a small model that has never
# completed a two-step task will not benefit from either, and both are pure schema overhead.
_SMALL_DISABLED = (
    "parallel_tasks",
    "send_to_subagent",
    "save_skill",
    "search_memory",
    "checkpoint_restore",
)
_BALANCED_DISABLED = ("parallel_tasks",)


# Named profiles, selectable with --profile. These are the contract: a run that says
# `profile=small` can be compared against another run that says the same, whatever model produced it.
PROFILES: dict[str, ModelProfile] = {
    "small": ModelProfile(
        name="small",
        context_window=16_000,
        max_output_tokens=2_048,
        verbosity="terse",
        tool_result_chars=1_200,
        tool_result_hard_cap=6_000,
        allow_delegation=False,
        warn_at=0.50,
        prune_at=0.65,
        compact_at=0.80,
        refuse_at=0.95,
        needs_argument_coaching=True,
        system_prompt_chars=2_400,
        disabled_tools=_SMALL_DISABLED,
        notes="Tiny/weak models: short prompt, small results, no delegation, aggressive compaction.",
        detection="explicit",
        tags=("low-context",),
    ),
    "balanced": ModelProfile(
        name="balanced",
        context_window=64_000,
        max_output_tokens=8_192,
        verbosity="normal",
        tool_result_chars=8_000,
        tool_result_hard_cap=40_000,
        allow_delegation=False,
        warn_at=0.60,
        prune_at=0.75,
        compact_at=0.85,
        refuse_at=0.95,
        needs_argument_coaching=False,
        system_prompt_chars=6_000,
        disabled_tools=_BALANCED_DISABLED,
        notes="Mid-size models (default): full rules, moderate result budget, single-agent.",
        detection="explicit",
        tags=("default",),
    ),
    "large": ModelProfile(
        name="large",
        context_window=200_000,
        max_output_tokens=32_000,
        verbosity="full",
        # 8,000, not 24,000. A tool result is re-sent on every later request in the same turn, so the
        # cost of one large read compounds by the number of remaining steps -- measured, a 24,000-char
        # result costs 6,000 tokens on each of them, and 30,000 tokens over five.
        #
        # The reduction is recoverable rather than lossy: `read_file` takes `offset` and `limit` and
        # returns 1-indexed lines, so the model reads the rest in a second call instead of carrying the
        # whole file through the turn. `balanced` already ran at this exact value and `small` at 1,200,
        # so 8,000 is this harness's own established moderate figure -- 24,000 was the outlier, three
        # times the tier below it with no recorded measurement behind it.
        tool_result_chars=8_000,
        tool_result_hard_cap=120_000,
        allow_delegation=True,
        warn_at=0.70,
        prune_at=0.82,
        compact_at=0.90,
        refuse_at=0.97,
        needs_argument_coaching=False,
        system_prompt_chars=12_000,
        notes="Frontier models: full prompt, large results, delegation permitted.",
        detection="explicit",
        tags=("high-context",),
    ),
}

DEFAULT_PROFILE = "balanced"

# The user's own statement of a model's window, for the case where this table is WRONG for their
# endpoint. It exists because the table cannot be right for every gateway at once: `deepseek` is 1M
# at DeepSeek's own API, while a gateway that fronts it (`netra`) may serve the same weights with a
# smaller window, or a larger one -- and the harness has no way to ask. Without this knob the only
# remedy was editing the source, which is not something a user should have to do to use the window
# they are paying for.
#
# It is applied ABOVE the table (see `_window_for`) and above the live lookup, because it is the only
# statement of intent in the chain. Accepted forms: a plain number, or a number with a k/m suffix
# (`1000000`, `1m`, `200k`). An unreadable value is ignored rather than raising: a typo in an
# environment variable must not be able to stop the harness from starting.
CONTEXT_WINDOW_ENV = "MYRIAXON_CONTEXT_WINDOW"


def _suffix_number(value: str) -> int:
    """`"1m"` -> 1000000, `"200k"` -> 200000, `"32768"` -> 32768, anything else -> 0."""
    text = (value or "").strip().lower().replace("_", "").replace(",", "")
    if not text:
        return 0
    multiplier = 1
    if text.endswith("k"):
        multiplier, text = 1_000, text[:-1]
    elif text.endswith("m"):
        multiplier, text = 1_000_000, text[:-1]
    try:
        number = float(text)
    except ValueError:
        return 0
    if number <= 0:
        return 0
    return int(number * multiplier)


def context_window_override() -> int:
    """The window the user declared for this run, or 0 when they did not."""
    import os

    return _suffix_number(os.environ.get(CONTEXT_WINDOW_ENV, ""))


def _window_for(model: str) -> tuple[int, str]:
    """Longest-matching key wins, checked in order of decreasing length.

    The loop must test every pattern and keep the best match rather than returning on the first hit:
    `deepseek/deepseek-v4.1-flash` contains both `deepseek` (1M) and `deepseek/deepseek-v4.1-flash`
    (128k), and returning the first would silently apply the wrong window to a model whose real limit
    was measured directly.

    `MYRIAXON_CONTEXT_WINDOW` wins over the table. The table is a set of measured facts about
    ENDPOINTS, and an endpoint can change what it serves without this file knowing -- so the user's
    explicit statement has to outrank it, or the only fix is editing source. Reported as its own
    source ("declared") so a profile is still explainable from the value alone.
    """
    declared = context_window_override()
    if declared:
        return declared, "declared"
    key = (model or "").lower()
    for pattern in sorted(KNOWN_WINDOWS, key=len, reverse=True):
        if pattern in key:
            return KNOWN_WINDOWS[pattern], "known"
    return FALLBACK_CONTEXT_WINDOW, "assumed"


# Models named by parameter count that are known to be usable at that size. Matched as whole
# tokens against the id, so "llama-3.3-70b" is not weak while "llama-3.2-1b" is.
_WEAK_SIZE_RE = re.compile(r"(?<![0-9.])(1b|2b|3b|4b|7b|8b|9b)(?![0-9])", re.IGNORECASE)
# Named small/weak families. Kept as explicit strings and deliberately *not* including bare "flash"
# or "mini": a substring rule broad enough to catch every small model also catches strong ones
# (confirmed: a bare "flash" hint classified gemini-3.8-flash as small, which would have withheld
# tools and truncated results for a frontier model -- a far more expensive error than missing a weak
# model, which only means it keeps the normal profile).
_WEAK_NAMES = (
    "flash-lite",
    "gpt-oss-20b",
    "phi-3",
    "phi-4-mini",
    "gemma-2-2b",
    "gemma-2-9b",
    "gemma-3-1b",
    "gemma-3-4b",
    "tinyllama",
    "smollm",
    "qwen2.5-0.5b",
    "qwen2.5-1.5b",
    "qwen2.5-3b",
    "ling-",
    "nemotron-nano",
)


def _is_weak_name(model: str) -> bool:
    """True when the model is *known* to be small, from its parameter count or its family name.

    A 1B-parameter model with a 128k window is still a 1B model: it cannot follow a long prompt or
    reason about a 20k-character tool result, and treating it as `large` solely because its window is
    wide hands it more than it can use. Precision is prioritised over recall here -- a false positive
    disables tools and truncates results for a capable model, while a false negative only means an
    unknown small model keeps the balanced profile.
    """
    key = (model or "").lower()
    if _WEAK_SIZE_RE.search(key):
        return True
    return any(name in key for name in _WEAK_NAMES)


def detect_profile(
    model: str,
    *,
    explicit: str | None = None,
    max_input_tokens: int | None = None,
    agent_mode: AgentMode = "single",
    provider: str = "",
    base: str | None = None,
    detection: str | None = None,
) -> ModelProfile:
    """Pick the profile for a model.

    `base` forces the starting tier (used when a profile was already chosen from the model id and a
    later, authoritative window lookup arrives). `agent_mode="single"` -- the default -- strips
    delegation from whatever profile is chosen, because the harness's stated goal is one strong
    agent: a weak model delegating to another weak model with less context is strictly worse than
    not delegating at all.
    """
    if explicit and explicit in PROFILES:
        base_profile = PROFILES[explicit]
    elif base and base in PROFILES:
        base_profile = PROFILES[base]
    elif max_input_tokens:
        base_profile = _profile_from_window(max_input_tokens)
        if _is_weak_name(model):
            base_profile = _downgrade(base_profile)
    else:
        window, window_detection = _window_for(model)
        base_profile = _profile_from_window(window)
        if _is_weak_name(model):
            base_profile = PROFILES["small"]
        elif window_detection == "assumed":
            # Unrecognized model: bucket by the assumed window, then err one tier smaller.
            #
            # Only "assumed" is downgraded. This used to read `!= "known"`, which punished a window
            # the USER had declared exactly as if the harness had guessed it -- so `MYRIAXON_CONTEXT_WINDOW=1m`
            # produced the balanced tier and a 64,000-token budget, silently giving the user less than
            # the 128,000 they had without any override at all. A declaration is the strongest
            # statement in the chain, not a guess.
            base_profile = _downgrade(base_profile)
        base_profile = _replace(base_profile, detection=window_detection)
        # A trusted window also replaces the tier's nominal one. Without this the profile kept
        # `balanced`'s 64,000 or `large`'s 200,000 while the declared number lived only in
        # fetch_model_limits -- so the reported window and the one the budget used could disagree.
        if window_detection in ("known", "declared"):
            base_profile = _replace(base_profile, context_window=window)
    base = base_profile

    if agent_mode == "single":
        base = _replace(base, allow_delegation=False)

    # A resolved window always beats the profile's nominal one: the real number is what protects the
    # user. Two cases, deliberately different:
    #
    #  * tier chosen by window (`base=None`, `explicit=None`) -- the window already picked the tier,
    #    so only the nominal value needs correcting.
    #  * tier chosen by name (`--profile small`) -- the user asked for small-model *behavior* and
    #    keeps its thresholds, prompt size and argument coaching. Only the sizes that are literally
    #    derived from the window are rescaled, because handing a 512k-window model a 1,200-character
    #    result cap just because someone typed `small` would be obeying the letter and defeating the
    #    point.
    if max_input_tokens and max_input_tokens != base.context_window:
        scaled_results = min(max(1_200, max_input_tokens // 8), 24_000)
        scaled_cap = min(max(6_000, max_input_tokens // 2), 120_000)
        if explicit:
            base = _replace(base, context_window=max_input_tokens, tool_result_chars=scaled_results,
                            tool_result_hard_cap=scaled_cap)
        else:
            base = _replace(base, context_window=max_input_tokens, tool_result_chars=min(base.tool_result_chars, scaled_results),
                            tool_result_hard_cap=min(base.tool_result_hard_cap, scaled_cap))

    # `detection` records *where the window number came from*, which is a different question from
    # which tier was selected. A live provider lookup is "known"; a hardcoded table match is "known"
    # too but a table assumption is "assumed" -- and a report that cannot tell those apart cannot be
    # trusted about the one number that decides whether the user's request overflows.
    if detection:
        base = _replace(base, detection=detection)
    return base


def _with_detection(profile: ModelProfile, detection: str) -> ModelProfile:
    return _replace(profile, detection=detection)


def _replace(profile: ModelProfile, **changes) -> ModelProfile:
    data = {f: getattr(profile, f) for f in profile.__dataclass_fields__}
    data.update(changes)
    return ModelProfile(**data)


def describe_profile(profile: ModelProfile) -> str:
    delegation = "on" if profile.allow_delegation else "off"
    return (
        f"profile={profile.name} ({profile.detection}) ctx={profile.context_window:,} "
        f"verbosity={profile.verbosity} tool_results<={profile.tool_result_chars:,}ch "
        f"delegation={delegation}"
    )
