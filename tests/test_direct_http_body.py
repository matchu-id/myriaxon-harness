"""The `json_body` keyword was silently dropped, so chat completions sent an empty body.

Tests for a bug class that has now occurred TWICE in `direct_http_client.py`, both times surviving because a
permissive `**kwargs` accepted a plausible-looking keyword and threw it away:

  1. `get()` called `client.post(...)` while its own docstring said "the one call that is not a POST", so
     `GET /models` was sent as `POST /models`. `model_info.list_models` catches every exception and returns
     `[]`, which the UI renders as "unsupported or request failed" -- so `/models` reported an unsupported
     provider for Netra, DeepSeek, OpenRouter, OpenAI and Ollama alike, while the catalogue was one correct
     verb away. Fixed and recorded on `get()`.
  2. `_complete_chat()` called `self._http.post(url, ..., json_body=body)`, and `_Client.post`'s signature
     took only `content`. `json_body` fell into `**kwargs`, the body stayed `None`, and an EMPTY POST went out.

The second one was found by testing against the LIVE Netra gateway, and the pair of measurements is the whole
evidence:

    DirectOpenAI.chat.completions.create(...)  ->  400 "request body must be a JSON object"
    the same payload via _Client.request(...)  ->  200, "PONG"

After the fix, the public client returns `PONG` both streaming and non-streaming.
"""
from __future__ import annotations

import inspect
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from myriaxon import direct_http_client as http  # noqa: E402


class TestTheKeywordIsAccepted:
    def test_post_takes_json_body_by_name(self):
        """Named, not swallowed. `**kwargs` accepting it is exactly how the bug survived."""
        parameters = inspect.signature(http._Client.post).parameters
        assert "json_body" in parameters, (
            "`json_body` must be an explicit parameter: if it falls into **kwargs the body is silently "
            "dropped, which is how an empty POST was sent to Netra"
        )

    def test_a_json_body_is_serialised_and_sent(self, monkeypatch):
        """The behaviour the fix exists for: a body actually reaches the wire."""
        sent = {}

        class FakeConnection:
            status = 200

            def getheaders(self):
                return []

        def fake_send(self, method, url, headers, body):  # noqa: ANN001
            sent["method"] = method
            sent["url"] = url
            sent["headers"] = dict(headers)
            sent["body"] = body
            return FakeConnection(), None

        monkeypatch.setattr(http._Client, "_send", fake_send)
        client = http._Client()
        client.post("https://example.invalid/v1/chat/completions", headers={"Content-Type": "application/json"},
                    json_body={"model": "m", "messages": [{"role": "user", "content": "hi"}]})

        assert sent["method"] == "POST"
        assert sent["body"] is not None, "an empty body is the bug"
        payload = json.loads(sent["body"].decode("utf-8"))
        assert payload["model"] == "m"
        assert payload["messages"][0]["content"] == "hi"

    def test_content_length_matches_the_encoded_body(self, monkeypatch):
        sent = {}

        class FakeConnection:
            status = 200

            def getheaders(self):
                return []

        def fake_send(self, method, url, headers, body):  # noqa: ANN001
            sent["headers"] = dict(headers)
            sent["body"] = body
            return FakeConnection(), None

        monkeypatch.setattr(http._Client, "_send", fake_send)
        http._Client().post("https://example.invalid/x", json_body={"a": "b"})
        assert int(sent["headers"]["Content-Length"]) == len(sent["body"])

    def test_raw_content_still_works(self, monkeypatch):
        """The path web_tools uses. The fix must not break the other calling convention."""
        sent = {}

        class FakeConnection:
            status = 200

            def getheaders(self):
                return []

        def fake_send(self, method, url, headers, body):  # noqa: ANN001
            sent["body"] = body
            return FakeConnection(), None

        monkeypatch.setattr(http._Client, "_send", fake_send)
        http._Client().post("https://example.invalid/x", content="raw text")
        assert sent["body"] == b"raw text"

    def test_a_string_content_is_encoded(self, monkeypatch):
        sent = {}

        class FakeConnection:
            status = 200

            def getheaders(self):
                return []

        def fake_send(self, method, url, headers, body):  # noqa: ANN001
            sent["body"] = body
            return FakeConnection(), None

        monkeypatch.setattr(http._Client, "_send", fake_send)
        http._Client().post("https://example.invalid/x", content="Ã¼nÃ¯code")
        assert isinstance(sent["body"], bytes)


class TestTheCallSiteSendsABody:
    def test_complete_chat_passes_json_body(self):
        """The actual regression: this call site is what sent an empty body."""
        source = inspect.getsource(http.DirectOpenAI._complete_chat)
        assert "json_body=body" in source

    def test_complete_chat_does_not_use_a_dropping_keyword(self):
        """Any other spelling would be swallowed again."""
        source = inspect.getsource(http.DirectOpenAI._complete_chat)
        assert "json=body" not in source, "`json=` is not a parameter either"

    def test_a_completion_reaches_the_network_with_a_body(self, monkeypatch):
        """End to end through the public client, with the transport stubbed."""
        captured = {}

        class FakeResponse:
            status_code = 200

            def __init__(self):
                self.text = json.dumps({
                    "choices": [{"message": {"role": "assistant", "content": "PONG"}, "index": 0}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                })

            def json(self):
                return json.loads(self.text)

        def fake_request(self, method, url, *, headers=None, json_body=None, **kwargs):  # noqa: ANN001
            captured["json_body"] = json_body
            captured["url"] = url
            return FakeResponse()

        monkeypatch.setattr(http._Client, "request", fake_request)
        client = http.DirectOpenAI(api_key="k", base_url="https://example.invalid/v1")
        completion = client.chat.completions.create(
            model="m", messages=[{"role": "user", "content": "hi"}], max_tokens=8)

        assert captured["json_body"] is not None, "an empty body is the bug this file exists for"
        assert captured["json_body"]["model"] == "m"
        assert captured["url"].endswith("/chat/completions")
        assert completion.choices[0].message.content == "PONG"


class TestTheSiblingBugIsStillFixed:
    def test_get_sends_a_GET(self):
        """The first instance of this class. A regression here is silent: list_models returns []."""
        source = inspect.getsource(http.get)
        assert '"GET"' in source or "'GET'" in source


class TestTheLiveMeasurements:
    """Recorded so the next reader knows this was found against a real gateway, not a mock."""

    def test_the_module_records_both_measurements(self):
        module_doc = http.__doc__ or ""
        client_source = inspect.getsource(http)
        # The /models instance is documented on `get`.
        assert "405" in client_source or "method not allowed" in inspect.getsource(http.get)
        # And the empty-body instance is documented on `post`.
        assert "request body must be a JSON object" in client_source, (
            "the live error that exposed the empty body should be recorded where the fix is"
        )
