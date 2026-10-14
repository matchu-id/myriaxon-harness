"""MCP tool output must be redacted BEFORE it can reach the model.

Found by an audit: `mcp_client.call_tool` returned the server's output verbatim, and `redaction` ran
only when RECORDING a session or exporting a bundle. So a connector that echoed a credential put it
into the live transcript -- and therefore into every later request sent upstream, where no local
redaction can reach it.

The measured detail that shaped the fix: `redaction.redact` is SHAPE based, and a generic gateway
token matches no shape at all. A 68-character `sk_live_...` token is caught only because it happens to
look like a Stripe key; a plain 68-character token produced zero findings. Shape matching alone would
therefore have missed the exact credential this harness runs on, which is why the boundary also
replaces the VALUES the harness holds.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import pytest  # noqa: E402

from myriaxon import mcp_client, redaction  # noqa: E402

# A token that matches NO shape pattern -- the case that motivated the value-based pass.
SHAPELESS = "Zq7" * 22 + "w"          # 67 characters, no recognisable prefix
NETRA_LIKE = "sk_live_" + "a1b2c3d4" * 7   # looks like a Stripe key by accident
DEEPSEEK_LIKE = "sk-" + "0f1e2d3c" * 4


@pytest.fixture()
def bridge(tmp_path, monkeypatch):
    """A bridge whose credential store is a real file on disk, so the lookup is exercised."""
    store = tmp_path / "mcp_servers.json"
    token = "ghp_" + "A" * 36
    store.write_text(
        json.dumps(
            {
                "stdio_servers": [
                    {"name": "fs", "command": "x", "env": {"SERVICE_API_KEY": SHAPELESS}},
                ],
                "url_servers": [
                    {"name": "remote", "url": "https://example.invalid", "token": token},
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(mcp_client, "MCP_SERVERS_FILE", store, raising=False)
    monkeypatch.setattr("myriaxon.constants.MCP_SERVERS_FILE", store, raising=False)

    made = mcp_client.McpBridge.__new__(mcp_client.McpBridge)   # no __init__: no servers to connect
    made._secret_cache = None
    return made


def test_a_shapeless_stored_token_is_redacted(bridge):
    """THE regression. This token matches no shape pattern, so only the known-value pass catches it."""
    out = bridge._redact(f"the key is {SHAPELESS} and that is all")
    assert SHAPELESS not in out
    assert "[redacted" in out


def test_a_stored_url_token_is_redacted(bridge):
    out = bridge._redact(f"Authorization: Bearer {'ghp_' + 'A' * 36}")
    assert "A" * 36 not in out


def test_credential_shapes_are_still_caught_without_being_stored(bridge):
    """A credential the harness has never seen must still be caught, or the value pass is the only
    defence and any new token shape leaks until someone adds it to the store."""
    out = bridge._redact(f"found AKIA{'B' * 16} in the config")
    assert "AKIA" not in out


def test_ordinary_output_survives_untouched(bridge):
    """Redaction that mangles normal output would be turned off by the first person it annoys."""
    text = "3 files matched:\n  a.py\n  b.py\n  c.py"
    assert bridge._redact(text) == text


def test_a_short_value_is_not_treated_as_a_secret(bridge, tmp_path, monkeypatch):
    """A one- or two-character env value must not become a blanket replacement."""
    store = tmp_path / "short.json"
    store.write_text(
        json.dumps({"stdio_servers": [{"name": "s", "command": "x", "env": {"SERVICE_API_KEY": "ab"}}]}),
        encoding="utf-8",
    )
    monkeypatch.setattr("myriaxon.constants.MCP_SERVERS_FILE", store, raising=False)
    bridge._secret_cache = None
    # "ab" appears inside ordinary words; replacing it would destroy the output.
    assert bridge._redact("about absolute labels") == "about absolute labels"


def test_redaction_never_raises_on_a_broken_store(tmp_path, monkeypatch):
    """A malformed or missing store must not break a tool call -- redaction is a safety net, not a
    dependency."""
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr("myriaxon.constants.MCP_SERVERS_FILE", broken, raising=False)
    made = mcp_client.McpBridge.__new__(mcp_client.McpBridge)
    made._secret_cache = None
    assert made._known_secrets() == []
    assert made._redact("hello") == "hello"


def test_empty_output_is_passed_through(bridge):
    assert bridge._redact("") == ""


def test_call_tool_actually_wires_the_redaction_in():
    """The regression guard for the WIRING, not the helper.

    Every other test here calls `_redact` directly, so they would all still pass if `call_tool`
    stopped calling it -- which is precisely the bug that was found: the helper existed, and the
    boundary did not use it. Asserting on the source is crude, and it is the only way to catch
    "correct code, not called" from a unit test.
    """
    import inspect

    source = inspect.getsource(mcp_client.McpBridge.call_tool)
    assert "_redact(" in source, "call_tool returns MCP output without redacting it"
    # And the redaction must happen before the value is returned, not after.
    assert source.index("_redact(") < source.index("return content"), (
        "the redaction is unreachable code after the return"
    )
