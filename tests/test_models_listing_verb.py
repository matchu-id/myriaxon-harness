"""`/models` must actually GET. It used to POST, and failed silently for every OpenAI-compatible provider.

Measured against the live Netra gateway before the fix:

    /models via DirectOpenAI  ->  DirectConnectionError: Error code: 405 - method not allowed
    /models via plain HTTP    ->  200, two models

`model_info.list_models` catches every exception and returns `[]`, which the caller renders as
"Could not list models for this provider (unsupported or request failed)" -- so a wrong HTTP verb
presented as an unsupported provider, and `/models` was useless for Netra, DeepSeek, OpenRouter, OpenAI
and Ollama alike while the catalogue sat one verb away.

The test asserts the VERB rather than the output, because the output depends on a live endpoint and this
bug is about what got sent. A fake transport records the method and returns a plausible body, so the
regression is caught with no network and no credential.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from myriaxon import direct_http_client as dhc  # noqa: E402


class FakeResponse:
    status_code = 200

    def __init__(self, payload: dict) -> None:
        self._payload = payload

    @property
    def text(self) -> str:
        return json.dumps(self._payload)

    def json(self):
        return self._payload

    def raise_for_status(self) -> None:
        return None


class TestTheVerbIsGet:
    def test_get_sends_get_not_post(self, monkeypatch):
        """The whole bug, in one assertion."""
        seen = {}

        def fake_request(self, method, url, *, headers=None, json_body=None, **kwargs):
            seen["method"] = method
            seen["url"] = url
            return FakeResponse({"data": [{"id": "deepseek/deepseek-v4.1-flash"}]})

        monkeypatch.setattr(dhc._Client, "request", fake_request, raising=True)
        response = dhc.get("https://example.invalid/v1/models")
        assert seen["method"] == "GET", f"sent {seen['method']}, which is why a gateway answers 405"
        assert response.json()["data"][0]["id"] == "deepseek/deepseek-v4.1-flash"

    def test_the_executable_line_is_a_get(self):
        """The mismatch was the defect, so both halves are pinned: docstring says GET, code says GET.

        The docstring is stripped before the code is inspected. The first version of this test searched
        the raw source and failed -- because the new docstring EXPLAINS the old bug by name, so the words
        `client.post(` appeared in a comment about the defect rather than in the defect. A check that
        cannot tell an explanation from the thing explained is a check that punishes documentation.
        """
        import ast

        source = (REPO / "myriaxon" / "direct_http_client.py").read_text(encoding="utf-8")
        module = ast.parse(source)
        function = next(
            node for node in module.body
            if isinstance(node, ast.FunctionDef) and node.name == "get"
        )
        # Drop the docstring node, then look only at what actually executes.
        body = [node for node in function.body
                if not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
                        and isinstance(node.value.value, str))]
        executable = "\n".join(ast.unparse(node) for node in body)

        assert "post(" not in executable, f"the GET helper posts again: {executable}"
        assert "request('GET'" in executable or 'request("GET"' in executable, executable

        docstring = ast.get_docstring(function) or ""
        assert "not a POST" in docstring, "the docstring no longer states the intent"

    def test_models_list_returns_entries_not_empty(self, monkeypatch):
        """End to end through the public surface: the listing must not come back empty on a 200."""

        class FakeClient:
            base_url = "https://example.invalid/v1"
            _headers = {"Authorization": "Bearer x"}

        monkeypatch.setattr(
            dhc, "get",
            lambda url, headers=None, timeout=30.0: FakeResponse(
                {"data": [{"id": "a"}, {"id": "b"}]}
            ),
        )
        entries = dhc._DirectModels(FakeClient()).list()
        assert [e.id for e in entries] == ["a", "b"]


class TestListModelsStillDegradesSafely:
    def test_a_failure_is_an_empty_list_not_an_exception(self):
        """The reason the bug was invisible. Kept: /models must not raise at a user mid-session."""
        from myriaxon.model_info import list_models

        class Exploding:
            class models:
                @staticmethod
                def list():
                    raise RuntimeError("gateway down")

        assert list_models("openai_compat", Exploding()) == []
