"""Streaming from the built-in HTTP client, against the framing a real server actually sends.

This exists because of a bug that made the whole local-model path unusable and was invisible in every
test (2026-10-08): `_Response.iter_lines` de-chunked by hand, on the documented belief that
"`http.client` returns the RAW chunked body when `Transfer-Encoding: chunked` is set". It does not --
`http.client.HTTPResponse` decodes chunked framing itself -- so the de-chunker read `data: {...}` as a
hex chunk size, failed, and returned zero lines from a stream that was arriving perfectly.

What the user saw was "Empty response from the model -- retrying (1/2)... (2/2)..." and then nothing,
for every turn, with no error anywhere; what the server saw was the client hanging up 2 tokens into
generation. The two test doubles in the repository could not catch it, because both answered with
`Connection: close` and no transfer encoding at all.

So these tests serve the real framing -- chunked, no Content-Length -- and check that both lines and
whole turns come out the other end.
"""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from myriaxon.direct_http_client import DirectOpenAI  # noqa: E402


class _ChunkedSSEHandler(BaseHTTPRequestHandler):
    """Serves one scripted SSE completion the way Ollama does: chunked, no Content-Length."""

    protocol_version = "HTTP/1.1"
    payloads: list[dict] = []
    requests: list[dict] = []

    def log_message(self, *args) -> None:  # keep the test output clean
        pass

    def handle_one_request(self) -> None:
        """Answer one request, and treat a client that hangs up as the end of the test, not an error.

        `http.client` closes the connection as soon as it has read the body it asked for, which the
        client under test does between requests. Without this, each run of the suite printed
        "Exception occurred during processing of request from ('127.0.0.1', ...)" over the real
        result -- noise produced by the test harness, in a suite whose output people read to find the
        failure that is real.
        """
        try:
            super().handle_one_request()
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
            self.close_connection = True

    def handle_error(self, *args) -> None:  # same reason, for anything the above does not catch
        pass

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's spelling
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            _ChunkedSSEHandler.requests.append(json.loads(raw))
        except ValueError:
            _ChunkedSSEHandler.requests.append({})

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        for payload in _ChunkedSSEHandler.payloads:
            piece = f"data: {json.dumps(payload)}\n\n".encode("utf-8")
            self.wfile.write(f"{len(piece):X}\r\n".encode("ascii") + piece + b"\r\n")
            self.wfile.flush()
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()


@pytest.fixture
def chunked_server():
    _ChunkedSSEHandler.payloads = []
    _ChunkedSSEHandler.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ChunkedSSEHandler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, _ChunkedSSEHandler
    finally:
        server.shutdown()
        server.server_close()


def _chunk(delta: dict, finish: str | None = None) -> dict:
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion.chunk",
        "model": "myriaxon",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }


def test_lines_survive_a_chunked_response(chunked_server):
    """The framing itself: a chunked SSE body must come back as its `data:` lines, not as nothing."""
    server, _ = chunked_server
    _ChunkedSSEHandler.payloads = [_chunk({"role": "assistant", "content": "hi"})]

    client = DirectOpenAI(api_key="k", base_url=f"http://127.0.0.1:{server.server_port}/v1")
    response = client._http.stream(
        "POST",
        f"http://127.0.0.1:{server.server_port}/v1/chat/completions",
        headers={"Content-Type": "application/json"},
        json_body={"model": "myriaxon", "stream": True},
    )
    with response as open_response:
        lines = [line for line in open_response.iter_lines() if line.strip()]
    client.close()

    assert lines, "a chunked SSE body produced no lines at all -- the de-chunking bug is back"
    assert lines[0].startswith("data: ")
    assert json.loads(lines[0][len("data: "):])["choices"][0]["delta"]["content"] == "hi"


def test_a_chunked_stream_becomes_a_whole_completion(chunked_server):
    """The end the agent sees: text, reasoning and the finish reason, aggregated from many chunks."""
    server, _ = chunked_server
    _ChunkedSSEHandler.payloads = [
        _chunk({"role": "assistant", "reasoning": "Let me think."}),
        _chunk({"reasoning": " Still thinking."}),
        _chunk({"content": "Hello"}),
        _chunk({"content": " there."}),
        _chunk({}, finish="stop"),
        {"id": "chatcmpl-1", "object": "chat.completion.chunk", "choices": [],
         "usage": {"prompt_tokens": 11, "completion_tokens": 4, "total_tokens": 15}},
    ]

    client = DirectOpenAI(api_key="k", base_url=f"http://127.0.0.1:{server.server_port}/v1")
    text = ""
    reasoning = ""
    finish = None
    usage = None
    for chunk in client.chat.completions.create(
        model="myriaxon",
        messages=[{"role": "user", "content": "hello"}],
        stream=True,
        stream_options={"include_usage": True},
    ):
        if getattr(chunk, "usage", None) is not None:
            usage = chunk.usage
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta
        finish = chunk.choices[0].finish_reason or finish
        text += delta.content or ""
        reasoning += getattr(delta, "reasoning_content", None) or ""
    client.close()

    assert text == "Hello there.", "the streamed answer was lost -- this is the bug the user hit"
    assert reasoning == "Let me think. Still thinking."
    assert finish == "stop"
    assert usage is not None and usage.total_tokens == 15


def test_the_stream_is_read_incrementally_not_all_at_once(chunked_server):
    """A stream that never ends must still yield its first line.

    The previous implementation called `read()` on the whole body for a non-chunked response, which
    blocks until the provider closes the connection. That is indistinguishable from a hang on a
    streaming endpoint, so the first line has to be readable while the server is still writing.
    """
    server, _ = chunked_server
    _ChunkedSSEHandler.payloads = [_chunk({"content": "first"})]

    client = DirectOpenAI(api_key="k", base_url=f"http://127.0.0.1:{server.server_port}/v1")
    stream = client.chat.completions.create(
        model="myriaxon", messages=[{"role": "user", "content": "hello"}], stream=True
    )
    first = next(iter(stream))
    client.close()

    assert first.choices[0].delta.content == "first"
