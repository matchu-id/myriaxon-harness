"""Cost accounting.

IQB-14 reports dollars per task and dollars per solved task alongside tokens, because tokens are
not comparable across models and money is. Prices are declared here, dated, and overridable from
the run manifest -- a benchmark that silently uses stale prices produces confident wrong numbers,
so every report carries the price table's date and the report warns when it is old.
"""
from __future__ import annotations

PRICE_TABLE_DATE = "2026-02-01"

# USD per 1,000,000 tokens: (input, output). Matching is by substring on the model id, longest
# key first, so "openai/gpt-oss-120b" hits before a bare "gpt-oss".
PRICES: dict[str, tuple[float, float]] = {
    "deepseek-v4-flash": (0.28, 0.42),
    "deepseek": (0.28, 0.42),
    "claude-opus-5": (15.0, 75.0),
    "claude-sonnet": (3.0, 15.0),
    "claude-haiku": (0.80, 4.0),
    "gemini-3.8-flash": (0.30, 2.50),
    "gemini-3.7-flash": (0.30, 2.50),
    "gemini": (0.30, 2.50),
    "openai/gpt-oss-120b": (0.15, 0.60),
    "gpt-oss": (0.15, 0.60),
    "nvidia/nemotron": (0.0, 0.0),
    "inclusionai/ling": (0.0, 0.0),
}


def price_for(model: str) -> tuple[float, float] | None:
    if not model:
        return None
    key = model.lower()
    for pattern in sorted(PRICES, key=len, reverse=True):
        if pattern in key:
            return PRICES[pattern]
    return None


def estimate_cost_usd(model: str, input_tokens: int, output_tokens: int) -> float | None:
    """None -- never 0.0 -- when the model has no declared price.

    Returning 0.0 for an unknown model would make an unmeasured cost look like a free one, and
    "cheapest" is exactly the kind of claim a benchmark gets asked for.
    """
    price = price_for(model)
    if price is None:
        return None
    in_rate, out_rate = price
    return (input_tokens / 1_000_000.0) * in_rate + (output_tokens / 1_000_000.0) * out_rate
