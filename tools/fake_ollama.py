"""A fake Ollama server, so the Ollama mode can be tested with no Ollama installed -- and without touching the real one.

It speaks the parts of Ollama's API that Myriaxon uses, with Ollama's own shapes:

  GET    /api/tags      the installed models
  GET    /api/ps        the models loaded in memory, with the window each really runs with
  POST   /api/show      one model: details, architecture (`model_info`), `parameters`, `system`, capabilities
  POST   /api/create    build a model from another (streamed NDJSON, ending in {"status": "success"})
  DELETE /api/delete    remove a model
  POST   /api/generate  load a model and answer (or fail with Ollama's "requires more system memory" error)
  POST   /v1/chat/completions   the OpenAI-compatible chat, scripted like `mock_provider`, so a whole agent turn runs

The models are shaped like the real ones measured on a developer machine: a hybrid-attention 9B that declares 256K
(`ornith-like`), a 4B whose KV cache is listed per layer (`nemotron-like`), and a plain model with no window set. Like
Ollama, a model that sets no `num_ctx` runs with 4,096.

Used by `tests/test_ollama_wiring.py` and `tools/verify_ollama_mode.py`. Not shipped as part of the product.
"""

from __future__ import annotations

import copy
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from mock_provider import ScriptedProvider

GIB = 1024 ** 3
OLLAMA_DEFAULT_WINDOW = 4096


def ornith_like(name: str = "ornith-1.5:9b") -> dict:
    """A 9B hybrid-attention model that declares a 262,144-token window: 8 of 32 layers keep a KV cache (32,768 bytes per token)."""
    return {
        "name": name,
        "size": 6_234_423_947,
        "modified_at": "2026-09-30T10:00:00Z",
        "details": {"family": "qwen35", "parameter_size": "9.0B", "quantization_level": "Q4_K_M", "parent_model": ""},
        "model_info": {
            "general.architecture": "qwen35",
            "qwen35.context_length": 262_144,
            "qwen35.block_count": 32,
            "qwen35.embedding_length": 4096,
            "qwen35.attention.head_count": 16,
            "qwen35.attention.head_count_kv": 4,
            "qwen35.attention.key_length": 256,
            "qwen35.attention.value_length": 256,
            "qwen35.full_attention_interval": 4,
        },
        "capabilities": ["completion", "thinking", "tools", "vision"],
        "parameters": "",
        "system": "",
        "template": "{{ .Prompt }}",
    }


def nemotron_like(name: str = "nemotron-3-nano:4b") -> dict:
    """A 4B model whose KV cache is listed per layer (zeros for the layers that keep none): 16,384 bytes per token."""
    per_layer = [0] * 20 + [8, 0, 0, 0] * 4 + [0] * 4
    return {
        "name": name,
        "size": 2_800_000_000,
        "modified_at": "2026-09-01T10:00:00Z",
        "details": {"family": "nemotron_h", "parameter_size": "4.0B", "quantization_level": "Q4_K_M", "parent_model": ""},
        "model_info": {
            "general.architecture": "nemotron_h",
            "nemotron_h.context_length": 262_144,
            "nemotron_h.block_count": len(per_layer),
            "nemotron_h.embedding_length": 3072,
            "nemotron_h.attention.head_count": 32,
            "nemotron_h.attention.head_count_kv": per_layer,
            "nemotron_h.attention.key_length": 128,
            "nemotron_h.attention.value_length": 128,
        },
        "capabilities": ["completion", "thinking", "tools"],
        "parameters": "",
        "system": "",
        "template": "{{ .Prompt }}",
    }


def prose_only(name: str = "tiny-chat:1b") -> dict:
    """A model that cannot call tools, so it cannot drive the agent."""
    return {
        "name": name,
        "size": 700_000_000,
        "modified_at": "2026-08-01T10:00:00Z",
        "details": {"family": "llama", "parameter_size": "1.0B", "quantization_level": "Q4_0", "parent_model": ""},
        "model_info": {"general.architecture": "llama", "llama.context_length": 8192, "llama.block_count": 16,
                       "llama.embedding_length": 2048, "llama.attention.head_count": 16, "llama.attention.head_count_kv": 4},
        "capabilities": ["completion"],
        "parameters": "",
        "system": "",
        "template": "{{ .Prompt }}",
    }


def default_models() -> list[dict]:
    return [ornith_like(), nemotron_like()]


def _tag_for(name: str) -> str:
    return name if ":" in name else name + ":latest"


def _num_ctx(parameters: str) -> int:
    match = re.search(r"^\s*num_ctx\s+(\d+)\s*$", parameters or "", re.M)
    return int(match.group(1)) if match else 0


class FakeOllama:
    """Context manager: serve on an ephemeral loopback port.

    `models`: the entries to start with (default: an ornith-like and a nemotron-like one).
    `script`: what the chat endpoint says (see `mock_provider.ScriptedProvider`).
    `memory_error`: when set, loading a model (`/api/generate`) fails with this message, as Ollama does when it cannot fit one.
    `keep_window`: False makes `/api/create` ignore `parameters`, like an Ollama that predates that field.
    """

    def __init__(self, models: list[dict] | None = None, *, script: list[tuple] | None = None, memory_error: str = "",
                 keep_window: bool = True) -> None:
        self.models: dict[str, dict] = {}
        for entry in (models if models is not None else default_models()):
            entry = copy.deepcopy(entry)
            entry["name"] = _tag_for(entry["name"])
            self.models[entry["name"]] = entry
        self.loaded: dict[str, dict] = {}
        self.requests: list[tuple[str, str, dict | None]] = []
        self.chat = ScriptedProvider(script or [("text", "Hello from the fake Ollama.")])
        self.memory_error = memory_error
        self.keep_window = keep_window
        handler = type("_BoundHandler", (_Handler,), {"fake": self})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    # -- addresses
    @property
    def host(self) -> str:
        return f"127.0.0.1:{self.port}"

    @property
    def root(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def base_url(self) -> str:
        return self.root + "/v1"

    def __enter__(self) -> "FakeOllama":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.server.shutdown()
        self.server.server_close()

    # -- what a test asks
    def calls(self, method: str, path: str) -> list[dict | None]:
        return [payload for verb, where, payload in self.requests if verb == method and where == path]

    def find(self, name: str) -> dict | None:
        return self.models.get(name) or self.models.get(_tag_for(name))

    def window_of(self, name: str) -> int:
        """What Ollama would run this model with: its own `num_ctx`, else the default."""
        entry = self.find(name)
        return (_num_ctx(entry["parameters"]) if entry else 0) or OLLAMA_DEFAULT_WINDOW


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    fake: FakeOllama

    def log_message(self, *args) -> None:  # keep the test output clean
        pass

    # -- helpers
    def _send(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _payload(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    def _stream_lines(self, lines: list[dict]) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Connection", "close")
        self.end_headers()
        for line in lines:
            self.wfile.write((json.dumps(line) + "\n").encode())
            self.wfile.flush()
        self.close_connection = True

    def _entry(self, payload: dict) -> dict | None:
        return self.fake.find(str(payload.get("model") or payload.get("name") or ""))

    # -- routes
    def do_GET(self) -> None:
        path = self.path.split("?")[0].rstrip("/")
        self.fake.requests.append(("GET", path, None))
        if path == "/api/tags":
            self._send({"models": [
                {"name": e["name"], "model": e["name"], "modified_at": e["modified_at"], "size": e["size"],
                 "details": {k: e["details"].get(k, "") for k in ("family", "parameter_size", "quantization_level")}}
                for e in self.fake.models.values()
            ]})
        elif path == "/api/ps":
            self._send({"models": [{"name": name, "model": name, **info} for name, info in self.fake.loaded.items()]})
        elif path == "/api/version":
            self._send({"version": "0.12.0"})
        elif path == "/v1/models":
            self._send({"object": "list", "data": [{"id": name, "object": "model"} for name in self.fake.models]})
        else:
            self._send({"error": "not found"}, 404)

    def do_DELETE(self) -> None:
        path = self.path.split("?")[0].rstrip("/")
        payload = self._payload()
        self.fake.requests.append(("DELETE", path, payload))
        entry = self._entry(payload)
        if path != "/api/delete" or entry is None:
            self._send({"error": f"model '{payload.get('model')}' not found"}, 404)
            return
        del self.fake.models[entry["name"]]
        self.fake.loaded.pop(entry["name"], None)
        self._send({})

    def do_POST(self) -> None:
        path = self.path.split("?")[0].rstrip("/")
        payload = self._payload()
        self.fake.requests.append(("POST", path, payload))

        if path == "/api/show":
            entry = self._entry(payload)
            if entry is None:
                self._send({"error": f"model '{payload.get('model')}' not found"}, 404)
                return
            self._send({k: entry[k] for k in ("details", "model_info", "parameters", "system", "capabilities", "template")})
        elif path == "/api/create":
            self._create(payload)
        elif path == "/api/generate":
            self._generate(payload)
        elif path.endswith("/chat/completions"):
            self._chat(payload)
        else:
            self._send({"error": "not found"}, 404)

    def _create(self, payload: dict) -> None:
        name = _tag_for(str(payload.get("model") or payload.get("name") or ""))
        base = self.fake.find(str(payload.get("from") or ""))
        if not payload.get("from") or base is None:
            self._stream_lines([{"error": f"model '{payload.get('from')}' not found"}])
            return
        entry = copy.deepcopy(base)
        entry["name"] = name
        if payload.get("system"):
            entry["system"] = str(payload["system"])
        parameters = payload.get("parameters") or {}
        if self.fake.keep_window and isinstance(parameters, dict) and parameters.get("num_ctx"):
            entry["parameters"] = f"num_ctx                        {int(parameters['num_ctx'])}"
        self.fake.models[name] = entry
        self._stream_lines([{"status": "reading model metadata"}, {"status": "creating system layer"},
                            {"status": "writing manifest"}, {"status": "success"}])

    def _generate(self, payload: dict) -> None:
        entry = self._entry(payload)
        if entry is None:
            self._send({"error": f"model '{payload.get('model')}' not found"}, 404)
            return
        if self.fake.memory_error:
            self._send({"error": self.fake.memory_error}, 500)
            return
        window = self.fake.window_of(entry["name"])
        self.fake.loaded[entry["name"]] = {"context_length": window, "size": entry["size"] + window * 32_768,
                                           "size_vram": int(entry["size"] * 0.9)}
        self._send({"model": entry["name"], "response": "ready", "done": True})

    def _chat(self, payload: dict) -> None:
        self.fake.chat.requests.append(payload)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        # `Transfer-Encoding: chunked` and no Content-Length, which is exactly the framing the real
        # Ollama uses for a streamed completion. This is not cosmetic: the client has to read SSE out
        # of a chunked response, and a test double that answered with `Connection: close` instead let
        # a de-chunker that destroyed every line pass unnoticed (see `_Response.iter_lines`). The
        # framing a fake uses IS the framing the client is tested against, so it follows the real one.
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        for piece in self.fake.chat.next_response(payload.get("model", "m"), payload.get("messages") or []):
            self.wfile.write(f"{len(piece):X}\r\n".encode("ascii") + piece + b"\r\n")
            self.wfile.flush()
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()


__all__ = ["FakeOllama", "default_models", "nemotron_like", "ornith_like", "prose_only", "OLLAMA_DEFAULT_WINDOW"]
