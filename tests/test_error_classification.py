"""What the user is told when a turn fails, for the HTTP client every OpenAI-compatible provider uses.

The built-in HTTP client (`direct_http_client`) is the default for DeepSeek, OpenRouter, Netra, any OpenAI-compatible
endpoint and Ollama: it is used unless the `openai` SDK is asked for, and the SDK is not installed. Its errors were never
part of the CLI's classification, which looked only at the SDK's exception classes. So with the SDK absent a refused key,
a rate limit, a server error and a server that is not running ALL ended as "Unexpected error -- details logged to ...",
which names neither the cause nor the fix. These tests pin the classification to the classes the client really raises.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from myriaxon import cli  # noqa: E402
from myriaxon.direct_http_client import DirectConnectionError, api_error  # noqa: E402


def message_for(exc, provider="deepseek"):
    cli._active_provider_module = provider
    return cli._classify_run_error(exc, json_mode=True, console=None, logger=logging.getLogger("test"))


def test_a_refused_key_is_an_authentication_error():
    for status in (401, 403):
        error = api_error(status, "Invalid API key")
        assert isinstance(error, cli._auth_errors())
        assert "myriaxon --reauth" in message_for(error)


def test_a_429_is_a_rate_limit_not_an_unexpected_error():
    error = api_error(429, "Too many requests")
    assert isinstance(error, cli._rate_limit_errors())
    assert not isinstance(error, cli._auth_errors())
    assert message_for(error).startswith("Rate limited")


def test_other_statuses_keep_their_code_and_message():
    error = api_error(500, "upstream exploded")
    assert isinstance(error, cli._status_errors())
    assert not isinstance(error, cli._auth_errors() + cli._rate_limit_errors())
    assert message_for(error) == "API error (500): upstream exploded"


def test_a_server_that_is_not_running_is_a_connection_error():
    error = DirectConnectionError("ConnectionRefusedError: [WinError 10061] actively refused")
    assert isinstance(error, cli._connection_errors())
    text = message_for(error)
    assert text.startswith("Could not reach the API endpoint") and "Unexpected error" not in text


def test_the_ollama_hints_appear_only_for_ollama():
    refused = DirectConnectionError("refused")
    assert "Is Ollama running?" in message_for(refused, provider="ollama")
    assert "Is Ollama running?" not in message_for(refused, provider="deepseek")
    memory = api_error(500, "model requires more system memory (15.1 GiB) than is available (9.4 GiB)")
    assert "myriaxon ollama --create --ctx" in message_for(memory, provider="ollama")
    assert "myriaxon ollama" not in message_for(memory, provider="deepseek")


@pytest.mark.parametrize("provider", ["ollama", "deepseek", "openrouter", "netra", "openai_compat"])
def test_the_anthropic_sdk_is_not_loaded_to_classify_an_openai_compatible_error(provider, monkeypatch):
    """Importing the anthropic SDK costs tens of seconds on a machine with on-access file scanning; a failed local request must not pay it."""
    called = []
    monkeypatch.setattr(cli, "_get_anthropic", lambda: called.append("anthropic"))
    monkeypatch.setattr(cli, "_get_openai", lambda: None)
    cli._active_provider_module = provider
    cli._active_error_modules()
    assert called == []
