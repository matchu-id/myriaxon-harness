"""Cache accounting must read every provider's shape, and Gemini's was missing.

Measured before this fix, against the Vertex project Myriaxon now uses:

    cache_fields(GeminiUsage(cached_content_token_count=900)) -> (0, 0)

`cachedContents` responds 200 there, so context caching is a real available feature that was being
reported as a permanent 0% cache-hit rate. A number that is always zero for a working feature is worse
than no number: it reads as "caching does not help here" and steers a user away from a real saving.

Each provider is pinned by a separate case, because the four shapes disagree and a change that fixes one
by breaking another would otherwise pass unnoticed.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from myriaxon.usage import cache_fields  # noqa: E402


class AnthropicUsage:
    cache_read_input_tokens = 1200
    cache_creation_input_tokens = 300


class OpenAIUsage:
    class prompt_tokens_details:
        cached_tokens = 800


class GeminiSdkUsage:
    """google-genai exposes snake_case attributes."""
    cached_content_token_count = 900
    prompt_token_count = 1000


class PlainUsage:
    prompt_tokens = 10
    completion_tokens = 2


def test_anthropic_shape():
    assert cache_fields(AnthropicUsage()) == (1200, 300)


def test_openai_shape():
    assert cache_fields(OpenAIUsage()) == (800, 0)


def test_gemini_snake_case():
    """The bug this file exists for."""
    read, write = cache_fields(GeminiSdkUsage())
    assert read == 900, f"a real Gemini cache hit reads as {read}, i.e. 0%"
    assert write == 0, "Gemini has no write counter; inventing one would be a fabricated number"


def test_gemini_rest_camel_case():
    """A dict straight off the wire, for transports that do not build SDK objects."""
    assert cache_fields({"cachedContentTokenCount": 700, "promptTokenCount": 900}) == (700, 0)
    assert cache_fields({"cached_content_token_count": 650}) == (650, 0)


def test_a_provider_without_cache_fields_reports_zero_not_an_error():
    """A missing field means 'not reported', and it must not raise."""
    assert cache_fields(PlainUsage()) == (0, 0)
    assert cache_fields(object()) == (0, 0)
    assert cache_fields(None) == (0, 0)


def test_a_bad_value_does_not_raise():
    """A provider sending a string or null must degrade to 0, not take the turn down."""
    class Weird:
        cached_content_token_count = "not a number"

    assert cache_fields(Weird()) == (0, 0)

    class Nullish:
        cached_content_token_count = None

    assert cache_fields(Nullish()) == (0, 0)
