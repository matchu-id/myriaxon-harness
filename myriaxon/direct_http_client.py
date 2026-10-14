"""Direct HTTP client for OpenAI-compatible chat completions -- no `openai` SDK import.

Why this exists, measured on this machine:

    import httpx    4.2s   (~40 modules)
    import openai  65-75s  (803 modules, dominated by openai._module_client eagerly pulling every
                            resource's type definitions -- beta, graders, eval params, assistants)

`openai` is 15x the import cost of `httpx` for a wire protocol that is JSON over HTTP with an SSE
stream. For a CLI that starts a fresh process per command, that import IS the startup time, and it
dwarfs everything Myriaxon itself does.

This module speaks the same protocol directly:

  * streaming SSE with incremental text, reasoning, and tool-call deltas
  * `extra_body` passthrough (top_k, min_p, reasoning.effort -- what Netra and OpenRouter accept)
  * function calling, assembled from index-keyed deltas exactly like the SDK does
  * the same error surface shape the CLI already classifies (status_code, message)

It is deliberately a drop-in for the narrow slice of the SDK that Myriaxon actually uses, not a
general SDK. Anything the SDK would give beyond that slice is not needed by this harness.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Iterator


class DirectAPIError(Exception):
    """Mirrors the SDK's APIStatusError surface: .status_code and .message.

    The CLI's existing error classification reads those two attributes, so raising this keeps that
    logic working unchanged when the SDK is bypassed.
    """

    def __init__(self, status_code: int, message: str, body: str = "") -> None:
        super().__init__(f"Error code: {status_code} - {message}")
        self.status_code = status_code
        self.message = message
        self.body = body


class DirectAuthError(DirectAPIError):
    """A 401 or 403: the key was refused. The SDK has its own AuthenticationError for this; the CLI says what to do about it."""


class DirectRateLimitError(DirectAPIError):
    """A 429: the provider is limiting the caller. The SDK has its own RateLimitError, which the fallback model reacts to."""


def api_error(status_code: int, message: str, body: str = "") -> DirectAPIError:
    """The right error for an HTTP status, so the CLI can tell a refused key and a rate limit from any other failure."""
    kind = DirectAuthError if status_code in (401, 403) else DirectRateLimitError if status_code == 429 else DirectAPIError
    return kind(status_code, message, body)


class DirectConnectionError(Exception):
    """Transport failure. Named like the SDK's APIConnectionError for the same reason as above."""


@dataclass
class FunctionCallShim:
    """Mirrors `openai.types.chat.ChatCompletionChunk`'s function-call shape.

    Exposes both `arguments` (the SDK's attribute) and `arguments_text()` (what myriaxon's
    openai_agent calls), so the agent works unchanged against either client.
    """

    name: str | None = None
    arguments: str = ""

    def arguments_text(self) -> str:
        return self.arguments or ""


@dataclass
class ToolCallShim:
    index: int = 0
    id: str | None = None
    type: str = "function"
    function: FunctionCallShim = field(default_factory=FunctionCallShim)


@dataclass
class DeltaShim:
    content: str | None = None
    reasoning_content: str | None = None
    reasoning_details: list[dict] | None = None
    tool_calls: list[ToolCallShim] | None = None


@dataclass
class ChoiceShim:
    delta: DeltaShim = field(default_factory=DeltaShim)
    finish_reason: str | None = None
    index: int = 0


@dataclass
class UsageShim:
    """Mirrors the SDK's usage object -- openai_agent reads .prompt_tokens/.completion_tokens."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


@dataclass
class ChunkShim:
    """One streamed chunk in the SDK's shape, so agent code needs no branching per client."""

    choices: list[ChoiceShim] = field(default_factory=list)
    usage: UsageShim | None = None


@dataclass
class MessageShim:
    content: str | None = None
    tool_calls: list[ToolCallShim] | None = None


@dataclass
class CompletionShim:
    """Non-streaming response, matching `client.chat.completions.create()`'s return shape."""

    choices: list = field(default_factory=list)
    usage: UsageShim | None = None


class DirectChatCompletions:
    def __init__(self, client: "DirectOpenAI") -> None:
        self._client = client

    def create(self, **kwargs):
        if kwargs.pop("stream", False):
            return self._client._stream_chat(**kwargs)
        return self._client._complete_chat(**kwargs)


class DirectChat:
    def __init__(self, client: "DirectOpenAI") -> None:
        self.completions = DirectChatCompletions(client)


class DirectOpenAI:
    """Minimal stand-in for `openai.OpenAI`, limited to `.chat.completions.create`.

    `base_url` is used as-is (the OpenAI SDK appends /chat/completions to the same root), and
    `extra_body` entries are merged into the request body, matching the SDK's behaviour.
    """

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        timeout: float = 300.0,
        max_retries: int = 0,
        default_headers: dict | None = None,
    ) -> None:
        # No third-party HTTP import: the transport at the bottom of this file is stdlib.
        if not base_url:
            raise ValueError("base_url is required for the direct HTTP client")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.max_retries = max_retries
        self._headers = {"Content-Type": "application/json", **(default_headers or {})}
        # Only send an Authorization header when a key was supplied. "Bearer " with an empty token is
        # rejected by httpx as LocalProtocolError("Illegal header value b'Bearer '"), which is a
        # confusing way to report "no key given" -- and local or gateway endpoints legitimately need
        # no key at all.
        if api_key:
            self._headers["Authorization"] = f"Bearer {api_key}"
        # One client for the process: connection reuse is the point of keeping a warm session.
        # Explicit per-phase timeouts rather than a single float. Passing one number sets connect,
        # read, write and pool to the same value, which is wrong in both directions for a streaming
        # LLM call: a long read timeout is wanted (a model can legitimately pause between tokens),
        # but a long connect timeout only ever delays reporting an unreachable host.
        self._timeout = _Timeout(
            connect=30.0,
            # between-token gap allowance
            read=timeout,
            write=60.0,
            pool=30.0,
        )
        self._http = _Client(timeout=self._timeout, follow_redirects=True)
        self.chat = DirectChat(self)
        
        # Attribute the SDK exposes and the CLI inspects; reported as-is so /doctor stays truthful.
        self.models = _DirectModels(self)

    # -- internals ---------------------------------------------------------------
    def _stream_chat(self, **kwargs) -> Iterator[ChunkShim]:
        body = dict(kwargs)
        extra = body.pop("extra_body", None) or {}
        body.update(extra)
        body["stream"] = True
        # Make reasoning and usage visible: without this some gateways omit them entirely.
        body.setdefault("stream_options", {"include_usage": True})

        url = f"{self.base_url}/chat/completions"
        request_start = time.perf_counter()
        first_byte_at: float | None = None
        try:
            body_bytes = len(json.dumps(body, default=str))
            with self._http.stream("POST", url, headers=self._headers, json_body=body) as response:
                if response.status_code >= 400:
                    detail = response.read().decode("utf-8", errors="replace")
                    raise api_error(
                        response.status_code,
                        _extract_message(detail) or f"HTTP {response.status_code}",
                        detail[:400],
                    )
                for line in response.iter_lines():
                    if first_byte_at is None:
                        first_byte_at = time.perf_counter()
                    if not line:
                        continue
                    if isinstance(line, bytes):
                        line = line.decode("utf-8", errors="replace")
                    line = line.strip()
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        chunk = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    shim = _to_chunk_shim(chunk)
                    if shim is not None:
                        yield shim
        except DirectAPIError:
            raise
        except Exception as exc:  # noqa: BLE001 -- normalized into the SDK-shaped error the CLI knows
            raise DirectConnectionError(f"{type(exc).__name__}: {exc}") from exc

    def _complete_chat(self, **kwargs) -> CompletionShim:
        """Non-streaming call. Used by /compact and the memory-summary path."""
        body = dict(kwargs)
        extra = body.pop("extra_body", None) or {}
        body.update(extra)
        body.pop("stream", None)
        url = f"{self.base_url}/chat/completions"
        try:
            response = self._http.post(url, headers=self._headers, json_body=body)
            if response.status_code >= 400:
                raise api_error(response.status_code, _extract_message(response.text), response.text)
            return _to_completion_shim(response.json())
        except DirectAPIError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise DirectConnectionError(f"{type(exc).__name__}: {exc}") from exc

    def close(self) -> None:
        try:
            self._http.close()
        except Exception:  # noqa: BLE001
            pass


class _DirectModels:
    """`/models` support, so `/models` and the doctor report keep working without the SDK."""

    def __init__(self, client: DirectOpenAI) -> None:
        self._client = client

    def list(self):
        pass  # stdlib only: get below is this module's own.
        try:
            response = get(f"{self._client.base_url}/models", headers=self._client._headers, timeout=30.0)
            response.raise_for_status()
            data = response.json().get("data", [])
        except Exception as exc:  # noqa: BLE001
            raise DirectConnectionError(str(exc)) from exc

        class _Entry:
            def __init__(self, payload: dict) -> None:
                self.id = payload.get("id", "")
                self.display_name = payload.get("name") or payload.get("display_name")
                self.context_length = payload.get("context_length")
                self.top_provider = payload.get("top_provider")

        return [_Entry(item) for item in data]

    def retrieve(self, model: str):  # pragma: no cover -- documented as 404 on most gateways
        for entry in self.list():
            if entry.id == model:
                return entry
        raise DirectAPIError(404, f"model {model!r} not found")




# ---------------------------------------------------------------------------------------
# A small HTTP transport, so this module needs no third-party HTTP library.
# ---------------------------------------------------------------------------------------


class _Timeout:
    """Per-phase timeouts, matching the httpx shape the constructor already passed."""

    def __init__(self, *, connect: float = 30.0, read: float = 300.0,
                 write: float = 60.0, pool: float = 30.0) -> None:
        self.connect = connect
        self.read = read
        self.write = write
        self.pool = pool


class _Response:
    """What the call sites need from a response: status, text, json, and the raw byte stream."""

    def __init__(self, status_code: int, headers: dict, raw, connection) -> None:
        self.status_code = status_code
        self.headers = headers
        self._raw = raw
        self._connection = connection
        self._body: bytes | None = None

    @property
    def text(self) -> str:
        return self.read().decode("utf-8", errors="replace")

    def read(self) -> bytes:
        if self._body is None:
            self._body = self._raw.read() if self._raw is not None else b""
        return self._body

    def json(self):
        return json.loads(self.text)

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise api_error(
                self.status_code,
                _extract_message(self.text) or f"HTTP {self.status_code}",
                self.text[:400],
            )

    def iter_lines(self):
        """SSE lines as they arrive, one at a time.

        There is no de-chunking here, deliberately. The previous version checked for
        `Transfer-Encoding: chunked` and de-chunked by hand, on the stated belief that "`http.client` returns the RAW
        chunked body". That belief is false: `http.client.HTTPResponse` decodes chunked framing itself, so the de-chunker
        was handed already-decoded SSE. Its first `readline()` returned `data: {...}`, `int(b"data: ...", 16)` raised
        ValueError, and it returned -- yielding ZERO lines from a stream that was working perfectly.

        Measured cost of that bug (2026-10-08): every turn against a local Ollama model raised a bare, silent
        "Empty response from the model" and retried twice; the server log showed each request cancelled after 2 tokens
        with the client never reading a chunk. Ollama sends `Transfer-Encoding: chunked` on every streamed completion,
        so the check that was meant to make the reader robust was the single thing that broke it.

        Reading line by line also keeps the stream incremental. `read()` would block until the provider closed the
        response, turning every turn into one long wait and defeating the streaming path entirely.
        """
        stream = self._raw
        if stream is None:
            return
        while True:
            try:
                line = stream.readline()
            except Exception:  # noqa: BLE001 -- a broken socket ends the stream, it does not crash the turn
                return
            if not line:
                return
            if isinstance(line, bytes):
                line = line.decode("utf-8", errors="replace")
            yield line.rstrip("\r\n")


class _Client:
    """POSTs JSON over http.client, applying the connect and read timeouts separately."""

    def __init__(self, *, timeout=None, follow_redirects: bool = True) -> None:
        self.timeout = timeout or _Timeout()
        self.follow_redirects = follow_redirects

    # -- internals ------------------------------------------------------------------
    def _connect(self, parsed, timeout: float):
        import http.client

        if parsed.scheme == "https":
            import ssl

            return http.client.HTTPSConnection(
                parsed.hostname, parsed.port or 443, timeout=timeout,
                context=ssl.create_default_context(),
            )
        return http.client.HTTPConnection(parsed.hostname, parsed.port or 80, timeout=timeout)

    def _send(self, method: str, url: str, headers: dict, body: bytes | None):
        """Send one request, following redirects up to a bound. Returns `(response, connection)`."""
        import http.client
        from urllib.parse import urljoin, urlparse

        target = url
        for _hop in range(5):
            parsed = urlparse(target)
            # CONNECT gets the connect timeout; everything after it gets the read timeout.
            connection = self._connect(parsed, self.timeout.connect)
            try:
                path = parsed.path or "/"
                if parsed.query:
                    path += "?" + parsed.query
                connection.request(method, path, body=body, headers=headers)
                connection.sock.settimeout(self.timeout.read)
                response = connection.getresponse()
            except Exception:
                connection.close()
                raise
            if self.follow_redirects and response.status in (301, 302, 303, 307, 308):
                location = response.getheader("Location")
                response.read()
                connection.close()
                if not location:
                    raise DirectConnectionError(f"HTTP {response.status} with no Location header")
                if response.status == 303:
                    # A 303 means "GET the result", which is the one redirect whose method changes.
                    method = "GET"
                    body = None
                target = urljoin(target, location)
                continue
            return response, connection
        raise DirectConnectionError("too many redirects")

    # -- the httpx surface the call sites use ---------------------------------------
    def request(self, method: str, url: str, *, headers: dict | None = None, json_body=None, **kwargs):
        body = None
        send_headers = dict(headers or {})
        if json_body is not None:
            body = json.dumps(json_body, default=str).encode("utf-8")
            send_headers.setdefault("Content-Length", str(len(body)))
        response, connection = self._send(method, url, send_headers, body)
        return _Response(response.status, dict(response.getheaders()), response, connection)

    def post(self, url: str, *, headers: dict | None = None, content=None, json_body=None, **kwargs):
        """POST, accepting either raw `content` or a `json_body` to serialise.

        `json_body` IS ACCEPTED HERE BECAUSE ITS ABSENCE WAS A SILENT BUG, and the second of its kind in this
        file. `_complete_chat` called `self._http.post(url, ..., json_body=body)`, and this signature took
        only `content` -- so `json_body` fell into `**kwargs`, the body stayed `None`, and an EMPTY POST was
        sent. Measured against the live Netra gateway:

            DirectOpenAI.chat.completions.create(...)  -> 400 "request body must be a JSON object"
            the same payload via _Client.request(...)   -> 200, "PONG"

        The failure was loud here only because Netra rejects an empty body. A gateway that tolerated one would
        have answered a DIFFERENT question than the one asked, silently.

        The sibling bug was the same shape: `get()` called `post()` while its docstring said "the one call that
        is not a POST", and the reason it survived is recorded on that function. Both were possible because a
        permissive `**kwargs` accepts a plausible-looking keyword and drops it, so the keyword is now named.
        """
        if json_body is not None:
            return self.request("POST", url, headers=headers, json_body=json_body)
        body = content
        if isinstance(body, str):
            body = body.encode("utf-8")
        response, connection = self._send("POST", url, dict(headers or {}), body)
        return _Response(response.status, dict(response.getheaders()), response, connection)

    def stream(self, method: str, url: str, *, headers: dict | None = None, json_body=None, **kwargs):
        """A context manager over a response, so the connection closes after the body is consumed."""
        return _StreamContext(self, method, url, headers=headers, json_body=json_body)

    def close(self) -> None:
        """Nothing is held open between calls, so there is nothing to close."""


class _StreamContext:
    """`with client.stream(...) as response:` -- what the SSE path uses."""

    def __init__(self, client: _Client, method: str, url: str, *, headers=None, json_body=None) -> None:
        self._client = client
        self._method = method
        self._url = url
        self._headers = headers
        self._json_body = json_body
        self._connection = None

    def __enter__(self) -> _Response:
        body = None
        send_headers = dict(self._headers or {})
        if self._json_body is not None:
            body = json.dumps(self._json_body, default=str).encode("utf-8")
            send_headers.setdefault("Content-Length", str(len(body)))
        response, connection = self._client._send(self._method, self._url, send_headers, body)
        self._connection = connection
        return _Response(response.status, dict(response.getheaders()), response, connection)

    def __exit__(self, *exc_info) -> bool:
        if self._connection is not None:
            try:
                self._connection.close()
            except Exception:  # noqa: BLE001 - closing must never mask the original error
                pass
        return False


def get(url: str, *, headers: dict | None = None, timeout: float = 30.0):
    """A module-level GET, for the `/models` listing. The one call that is not a POST.

    It used to call `client.post(...)` while its own docstring said "the one call that is not a POST",
    so `GET /models` was sent as `POST /models`. Measured against the live Netra gateway:

        /models via DirectOpenAI  -> DirectConnectionError: Error code: 405 - method not allowed
        /models via plain HTTP    -> 200, two models

    The consequence was silent rather than loud, which is why it survived: `model_info.list_models`
    catches every exception and returns `[]`, and the caller renders that as "Could not list models for
    this provider (unsupported or request failed)". So `/models` reported an unsupported provider for
    every OpenAI-compatible endpoint -- Netra, DeepSeek, OpenRouter, OpenAI and Ollama alike -- while the
    catalogue was one correct verb away.

    The `pass` that used to open this function is gone with it. It was there to keep the local name
    `get` bound before the call below it, which is the kind of thing that reads as a typo and is easy to
    delete by accident -- and deleting it would have made this function resolve its own name as a global
    and recurse.
    """
    client = _Client(timeout=_Timeout(connect=timeout, read=timeout, write=timeout, pool=timeout))
    return client.request("GET", url, headers=headers)

def _extract_message(body: str) -> str:
    """Pull the human-readable error out of the provider's envelope."""
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        return body[:300]
    error = parsed.get("error")
    if isinstance(error, dict):
        return error.get("message") or json.dumps(error)[:300]
    return (error if isinstance(error, str) else None) or body[:300]


def _usage_shim(raw: dict | None) -> UsageShim | None:
    if not raw:
        return None
    return UsageShim(
        prompt_tokens=int(raw.get("prompt_tokens") or 0),
        completion_tokens=int(raw.get("completion_tokens") or 0),
        total_tokens=int(raw.get("total_tokens") or 0),
    )


def _to_chunk_shim(chunk: dict) -> ChunkShim | None:
    """Convert one SSE payload into the SDK's chunk shape.

    The usage-only final chunk carries no `choices` -- the agent checks `.usage` before skipping
    empty choice lists, exactly as it does with SDK chunks, so that ordering is preserved here.
    """
    usage = _usage_shim(chunk.get("usage"))
    choices_raw = chunk.get("choices") or []
    if not choices_raw:
        return ChunkShim(choices=[], usage=usage) if usage else None

    choices = []
    for raw_choice in choices_raw:
        raw_delta = raw_choice.get("delta") or {}
        tool_calls = None
        if raw_delta.get("tool_calls"):
            tool_calls = []
            for raw_call in raw_delta["tool_calls"]:
                function = raw_call.get("function") or {}
                tool_calls.append(
                    ToolCallShim(
                        index=int(raw_call.get("index") or 0),
                        id=raw_call.get("id"),
                        function=FunctionCallShim(
                            name=function.get("name"), arguments=function.get("arguments") or ""
                        ),
                    )
                )
        details = raw_delta.get("reasoning_details")
        choices.append(
            ChoiceShim(
                index=int(raw_choice.get("index") or 0),
                finish_reason=raw_choice.get("finish_reason"),
                delta=DeltaShim(
                    content=raw_delta.get("content"),
                    # Providers differ: OpenRouter/Netra use reasoning_details, DeepSeek uses
                    # reasoning_content, some gateways send a plain `reasoning` string.
                    reasoning_content=raw_delta.get("reasoning_content") or raw_delta.get("reasoning"),
                    reasoning_details=details if isinstance(details, list) else None,
                    tool_calls=tool_calls,
                ),
            )
        )
    return ChunkShim(choices=choices, usage=usage)


def _to_completion_shim(payload: dict) -> CompletionShim:
    choices = []
    for raw_choice in payload.get("choices") or []:
        message = raw_choice.get("message") or {}
        tool_calls = None
        if message.get("tool_calls"):
            tool_calls = [
                ToolCallShim(
                    index=int(raw_call.get("index") or 0),
                    id=raw_call.get("id"),
                    function=FunctionCallShim(
                        name=(raw_call.get("function") or {}).get("name"),
                        arguments=(raw_call.get("function") or {}).get("arguments") or "",
                    ),
                )
                for raw_call in message["tool_calls"]
            ]
        choices.append(type("_Choice", (), {"message": MessageShim(content=message.get("content"), tool_calls=tool_calls)})())
    return CompletionShim(choices=choices, usage=_usage_shim(payload.get("usage")))
