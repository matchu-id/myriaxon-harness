"""Ollama on this machine: find it, read each model's real capabilities, and wire Myriaxon to it.

Ollama is the one provider that runs entirely on the user's own hardware -- no API key, no network,
no bill. The obstacle to using it well is that its models differ enormously, and that the numbers a model
*advertises* are not the numbers Ollama *runs* it with. So nothing here is guessed. Every number comes from
Ollama's own API:

  * the model's own context window, from the architecture-specific key in `model_info`
    (`qwen35.context_length`, `llama.context_length`, ... -- the prefix is the architecture, so the key is
    found rather than hard-coded per family);
  * the window Ollama will actually ALLOCATE, from the model's `num_ctx` parameter, or from `/api/ps` for a
    model that is loaded, or Ollama's default (4,096) when neither says;
  * the parameter count and quantisation, from `details`;
  * the capability list, from `capabilities` where the version provides it.

Declared is not usable
----------------------
A model that supports 256K tokens still runs with 4,096 unless something tells Ollama otherwise, and the
OpenAI-compatible endpoint Myriaxon talks to has no way to ask for more per request (Ollama documents this: the
only way is a model whose Modelfile sets `num_ctx`). Measured on the machine this was written on: the loaded model
reported `context_length: 4096` in `/api/ps` while declaring 262,144, and Myriaxon's own system prompt and tool list
are about 4,700 tokens, so the model never saw all of them.

The fix is a model that carries the window: `myriaxon ollama --create` builds one called `myriaxon` from a model
you already have, with the full window the model supports, so `ollama run myriaxon` and the Myriaxon agent both use
it. This module holds everything that command needs: choosing the base model, writing and creating the model,
estimating what the window costs in memory on THIS machine, and reading the real window back.

Absence is a normal state, not an error
---------------------------------------
Most machines do not have Ollama running, and that is not a failure to report as one. Detection returns a `Probe`
describing what was looked for and what answered, and callers say "not found on this machine, here is the one
command that starts it". A silent empty list would leave the user believing they had no models.

Only the standard library is used to reach Ollama, so none of this needs an optional package.
"""

from __future__ import annotations

import ipaddress
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

# Where Ollama listens by default, and the environment variable that moves it. Documented by Ollama
# as `OLLAMA_HOST`; the value may be a bare `host:port` or a full URL, and both are accepted.
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 11434
HOST_ENV = "OLLAMA_HOST"

# Inside a container, the host's loopback is not reachable; Docker Desktop publishes it under this
# name. Tried AFTER the loopback address: on a normal machine the loopback answers at once, and trying the alias
# first costs a DNS failure on every start.
CONTAINER_HOST_ALIAS = "host.docker.internal"

REQUEST_TIMEOUT = 8.0

# Keys `/api/show` uses for the context window, in the order they are tried. The architecture prefix
# varies by model family (`llama`, `qwen2`, `gemma3`, ...), so the suffix is what is matched.
CONTEXT_KEYS = ("context_length", "context_window", "n_ctx", "max_position_embeddings")

# The model this module creates, and what to call it by default. `ollama run myriaxon` is the whole point.
MYRIAXON_MODEL_NAME = "myriaxon"

# The window Ollama runs a model with when nothing sets one: its own documented default, and what `/api/ps` reported
# on the machine this was measured on. Used only as the answer to "what will it really get?".
OLLAMA_DEFAULT_CONTEXT = 4_096
# Ollama's server-wide default, when its owner changed it. Read from OUR environment, which only helps when the
# server was started from the same one; `/api/ps` is the authority for a model that is loaded.
SERVER_CONTEXT_ENV = "OLLAMA_CONTEXT_LENGTH"
# An optional CEILING on what the harness will budget against, for a machine that wants to stay below what a model
# can do. Not a default: unset, the harness uses the real window.
OLLAMA_CONTEXT_ENV = "MYRIAXON_OLLAMA_MAX_CONTEXT"

# Below this the agent cannot work: Myriaxon's system prompt and tool list alone are about 4,700 tokens.
MIN_AGENT_CONTEXT = 8_192

AUTOCREATE_ENV = "MYRIAXON_OLLAMA_AUTOCREATE"

# Persona for the model built by `myriaxon ollama --create`. Short and honest on purpose: it is what the model sees in a
# plain `ollama run myriaxon` chat. Ollama only adds it when a request carries no system message of its own, so the
# agent (which sends Myriaxon's full system prompt) is unaffected by it.
MYRIAXON_SYSTEM = (
    "You are Myriaxon, an assistant for engineering and operations work.\n"
    "- Be direct and concrete: lead with the answer, then the reasoning.\n"
    "- Say what you did and what you could not verify. Never claim that a command ran, a file changed or a test "
    "passed unless you saw it happen.\n"
    "- If you do not know, say so. Do not invent file contents, command output, package names or APIs.\n"
    "- Ask before anything destructive or irreversible.\n"
    "- Keep replies short unless the user asks for detail.\n"
    "In this plain chat you have no tools: you cannot read files or run commands. When the user wants work done on "
    "their machine, tell them to run `myriaxon`, which gives you tools (files, shell, git, the web) under their "
    "permission rules."
)

GIB = 1024 ** 3


@dataclass
class OllamaModel:
    """One model Ollama has locally, with what it can actually do."""

    name: str
    context_length: int = 0            # what the model's architecture supports
    num_ctx: int = 0                   # what the model's own Modelfile sets (0: it sets nothing)
    parameter_size: str = ""
    family: str = ""
    quantisation: str = ""
    size_bytes: int = 0
    modified_at: str = ""
    capabilities: list[str] = field(default_factory=list)
    kv_bytes_per_token: int = 0        # KV cache cost of one token at fp16, from the architecture; 0 when unknown
    raw: dict = field(default_factory=dict)

    @property
    def supports_vision(self) -> bool:
        return "vision" in self.capabilities

    @property
    def supports_tools(self) -> bool:
        return "tools" in self.capabilities

    @property
    def size_gb(self) -> float:
        return self.size_bytes / GIB

    def summary(self) -> str:
        parts = [self.name]
        if self.parameter_size:
            parts.append(self.parameter_size)
        if self.context_length:
            parts.append(f"{self.context_length:,} ctx")
        if self.quantisation:
            parts.append(self.quantisation)
        if self.capabilities:
            parts.append("+".join(sorted(self.capabilities)))
        return "  ".join(parts)


@dataclass
class Probe:
    """What detection found, including why it found nothing.

    Deliberately not a bare list. "No models" and "Ollama is not installed" and "Ollama is installed
    but not running" are three different situations with three different fixes, and a caller that
    cannot tell them apart can only report the least useful of the three.
    """

    base_url: str = ""
    reachable: bool = False
    installed: bool = False
    error: str = ""
    models: list[OllamaModel] = field(default_factory=list)
    loaded: dict[str, int] = field(default_factory=dict)   # model name -> the window Ollama allocated for it now

    @property
    def available(self) -> bool:
        return self.reachable and bool(self.models)

    def model(self, name: str) -> OllamaModel | None:
        wanted = {name, name + ":latest"} if ":" not in name else {name}
        return next((m for m in self.models if m.name in wanted), None)

    def has(self, name: str) -> bool:
        """Whether a model by this name exists. `ollama list` shows `myriaxon:latest`, so a bare name matches it."""
        return self.model(name) is not None

    def explain(self) -> str:
        """One line a user can act on."""
        if self.available:
            return f"Ollama at {self.base_url} â€” {len(self.models)} model(s)"
        if self.reachable:
            return (f"Ollama is running at {self.base_url} but has no models pulled. "
                    f"Get one with:  ollama pull qwen2.5-coder:7b")
        if self.installed:
            return (f"Ollama is installed but not answering at {self.base_url}. "
                    f"Start it with:  ollama serve")
        if self.error:
            return f"No Ollama found ({self.error}). Install it from https://ollama.com/download"
        return "No Ollama found on this machine. Install it from https://ollama.com/download"


def candidate_hosts() -> list[str]:
    """Base URLs to try, most likely first.

    `OLLAMA_HOST` wins when set, because a user who configured it means it. The loopback address comes next: it is where
    Ollama is on nearly every machine, and it answers or refuses instantly. The container alias is last, because
    Myriaxon may be running inside a container while Ollama runs on the host -- but trying it first costs a DNS lookup
    that fails on every ordinary start.
    """
    seen: list[str] = []
    configured = (os.environ.get(HOST_ENV) or "").strip()
    if configured:
        seen.append(_normalise(configured))
    seen.append(f"http://{DEFAULT_HOST}:{DEFAULT_PORT}")
    if CONTAINER_HOST_ALIAS not in " ".join(seen):
        seen.append(f"http://{CONTAINER_HOST_ALIAS}:{DEFAULT_PORT}")
    return list(dict.fromkeys(seen))


def _normalise(host: str) -> str:
    """Accept `host:port`, `http://host:port` and a trailing slash; return a base URL."""
    text = host.strip().rstrip("/")
    if not text.startswith(("http://", "https://")):
        text = "http://" + text
    return text


def is_installed() -> bool:
    """Whether an `ollama` binary exists on PATH.

    Used only to sharpen the message: "installed but not running" and "not installed" need different
    instructions, and without this the tool can only say the less useful one.
    """
    return shutil.which("ollama") is not None


class OllamaUnavailable(RuntimeError):
    """Ollama cannot be reached or refused a request."""


# ----------------------------------------------------------------------------------------------------- HTTP, stdlib only
class _Response:
    """The little of an HTTP response the callers use."""

    def __init__(self, status_code: int, body: bytes) -> None:
        self.status_code = status_code
        self._body = body

    @property
    def text(self) -> str:
        return self._body.decode("utf-8", "replace")

    def json(self):
        return json.loads(self._body.decode("utf-8"))


def _is_local(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    if host in ("localhost", CONTAINER_HOST_ALIAS):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_loopback or address.is_private


def _opener(url: str):
    """An opener that does not send local traffic through a proxy: an Ollama on this machine or this network is not
    behind the company proxy, and a proxy set for the web would make every local call fail."""
    if _is_local(url):
        return urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener()


def _call(method: str, url: str, payload: dict | None, timeout: float) -> _Response:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, method=method,
                                     headers={"Content-Type": "application/json", "Accept": "application/json"})
    try:
        with _opener(url).open(request, timeout=timeout) as response:
            return _Response(response.status, response.read())
    except urllib.error.HTTPError as exc:  # an HTTP error is an answer, with a body that says why
        return _Response(exc.code, exc.read() or b"")


def _get(url: str, timeout: float = REQUEST_TIMEOUT) -> _Response:
    return _call("GET", url, None, timeout)


def _post(url: str, payload: dict, timeout: float = REQUEST_TIMEOUT) -> _Response:
    return _call("POST", url, payload, timeout)


def _delete(url: str, payload: dict, timeout: float = REQUEST_TIMEOUT) -> _Response:
    return _call("DELETE", url, payload, timeout)


def _stream_post(url: str, payload: dict, timeout: float):
    """Yield each JSON line of a streamed reply. An HTTP error becomes one `{"error": ...}` event."""
    request = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), method="POST",
                                     headers={"Content-Type": "application/json"})
    try:
        response = _opener(url).open(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        body = (exc.read() or b"").decode("utf-8", "replace").strip()
        try:
            message = json.loads(body).get("error") or body
        except Exception:  # noqa: BLE001
            message = body or f"HTTP {exc.code}"
        yield {"error": str(message), "http_status": exc.code}
        return
    with response:
        for raw in response:
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except ValueError:
                continue


def _short_error(exc: BaseException) -> str:
    """A failed connection, in words a person can act on."""
    reason = getattr(exc, "reason", exc)
    if isinstance(reason, ConnectionRefusedError):
        return "connection refused"
    if isinstance(reason, TimeoutError) or "timed out" in str(reason).lower():
        return "timed out"
    return type(reason).__name__


# --------------------------------------------------------------------------------------------------------- describing
def _context_length_from(model_info: dict) -> int:
    """The context window, from whichever architecture key this model uses.

    The key is `<architecture>.context_length` and the architecture varies (`llama`, `qwen2`,
    `gemma3`, `phi3`, ...), so it is matched by suffix. A model whose family is not in any list still
    reports its window, which a hard-coded lookup would miss -- and the whole point is not guessing.
    """
    for key, value in (model_info or {}).items():
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
            continue
        short = key.rsplit(".", 1)[-1]
        if short in CONTEXT_KEYS:
            return int(value)
    return 0


def _num_ctx_from(parameters: str) -> int:
    """`num_ctx` from the parameters text `/api/show` returns (lines such as `num_ctx    16384`). 0 when the model sets none."""
    for line in (parameters or "").splitlines():
        match = re.match(r"\s*num_ctx\s+(\d+)\s*$", line)
        if match:
            return int(match.group(1))
    return 0


def kv_bytes_per_token(model_info: dict) -> int:
    """What one token costs in KV cache at fp16, from the architecture metadata. 0 when the metadata does not say.

    Not every layer keeps a KV cache, and that is why a 256K window is affordable for some models and not for others:

      * a plain transformer keeps one on every layer;
      * a HYBRID model (Qwen3.5 and its relatives) alternates a few full-attention layers with linear-attention
        layers whose state does not grow with the context: `full_attention_interval = 4` means one layer in four;
      * some architectures list `head_count_kv` PER layer with zeros for the layers that keep none.

    Each layer that keeps one stores key and value for `head_count_kv` heads, two bytes per element at fp16.
    """
    info = model_info or {}
    prefix = next((key[: -len(".block_count")] for key in info if key.endswith(".block_count")), "")
    if not prefix:
        return 0

    def number(suffix: str, default: float = 0) -> float:
        value = info.get(f"{prefix}.{suffix}", default)
        return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else default

    layers = int(number("block_count"))
    key_length = number("attention.key_length")
    value_length = number("attention.value_length")
    heads = number("attention.head_count")
    if not key_length and heads:
        key_length = value_length = number("embedding_length") / heads
    if layers <= 0 or key_length <= 0:
        return 0

    per_layer = info.get(f"{prefix}.attention.head_count_kv")
    if isinstance(per_layer, list):
        kv_heads_total = sum(float(n) for n in per_layer if isinstance(n, (int, float)))
    else:
        kv_heads = float(per_layer) if isinstance(per_layer, (int, float)) and not isinstance(per_layer, bool) else heads
        interval = int(number("full_attention_interval"))
        attention_layers = math.ceil(layers / interval) if interval > 1 else layers
        kv_heads_total = kv_heads * attention_layers
    return int(kv_heads_total * (key_length + (value_length or key_length)) * 2)


def describe_model(base_url: str, name: str) -> OllamaModel:
    """Read one model's real capabilities from `/api/show`. Never raises."""
    model = OllamaModel(name=name)
    try:
        response = _post(f"{base_url}/api/show", {"model": name})
        if response.status_code >= 400:
            return model
        payload = response.json()
    except Exception:  # noqa: BLE001
        return model

    model.raw = payload
    details = payload.get("details") or {}
    model.family = str(details.get("family") or "")
    model.parameter_size = str(details.get("parameter_size") or "")
    model.quantisation = str(details.get("quantization_level") or "")

    model_info = payload.get("model_info") or {}
    model.context_length = _context_length_from(model_info)
    model.kv_bytes_per_token = kv_bytes_per_token(model_info)
    model.num_ctx = _num_ctx_from(str(payload.get("parameters") or ""))
    if not model.family:
        # `details.family` is absent on some versions; the model_info prefix carries the same fact.
        for key in model_info:
            if "." in key:
                model.family = key.split(".", 1)[0]
                break

    # `capabilities` is the reliable list where present. Some builds do not send it, in which case an
    # empty list is honest -- claiming an unknown model supports tools is worse than saying nothing.
    caps = payload.get("capabilities")
    if isinstance(caps, list):
        model.capabilities = [str(c) for c in caps]
    return model


def loaded_models(base_url: str, timeout: float = REQUEST_TIMEOUT) -> dict[str, tuple[int, int, int]]:
    """Models Ollama has in memory right now: name -> (the window it allocated, bytes in memory, bytes of that in VRAM).

    `/api/ps` is the one place that reports the window a model REALLY runs with, so it outranks anything inferred. Never raises.
    """
    try:
        response = _get(f"{base_url}/api/ps", timeout=timeout)
        if response.status_code >= 400:
            return {}
        entries = response.json().get("models") or []
    except Exception:  # noqa: BLE001
        return {}
    result: dict[str, tuple[int, int, int]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or entry.get("model") or "")
        if name:
            result[name] = (int(entry.get("context_length") or 0), int(entry.get("size") or 0),
                            int(entry.get("size_vram") or 0))
    return result


def _loaded_window(loaded: dict[str, tuple[int, int, int]], name: str) -> int:
    """The window Ollama allocated for `name` if it is loaded; a bare name matches its `:latest` form."""
    entry = loaded.get(name) or loaded.get(name + ":latest") or (0, 0, 0)
    return entry[0]


def detect(*, with_details: bool = True, timeout: float = REQUEST_TIMEOUT, hosts: list[str] | None = None) -> Probe:
    """Find Ollama, and describe its models. Never raises.

    `with_details=False` skips the per-model `/api/show` calls, which is one request per model -- worth
    avoiding on a status line, worth doing when reporting capabilities. `hosts` asks about exactly those
    addresses instead of the usual candidates: a configuration that names an Ollama is asked about that one.
    """
    probe = Probe(installed=is_installed())
    errors: list[str] = []

    for base_url in (hosts if hosts is not None else candidate_hosts()):
        probe.base_url = base_url
        try:
            response = _get(f"{base_url}/api/tags", timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{base_url}: {_short_error(exc)}")
            continue
        if response.status_code >= 400:
            errors.append(f"{base_url}: HTTP {response.status_code}")
            continue

        try:
            payload = response.json()
        except Exception:  # noqa: BLE001
            errors.append(f"{base_url}: response was not JSON")
            continue

        probe.reachable = True
        entries = payload.get("models") or []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or entry.get("model") or "")
            if not name:
                continue
            model = OllamaModel(
                name=name,
                size_bytes=int(entry.get("size") or 0),
                modified_at=str(entry.get("modified_at") or ""),
            )
            details = entry.get("details") or {}
            model.family = str(details.get("family") or "")
            model.parameter_size = str(details.get("parameter_size") or "")
            model.quantisation = str(details.get("quantization_level") or "")
            probe.models.append(model)

        probe.loaded = {name: values[0] for name, values in loaded_models(base_url, timeout=timeout).items()}

        if with_details:
            # `/api/tags` reports the on-disk size and `/api/show` does not, so the detailed objects
            # replace the list ones and the size has to be carried across. Without this every model
            # came back as 0 bytes, and anything ordering by size silently ordered by name instead --
            # measured: the "smallest tool-capable model" resolved to a 9B HuggingFace path rather
            # than the 4B one.
            sizes = {m.name: m.size_bytes for m in probe.models}
            stamps = {m.name: m.modified_at for m in probe.models}
            detailed = [describe_model(base_url, m.name) for m in probe.models]
            for model in detailed:
                model.size_bytes = sizes.get(model.name, 0)
                model.modified_at = stamps.get(model.name, "")
            probe.models = detailed
        return probe

    probe.error = "; ".join(errors[:2]) if errors else "nothing answered"
    return probe


# ----------------------------------------------------------------------------------------------------------- the window
def _ceiling() -> int:
    """The optional ceiling the user put on what the harness budgets against, or 0."""
    value = (os.environ.get(OLLAMA_CONTEXT_ENV) or "").strip()
    return int(value) if value.isdigit() and int(value) > 0 else 0


def server_default_context() -> int:
    """The window Ollama gives a model that sets none: its server-wide default if the environment names one, else 4,096."""
    value = (os.environ.get(SERVER_CONTEXT_ENV) or "").strip()
    return int(value) if value.isdigit() and int(value) > 0 else OLLAMA_DEFAULT_CONTEXT


def runtime_context(model: OllamaModel, loaded: int = 0) -> int:
    """The window Ollama will actually run this model with, which is what the harness must budget against.

    In order of authority: the model's own `num_ctx` (set by its Modelfile, and what `myriaxon ollama --create` sets); the
    window `/api/ps` reports for a model that is loaded now; Ollama's default. Never more than the model supports, and
    never more than the optional ceiling the user set. The OLD behaviour here was a fixed 32,768 cap on everything, which
    understated a model that really runs with 262,144 and overstated one that really runs with 4,096.
    """
    window = model.num_ctx or loaded or server_default_context()
    if model.context_length:
        window = min(window, model.context_length)
    ceiling = _ceiling()
    return min(window, ceiling) if ceiling else window


def practical_context(declared: int) -> int:
    """The window to budget against when only a declared window is known: the declared one, under the optional ceiling.

    Unknown (0) is Ollama's default, because a model with no stated window is the one least safe to assume a large one for.
    """
    window = declared if declared > 0 else OLLAMA_DEFAULT_CONTEXT
    ceiling = _ceiling()
    return min(window, ceiling) if ceiling else window


def profile_hint(context_length: int) -> str:
    """Which Myriaxon profile a window maps to, so a user can see the tier they will get.

    Mirrors `model_profiles._profile_from_window`'s thresholds. Duplicating two numbers is a real
    tradeoff: importing the profile module here would drag the whole budget machinery into a
    detection path that must stay dependency-light, and this function is for *display* only -- the
    tier that actually applies is computed by `detect_profile` from the same window.
    """
    usable = practical_context(context_length)
    if usable <= 0:
        return "unknown"
    if usable <= 16_000:
        return "small"
    if usable <= 100_000:
        return "balanced"
    return "large"


def tool_capable(model: "OllamaModel") -> bool:
    """Whether this model can be given tool schemas.

    Ollama reports `capabilities`, and a model without `tools` cannot call functions. Sending it
    schemas anyway does not fail loudly -- the model ignores them and answers in prose, which reads as
    the agent refusing to use its tools rather than as a model that cannot.
    """
    if not model.capabilities:
        return True     # unknown: trying is the better default than silently disabling tools
    return model.supports_tools


def default_model(models: list["OllamaModel"]) -> str:
    """A sensible model to preselect, or "" when none is usable.

    Ollama has no notion of a default model -- which one is right depends entirely on what the user
    pulled and how much memory the machine has. But leaving the field empty is worse than choosing:
    a turn with no model name fails, and with Ollama's API that failure surfaces as a bare
    `404 model not found`, which names neither the provider nor the missing field.

    Preference order, and the reasoning is about tool calling rather than size: a model that cannot
    call tools cannot drive the agent at all, so those are excluded outright. Among the rest the
    smallest is chosen, because the most likely reason the field is empty is that the user has not
    decided, and a smaller model is likelier to fit in memory and to start quickly.
    """
    capable = [m for m in models if tool_capable(m)]
    if not capable:
        return ""
    # Sizes are unavailable when the probe skipped `/api/show`; falling back to name order keeps the
    # choice deterministic instead of depending on dict order.
    return min(capable, key=lambda m: (m.size_bytes, m.name.lower())).name


def _billions(parameter_size: str) -> float:
    """`"8.95B"` -> 8.95, `"397B"` -> 397.0, `"135M"` -> 0.135; 0 when unreadable."""
    match = re.fullmatch(r"\s*([0-9.]+)\s*([KMBT]?)\s*", (parameter_size or "").upper())
    if not match:
        return 0.0
    try:
        number = float(match.group(1))
    except ValueError:
        return 0.0
    return number * {"": 1e-9, "K": 1e-6, "M": 1e-3, "B": 1.0, "T": 1e3}[match.group(2)]


def choose_base(probe: Probe, preferred: str = "", *, memory: tuple[int, int] | None = None,
                gpu: tuple[int, int] | None = None) -> OllamaModel | None:
    """The model `myriaxon` should be built from.

    1. A model asked for by name.
    2. The model that is LOADED right now, if it can call tools: it is the one the user is evidently using, and that is a
       decision they have already made.
    3. Otherwise the best tool-capable model THIS MACHINE can hold: the largest by parameter count among those whose
       weights fit (in the GPU plus half the RAM), and the most recently added of equals. An agent is only as good as its
       model, so size ranks above the "smallest, to be safe" choice `default_model` makes; but a model that does not
       fit would swap and freeze the machine, so fitting ranks above size.

    A model already called `myriaxon` is never its own base.
    """
    candidates = [m for m in probe.models if m.name.split(":")[0] != MYRIAXON_MODEL_NAME]
    if preferred:
        wanted = {preferred, preferred + ":latest"} if ":" not in preferred else {preferred}
        return next((m for m in candidates if m.name in wanted), None)
    loaded = [m for m in candidates if (m.name in probe.loaded or m.name + ":latest" in probe.loaded) and tool_capable(m)]
    if loaded:
        return loaded[0]
    capable = [m for m in candidates if tool_capable(m)]
    if not capable:
        return None
    ram_total = (memory if memory is not None else system_memory())[1]
    vram_total = (gpu if gpu is not None else gpu_memory())[1]
    budget = vram_total + ram_total // 2

    def fits(model: OllamaModel) -> bool:
        return not budget or not model.size_bytes or model.size_bytes <= budget

    capable.sort(key=lambda m: m.modified_at, reverse=True)          # newest first; the sorts below are stable
    return sorted(capable, key=lambda m: (not fits(m), -_billions(m.parameter_size)))[0]


# ------------------------------------------------------------------------------------------------------------ the memory
KV_TYPE_ENV = "OLLAMA_KV_CACHE_TYPE"
# Bytes per element relative to fp16, from Ollama's own cache types (block layouts of 32 elements).
KV_TYPE_FACTOR = {"f16": 1.0, "q8_0": 34 / 64, "q4_0": 18 / 64}
FIT_OVERHEAD_BYTES = 1 * GIB     # compute graph and buffers
OS_RESERVE_BYTES = 3 * GIB       # what the rest of the machine needs to stay usable


def kv_cache_type() -> str:
    value = (os.environ.get(KV_TYPE_ENV) or "f16").strip().lower()
    return value if value in KV_TYPE_FACTOR else "f16"


def system_memory() -> tuple[int, int]:
    """(available, total) bytes of RAM, or (0, 0) when this platform will not say. Standard library only."""
    try:
        if sys.platform == "win32":
            import ctypes

            class _Status(ctypes.Structure):
                _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong), ("total", ctypes.c_ulonglong),
                            ("available", ctypes.c_ulonglong), ("page_total", ctypes.c_ulonglong),
                            ("page_available", ctypes.c_ulonglong), ("virtual_total", ctypes.c_ulonglong),
                            ("virtual_available", ctypes.c_ulonglong), ("extended", ctypes.c_ulonglong)]

            status = _Status()
            status.length = ctypes.sizeof(_Status)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
            return int(status.available), int(status.total)
        if sys.platform.startswith("linux"):
            values: dict[str, int] = {}
            for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
                key, _, rest = line.partition(":")
                parts = rest.split()
                if parts and parts[0].isdigit():
                    values[key] = int(parts[0]) * 1024
            return values.get("MemAvailable", 0), values.get("MemTotal", 0)
        if sys.platform == "darwin":
            total = int(subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=5).stdout.strip())
            text = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=5).stdout
            page = int(re.search(r"page size of (\d+) bytes", text).group(1))
            pages = sum(int(m) for m in re.findall(r"Pages (?:free|inactive|speculative):\s+(\d+)\.", text))
            return pages * page, total
    except Exception:  # noqa: BLE001
        pass
    return 0, 0


def gpu_memory() -> tuple[int, int]:
    """(free, total) bytes of the largest NVIDIA GPU, or (0, 0). Other GPUs are not measured, and are not guessed at."""
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=memory.free,memory.total", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=5).stdout
        rows = [tuple(int(p.strip()) * 1024 * 1024 for p in line.split(",")) for line in out.splitlines() if "," in line]
        return max(rows, key=lambda r: r[1]) if rows else (0, 0)
    except Exception:  # noqa: BLE001
        return 0, 0


@dataclass
class Fit:
    """What a window costs, against what this machine has."""

    window: int
    weights: int = 0
    kv: int = 0
    overhead: int = FIT_OVERHEAD_BYTES
    kv_type: str = "f16"
    ram_available: int = 0
    ram_total: int = 0
    vram_free: int = 0
    vram_total: int = 0
    loaded_bytes: int = 0
    verdict: str = "unknown"        # fits | tight | too-big | unknown

    @property
    def need(self) -> int:
        return self.weights + self.kv + self.overhead

    def lines(self, name: str = "") -> list[str]:
        """What to tell the user, in the words they need to act."""
        if self.verdict == "unknown" or not self.kv:
            return []
        label = f"{self.window:,}-token window" + (f" for {name}" if name else "")
        out = [f"{label}: about {self.need / GIB:.1f} GB = weights {self.weights / GIB:.1f} + KV cache "
               f"{self.kv / GIB:.1f} ({self.kv_type}) + {self.overhead / GIB:.1f} buffers."]
        have = []
        if self.vram_total:
            have.append(f"{self.vram_total / GIB:.0f} GB GPU ({self.vram_free / GIB:.1f} free)")
        if self.ram_total:
            have.append(f"{self.ram_total / GIB:.0f} GB RAM ({self.ram_available / GIB:.1f} available now)")
        if have:
            out.append("This machine: " + ", ".join(have) + ".")
        if self.verdict == "fits":
            out.append("That fits in the memory available now.")
        else:
            q8_need = (self.weights + self.kv * KV_TYPE_FACTOR["q8_0"] / KV_TYPE_FACTOR[self.kv_type] + self.overhead) / GIB
            quarter = (self.weights + self.kv // 4 + self.overhead) / GIB
            verb = "fits only if other programs are closed" if self.verdict == "tight" else "is more than this machine can hold"
            out.append(f"That {verb}. To need less: set OLLAMA_FLASH_ATTENTION=1 and OLLAMA_KV_CACHE_TYPE=q8_0 and restart "
                       f"Ollama (about {q8_need:.1f} GB), or lower the window (a quarter of it is about {quarter:.1f} GB).")
        return out


def estimate_fit(model: OllamaModel, window: int, *, memory: tuple[int, int] | None = None,
                 gpu: tuple[int, int] | None = None, loaded_bytes: int = 0, kv_type: str | None = None) -> Fit:
    """What `window` costs for `model`, against this machine. Never raises; the verdict is "unknown" when it cannot tell.

    Counted: the weights, the KV cache from the architecture (see `kv_bytes_per_token`), and a fixed allowance for
    buffers. "fits" means it fits in what is free now (counting a model Ollama would unload to make room); "tight" means
    it fits in this machine's total memory less a reserve for the system, so closing other programs would be enough;
    "too-big" means it does not fit even then.
    """
    kv_type = kv_type or kv_cache_type()
    available, total = memory if memory is not None else system_memory()
    vram_free, vram_total = gpu if gpu is not None else gpu_memory()
    fit = Fit(window=window, weights=model.size_bytes, kv_type=kv_type, ram_available=available, ram_total=total,
              vram_free=vram_free, vram_total=vram_total, loaded_bytes=loaded_bytes)
    if not model.kv_bytes_per_token or not model.size_bytes:
        return fit
    fit.kv = int(window * model.kv_bytes_per_token * KV_TYPE_FACTOR[kv_type])
    if not (total or vram_total):
        return fit
    free_now = available + vram_free + loaded_bytes
    capacity = max(0, int(total * 0.85) - OS_RESERVE_BYTES) + vram_total
    fit.verdict = "fits" if fit.need <= free_now else ("tight" if fit.need <= capacity else "too-big")
    return fit


# --------------------------------------------------------------------------------------------------------------- create
FALLBACK_WINDOW = 32_768     # for a base model that does not state its window: a size every tool-capable model handles


def parse_window(text: str, declared: int) -> int:
    """`--ctx` as tokens: "max" (or nothing) is the model's own window, "65536" is itself, "64k" is 64 x 1,024. 0 when unreadable.

    A lowercase `k` is 1,024 because that is how model windows are named (256K is 262,144, 32K is 32,768).
    """
    text = (text or "").strip().lower().replace(",", "").replace("_", "")
    if text in ("", "max", "full", "all"):
        return declared or FALLBACK_WINDOW
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)\s*([km]?)", text)
    if not match:
        return 0
    number = float(match.group(1)) * {"": 1, "k": 1024, "m": 1024 * 1024}[match.group(2)]
    return int(number)


def default_window(model: OllamaModel, *, memory: tuple[int, int] | None = None,
                   gpu: tuple[int, int] | None = None) -> tuple[int, Fit]:
    """The window `myriaxon` gets when nobody chose one: the model's full window, halved only while this machine cannot hold it at all.

    "Cannot hold" is `too-big`: not even with every other program closed. A window that is merely tight is kept, and the
    user is told what it costs (`Fit.lines`), because the full window is what they asked for and what the model is for.
    """
    window = model.context_length or FALLBACK_WINDOW
    fit = estimate_fit(model, window, memory=memory, gpu=gpu)
    while fit.verdict == "too-big" and window // 2 >= MIN_AGENT_CONTEXT:
        window = window // 2 // 1024 * 1024
        fit = estimate_fit(model, window, memory=memory, gpu=gpu)
    return window, fit


def made_by_us(model: OllamaModel) -> bool:
    """Whether a model is one `create_model` built (so `--remove` may delete it): it carries the Myriaxon persona."""
    system = str(model.raw.get("system") or "")
    return system.startswith(MYRIAXON_SYSTEM.splitlines()[0])


def modelfile_text(base: str, window: int, system: str = MYRIAXON_SYSTEM) -> str:
    """The Modelfile equivalent of what `create_model` asks Ollama for, for a user to read or to give `ollama create`."""
    return f'FROM {base}\nPARAMETER num_ctx {int(window)}\nSYSTEM """{system}"""\n'


@dataclass
class CreateResult:
    ok: bool
    name: str = ""
    base: str = ""
    window: int = 0
    via: str = ""                  # "api" or "cli"
    message: str = ""


def _api_create(base_url: str, name: str, base: str, window: int, system: str, timeout: float, progress) -> CreateResult:
    payload = {"model": name, "name": name, "from": base, "system": system,
               "parameters": {"num_ctx": int(window)}, "stream": True}
    for event in _stream_post(f"{base_url}/api/create", payload, timeout):
        if event.get("error"):
            return CreateResult(False, name, base, window, "api", str(event["error"]))
        status = str(event.get("status") or "")
        if status and progress:
            progress(status)
    # Do not trust the stream's last line: ask Ollama what it actually kept.
    shown = describe_model(base_url, name)
    if shown.num_ctx != window:
        return CreateResult(False, name, base, window, "api",
                            "Ollama created the model but did not keep the window (this version may predate the "
                            "parameters field of the create API)")
    return CreateResult(True, name, base, window, "api", "created")


def _is_default_host(base_url: str) -> bool:
    parsed = urlparse(base_url)
    return (parsed.hostname or "") in (DEFAULT_HOST, "localhost") and (parsed.port or DEFAULT_PORT) == DEFAULT_PORT


def _cli_create(base_url: str, name: str, base: str, window: int, system: str, timeout: float) -> CreateResult:
    """`ollama create -f Modelfile`, for a server whose create API is older than this module expects."""
    binary = shutil.which("ollama")
    if not binary:
        return CreateResult(False, name, base, window, "cli", "the `ollama` command is not on PATH")
    env = dict(os.environ)
    if base_url and not _is_default_host(base_url):
        env[HOST_ENV] = urlparse(base_url).netloc
    with tempfile.TemporaryDirectory(prefix="myx-modelfile-") as folder:
        path = Path(folder) / "Modelfile"
        path.write_text(modelfile_text(base, window, system), encoding="utf-8", newline="\n")
        done = subprocess.run([binary, "create", name, "-f", str(path)], capture_output=True, text=True, timeout=timeout, env=env)
    if done.returncode != 0:
        return CreateResult(False, name, base, window, "cli", (done.stderr or done.stdout).strip()[-300:])
    return CreateResult(True, name, base, window, "cli", "created")


def create_model(base_url: str, name: str, base: str, window: int, *, system: str = MYRIAXON_SYSTEM,
                 timeout: float = 600.0, progress=None) -> CreateResult:
    """Create (or replace) the Ollama model `name` from `base`, with `window` as its `num_ctx` and `system` as its persona.

    Asked of the server's create API first, then of the `ollama` command when the API does not support it. Creating
    only writes a small manifest that points at the base model's files, so it is quick and uses almost no disk. Never
    raises: the reason is in `.message`.
    """
    try:
        result = _api_create(base_url, name, base, window, system, timeout, progress)
    except Exception as exc:  # noqa: BLE001
        result = CreateResult(False, name, base, window, "api", f"{_short_error(exc)}: {exc}")
    if result.ok:
        return result
    try:
        fallback = _cli_create(base_url, name, base, window, system, timeout)
    except Exception as exc:  # noqa: BLE001
        fallback = CreateResult(False, name, base, window, "cli", str(exc))
    if fallback.ok:
        if describe_model(base_url, name).num_ctx == window:
            return fallback
        return CreateResult(False, name, base, window, "cli", "the model was created but its window was not kept")
    return CreateResult(False, name, base, window, "api", f"{result.message}. {fallback.message}".strip())


def remove_model(base_url: str, name: str) -> tuple[bool, str]:
    """Delete a model Ollama has. Removing one built by `create_model` leaves the base model it was built from untouched."""
    try:
        response = _delete(f"{base_url}/api/delete", {"model": name, "name": name})
    except Exception as exc:  # noqa: BLE001
        return False, f"{_short_error(exc)}: {exc}"
    if response.status_code == 404:
        return False, f"Ollama has no model called {name}"
    if response.status_code >= 400:
        return False, response.text.strip()[:200] or f"HTTP {response.status_code}"
    return True, "removed"


def probe_load(base_url: str, name: str, timeout: float = 300.0) -> tuple[bool, str, dict]:
    """Load `name` and ask it for a few tokens, to see that it really runs at its window. Returns (ok, message, `/api/ps` entry).

    This allocates the model's memory. Callers run it only when the memory estimate says it fits.
    """
    try:
        response = _post(f"{base_url}/api/generate",
                         {"model": name, "prompt": "Reply with the single word: ready", "stream": False,
                          "options": {"num_predict": 16}, "keep_alive": "2m"}, timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        return False, f"{_short_error(exc)}: {exc}", {}
    if response.status_code >= 400:
        try:
            message = response.json().get("error") or response.text
        except Exception:  # noqa: BLE001
            message = response.text
        return False, str(message).strip()[:300], {}
    window, size, in_vram = (loaded_models(base_url).get(name) or loaded_models(base_url).get(name + ":latest") or (0, 0, 0))
    return True, "loaded and answered", {"context_length": window, "size": size, "size_vram": in_vram}


# ---------------------------------------------------------------------------------------------------- wiring Myriaxon in
@dataclass
class Wiring:
    """Where Myriaxon should talk to, and what it learned getting there."""

    base_url: str            # the OpenAI-compatible endpoint, ending in /v1
    model: str
    window: int
    notes: list[str] = field(default_factory=list)


def autocreate_enabled() -> bool:
    return (os.environ.get(AUTOCREATE_ENV) or "1").strip().lower() not in ("0", "false", "no", "off")


def wire(*, model: str = "", create: bool | None = None, timeout: float = 2.0, progress=None) -> tuple[Wiring | None, str]:
    """Find Ollama and choose what Myriaxon should use: (wiring, "") or (None, why not).

    A model asked for by name is used as it is. Otherwise the `myriaxon` model if it exists; otherwise one is created
    from the best base model (see `choose_base`) with its full window, unless creation is switched off. A machine
    with no Ollama, or none with a tool-capable model, gets None and the reason, never an exception.
    """
    probe = detect(with_details=True, timeout=timeout)
    if not probe.available:
        return None, probe.explain()

    notes: list[str] = []
    chosen: OllamaModel | None
    if model:
        chosen = probe.model(model)
        if chosen is None:
            return None, f"Ollama has no model called '{model}'. It has: {', '.join(m.name for m in probe.models)}"
    else:
        chosen = probe.model(MYRIAXON_MODEL_NAME)
        if chosen is None:
            base = choose_base(probe)
            if base is None:
                return None, ("Ollama has models, but none of them can call tools, which the agent needs. "
                              "Pull one that can, for example:  ollama pull qwen2.5-coder:7b")
            if autocreate_enabled() if create is None else create:
                window, _fit = default_window(base)
                result = create_model(probe.base_url, MYRIAXON_MODEL_NAME, base.name, window, progress=progress)
                if result.ok:
                    notes.append(f"Created the Ollama model '{MYRIAXON_MODEL_NAME}' from {base.name} with a {window:,}-token "
                                 f"window. `ollama run {MYRIAXON_MODEL_NAME}` works too; remove it with "
                                 f"`ollama rm {MYRIAXON_MODEL_NAME}`.")
                    chosen = describe_model(probe.base_url, MYRIAXON_MODEL_NAME)
                else:
                    notes.append(f"Could not create '{MYRIAXON_MODEL_NAME}' ({result.message}); using {base.name} as it is.")
                    chosen = base
            else:
                chosen = base
    window = runtime_context(chosen, _loaded_window(loaded_models(probe.base_url), chosen.name))
    # A window too small for the agent's own prompt is said HERE, not only in `startup_notes`.
    #
    # `startup_notes` reaches the interactive REPL and nothing else: a one-shot run (`-p`), the loci
    # commands and any other entry point build their config straight from `wire` and never call it. On
    # a 4,096-token model those paths said nothing at all and the first turn failed with "Context
    # window is 216% full" or an empty response -- the model judged before it was told the fix. The
    # note is attached where the window is decided, so every consumer inherits it.
    if window < MIN_AGENT_CONTEXT:
        notes.append(
            f"Ollama runs {plain_name(chosen.name)} with a {window:,}-token window, too small for the "
            f"agent (its own prompt is about 4,700). It may answer short questions but will refuse a "
            f"real task. Fix it with:  myriaxon ollama --create"
        )
    return Wiring(probe.base_url.rstrip("/") + "/v1", plain_name(chosen.name), window, notes), ""


def plain_name(name: str) -> str:
    """`myriaxon:latest` -> `myriaxon`. Ollama lists the default tag but accepts the bare name for it, and `ollama run myriaxon` is what people type."""
    return name[: -len(":latest")] if name.endswith(":latest") else name


def _root_of(base_url: str) -> str:
    root = base_url.rstrip("/")
    return root[:-3] if root.endswith("/v1") else root


def startup_notes(base_url: str, name: str, timeout: float = 2.0) -> list[str]:
    """What the user should know when Myriaxon starts on this Ollama model: at most two short lines, never an exception.

    The three things that go wrong with a local model, each said before the first turn rather than as a connection error
    or a bare 404 during it: Ollama is not running; it has no model by that name; the window is too small for the agent
    (or so large that this machine may struggle to load it).
    """
    try:
        root = _root_of(base_url)
        probe = detect(with_details=False, timeout=timeout, hosts=[root])
        if not probe.reachable:
            return [f"Ollama is not answering at {root}. Start it with `ollama serve` (or open the Ollama app) and run this again."]
        installed = probe.model(name)
        if installed is None:
            return [f"Ollama has no model called {name}. See what it has with `myriaxon ollama`, or build one with "
                    f"`myriaxon ollama --create`."]
        model = describe_model(root, installed.name)
        model.size_bytes = installed.size_bytes
        loaded = loaded_models(root, timeout=timeout)
        window = runtime_context(model, _loaded_window(loaded, installed.name))
        notes: list[str] = []
        if window < MIN_AGENT_CONTEXT:
            notes.append(f"Ollama runs {name} with a {window:,}-token window, too small for the agent (its own prompt is about "
                         f"4,700). Fix it with:  myriaxon ollama --create")
        elif window >= 65_536:
            fit = estimate_fit(model, window, loaded_bytes=sum(v[1] for v in loaded.values()))
            if fit.verdict in ("tight", "too-big"):
                lower = max(MIN_AGENT_CONTEXT, window // 2 // 1024 * 1024)
                notes.append(f"{window:,}-token window needs about {fit.need / GIB:.1f} GB; if loading is slow or fails, lower "
                             f"it with:  myriaxon ollama --create --ctx {lower}")
        return notes[:2]
    except Exception:  # noqa: BLE001
        return []


def agent_facts(base_url: str, name: str, timeout: float = 2.0) -> tuple[int, str]:
    """What the agent needs to know about this Ollama model: (the window to budget against, a size tag like "9b").

    The window is the one Ollama will really run the model with (`runtime_context`), which for a model made by
    `myriaxon ollama --create` is the full window it was made with. The size tag carries the parameter count into the
    harness's rules about small models: a model called `myriaxon` says nothing about being a 9B, but Ollama knows.
    Returns (0, "") when Ollama cannot be asked or does not know the model. Never raises.
    """
    try:
        root = _root_of(base_url)
        model = describe_model(root, name)
        if not (model.context_length or model.num_ctx or model.capabilities):
            return 0, ""
        window = runtime_context(model, _loaded_window(loaded_models(root, timeout=timeout), name))
        billions = _billions(model.parameter_size)
        return window, (f"{max(1, round(billions))}b" if billions else "")
    except Exception:  # noqa: BLE001
        return 0, ""


def limits_for(base_url: str, name: str, timeout: float = 2.0) -> int:
    """The window to budget the agent against for this Ollama model, or 0 when Ollama cannot be asked. Never raises."""
    return agent_facts(base_url, name, timeout)[0]


def hint_for_error(message: str, *, connection: bool = False) -> str:
    """One line of advice for an error from a local model, or "".

    A cloud provider's error is the provider's to fix; a local one nearly always has its cause on this machine and a
    fix the user can do in a minute, which the raw message ("model requires more system memory (15.1 GiB) than is
    available (9.4 GiB)", or a refused connection) does not say.
    """
    text = (message or "").lower()
    if connection:
        return "Is Ollama running? Start it with `ollama serve` (or open the Ollama app), then try again."
    if "more system memory" in text or "out of memory" in text:
        return ("Ollama could not fit the model and its context window in memory. Lower the window:  "
                "myriaxon ollama --create --ctx 65536   (or set OLLAMA_FLASH_ATTENTION=1 and OLLAMA_KV_CACHE_TYPE=q8_0, "
                "restart Ollama; closing other programs also helps).")
    if "not found" in text and "model" in text:
        return "Ollama does not have that model. See what it has with:  myriaxon ollama"
    return ""


# ------------------------------------------------------------------------------------------------------------ reporting
def report(*, with_details: bool = True) -> list[str]:
    """Human-readable lines for `myriaxon ollama`. Never raises."""
    probe = detect(with_details=with_details)
    lines = [probe.explain(), ""]
    if not probe.models:
        return lines

    width = max(len(m.name) for m in probe.models)
    lines.append(f"  {'model'.ljust(width)}  {'params':>8}  {'declared':>10}  {'runs with':>10}  "
                 f"{'profile':>8}  caps")
    too_small: list[str] = []
    for model in sorted(probe.models, key=lambda m: m.name):
        declared = f"{model.context_length:,}" if model.context_length else "unknown"
        runs = runtime_context(model, _loaded_window({k: (v, 0, 0) for k, v in probe.loaded.items()}, model.name))
        caps = ",".join(sorted(model.capabilities)) or "-"
        lines.append(
            f"  {model.name.ljust(width)}  {model.parameter_size or '-':>8}  {declared:>10}  "
            f"{runs:>10,}  {profile_hint(runs):>8}  {caps}"
        )
        if with_details and runs < MIN_AGENT_CONTEXT and tool_capable(model):
            too_small.append(model.name)
    lines.append("")
    lines.append("  'runs with' is the window Ollama gives the model: the model's own num_ctx if it sets one, "
                 f"otherwise Ollama's default ({OLLAMA_DEFAULT_CONTEXT:,}).")
    if too_small:
        lines.append(f"  Too small for the agent (its prompt alone is about 4,700 tokens): {', '.join(too_small)}")
        lines.append("  Fix:  myriaxon ollama --create     (builds a 'myriaxon' model with the full window; "
                     "then `ollama run myriaxon` and the agent both use it)")
    lines.append("  Switch to one with:  myriaxon ollama --use <name>")
    without_tools = [m.name for m in probe.models if not tool_capable(m)]
    if without_tools:
        lines.append(f"  No tool calling (prose-only, they cannot drive the agent): "
                     f"{', '.join(without_tools)}")
    return lines


def save_detection(path: Path) -> bool:
    """Cache the last detection, so a status line does not re-probe on every render."""
    try:
        probe = detect(with_details=True)
        payload = {
            "base_url": probe.base_url,
            "reachable": probe.reachable,
            "installed": probe.installed,
            "models": [
                {
                    "name": m.name,
                    "context_length": m.context_length,
                    "num_ctx": m.num_ctx,
                    "parameter_size": m.parameter_size,
                    "family": m.family,
                    "capabilities": m.capabilities,
                }
                for m in probe.models
            ],
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8", newline="\n")
        return True
    except Exception:  # noqa: BLE001
        return False


def load_detection(path: Path) -> dict:
    try:
        if not path.is_file():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


