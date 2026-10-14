"""A minimal OpenAI-compatible provider, for verifying a real agent turn with no API key.

The harness's own tests are unit tests: they check the pieces. Nothing in them proves that a
turn actually completes -- that a prompt is sent, the stream is parsed, a tool call is
dispatched, its result is fed back, and a final answer is produced. That path is where the
release-blocking defects lived (a missing import, a wrong argument), and every one of them was
invisible to the unit suite because the unit suite never runs a turn.

So this is a tiny server that speaks just enough of the OpenAI chat-completions protocol to
drive that path for real: `/v1/models` for discovery and `/v1/chat/completions` with SSE
streaming and OpenAI-shaped `tool_calls` fragments. It is deterministic -- a scripted sequence of
responses -- so a failure is reproducible rather than a flake.

Used by `tools/e2e_check.py`. Not shipped as part of the product.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODELS = ["myriaxon-test-model"]


def _chunk(payload: dict) -> bytes:
    return f"data: {json.dumps(payload)}\n\n".encode()


def _text_response(model: str, text: str) -> list[bytes]:
    """A streamed plain-text answer, delivered in a few pieces to exercise reassembly."""
    base = {"id": "chatcmpl-test", "object": "chat.completion.chunk", "created": int(time.time()), "model": model}
    out = [_chunk({**base, "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]})]
    for word in text.split(" "):
        out.append(_chunk({**base, "choices": [{"index": 0, "delta": {"content": word + " "}, "finish_reason": None}]}))
    out.append(_chunk({**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}))
    out.append(b"data: [DONE]\n\n")
    return out


def _tool_call_response(model: str, call_id: str, name: str, arguments: dict) -> list[bytes]:
    """A streamed tool call, split mid-argument.

    Split on purpose: providers deliver tool-call arguments as fragments that must be
    concatenated by index, and a client that assumes one complete fragment per delta works
    against this fixture only if it is accidentally right. The first release of the direct HTTP
    client assembled these by index for exactly this reason.
    """
    base = {"id": "chatcmpl-test", "object": "chat.completion.chunk", "created": int(time.time()), "model": model}
    raw = json.dumps(arguments)
    head, tail = raw[: len(raw) // 2], raw[len(raw) // 2 :]
    return [
        _chunk({**base, "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]}),
        _chunk({**base, "choices": [{"index": 0, "delta": {"tool_calls": [
            {"index": 0, "id": call_id, "type": "function",
             "function": {"name": name, "arguments": head}}]}, "finish_reason": None}]}),
        _chunk({**base, "choices": [{"index": 0, "delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": tail}}]}, "finish_reason": None}]}),
        _chunk({**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]}),
        b"data: [DONE]\n\n",
    ]


# A dot's briefing opens with this exact phrase, and the name is quoted. Matching the whole
# sentence rather than just "You are " is essential: the *system* prompt also opens with "You are
# <product>, a terminal-based coding agent" (see system_prompt.py), and for the OpenAI-compatible
# path that prompt is the first message in every request. A looser match therefore read the product
# name out of the system prompt and rewrote every scripted tool call to address a dot called
# "Myriaxon" -- which failed intermittently, depending on which message the scan reached first.
_DOT_BRIEFING_PREFIX = 'You are "'
_DOT_BRIEFING_SUFFIX = '", a persistent Myriaxon agent'


def _dot_name_from(messages: list[dict]) -> str:
    """Extract the dot's name from the conversation, if this turn belongs to a dot.

    Returns an empty string for an ordinary session, so a scripted `dot` argument is left alone.
    Only a *user* message is considered: the briefing is delivered as the turn's prompt, whereas
    the system prompt is a system message and must never be read as one.
    """
    for message in messages:
        if message.get("role") == "system":
            continue
        content = message.get("content")
        if not isinstance(content, str):
            continue
        start = content.find(_DOT_BRIEFING_PREFIX)
        if start < 0:
            continue
        after = content[start + len(_DOT_BRIEFING_PREFIX):]
        end = after.find(_DOT_BRIEFING_SUFFIX)
        if end > 0:
            return after[:end]
        # Older or hand-written briefings may omit the closing phrase; fall back to the closing
        # quote, which is still unambiguous because the name is always quoted.
        end = after.find('"')
        if end > 0:
            return after[:end]
    return ""


def _resolve_arguments(arguments: dict, messages: list[dict]) -> dict:
    """Point a `dot` argument at the dot actually being addressed."""
    if "dot" not in arguments:
        return arguments
    name = _dot_name_from(messages)
    if name:
        resolved = dict(arguments)
        resolved["dot"] = name
        return resolved
    return arguments


class ScriptedProvider:
    """Serves the scripted conversation, choosing by *what is in the request*.

    Each script entry is either ``("text", "...")`` or ``("tool", call_id, name, {args})``. The
    first tool entry is served until the conversation contains a tool result, and the final text
    entry from then on.

    This is deliberately stateless. The previous version counted requests and advanced an index,
    which made it correct only for a single exchange in a fresh server: one provider serves a whole
    test class, so the second scenario began at whatever index the first had reached. The symptom
    was intermittent and confusing -- a dot whose first request returned the closing text instead
    of its tool call, so the transcript had an answer but no tool ever ran, and whether it happened
    depended on how many exchanges a previous test had used.

    Deriving the response from the messages instead means any number of independent exchanges can
    share one server, in any order, and each gets the right answer.
    """

    def __init__(self, script: list[tuple]) -> None:
        self.script = script
        self.requests: list[dict] = []
        self._lock = threading.Lock()

    def _entry_for(self, messages: list[dict]) -> tuple:
        """The scripted response that fits this conversation.

        A tool result already in the history means the tool round trip has happened, so the next
        thing the script has to say is the one after that. The last entry repeats, because a
        conversation may legitimately ask for another turn.
        """
        tool_entries = [e for e in self.script if e[0] == "tool"]
        text_entries = [e for e in self.script if e[0] == "text"]
        if not tool_entries:
            return text_entries[-1] if text_entries else ("text", "")
        if not text_entries:
            return tool_entries[-1]

        has_tool_result = any(m.get("role") == "tool" for m in messages)
        if not has_tool_result:
            # How many tool calls the script wants before it answers: serve them in order, and
            # repeat the last once they are exhausted, so a script with one step never stalls.
            already_called = sum(
                1 for m in messages if m.get("role") == "assistant" and m.get("tool_calls")
            )
            return tool_entries[min(already_called, len(tool_entries) - 1)]
        return text_entries[-1]

    def next_response(self, model: str, messages: list[dict] | None = None) -> list[bytes]:
        messages = messages or []
        with self._lock:
            entry = self._entry_for(messages)
        if entry[0] == "text":
            return _text_response(model, entry[1])
        _, call_id, name, arguments = entry
        return _tool_call_response(model, call_id, name, _resolve_arguments(arguments, messages))


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    provider: ScriptedProvider

    def log_message(self, *args) -> None:  # keep the test output clean
        pass

    def _json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path.rstrip("/").endswith("/models"):
            self._json({"object": "list", "data": [{"id": m, "object": "model"} for m in MODELS]})
            return
        self._json({"error": {"message": "not found"}}, status=404)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {}
        self.provider.requests.append(payload)

        if not self.path.rstrip("/").endswith("/chat/completions"):
            self._json({"error": {"message": "not found"}}, status=404)
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        for piece in self.provider.next_response(
            payload.get("model", MODELS[0]), payload.get("messages") or []
        ):
            self.wfile.write(piece)
            self.wfile.flush()


class RunningProvider:
    """Context manager: serve on an ephemeral loopback port."""

    def __init__(self, script: list[tuple]) -> None:
        self.provider = ScriptedProvider(script)
        handler = type("_BoundHandler", (_Handler,), {"provider": self.provider})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    def __enter__(self) -> "RunningProvider":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.server.shutdown()
        self.server.server_close()


__all__ = ["RunningProvider", "ScriptedProvider", "MODELS"]
