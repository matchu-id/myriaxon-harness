"""Named endpoints the user can add from the UI.

A built-in provider is a hardcoded (endpoint, key-env-var, model list) triple. That covers the
popular services but not "my company runs an OpenAI-compatible gateway at this URL", which is exactly
the case that otherwise forces someone to edit `config.json` by hand or run the CLI wizard.

These endpoints are stored beside the CLI config so both surfaces see the same set, and are all
spoken over the OpenAI-compatible protocol -- that is what virtually every gateway and self-hosted
runner exposes, and it is the protocol the direct HTTP client already implements.

The API key is stored in the same file as the CLI's own key, which is the existing behaviour for
`config.json` on this platform; the file is written with user-only intent and never served over the
API in full (see `public()`).
"""
from __future__ import annotations

import json
import re
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path

# A name is used as a provider id in the UI and in URLs, so it is restricted to something safe.
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


@dataclass
class CustomEndpoint:
    name: str
    base_url: str
    api_key: str = ""
    models: list[str] = field(default_factory=list)
    model: str = ""
    label: str = ""
    # Anything OpenAI-compatible; kept as a field so a future protocol (e.g. Anthropic-shaped
    # gateways) can be added without a second store.
    protocol: str = "openai"

    def public(self) -> dict:
        """The view sent to the browser: everything except the key itself."""
        data = asdict(self)
        data["has_api_key"] = bool(self.api_key)
        data["api_key"] = ""
        return data

    def to_config(self, base):
        """A Config pointed at this endpoint, reusing the CLI's Config class."""
        import dataclasses

        changes = {
            "provider": self.name,
            "base_url": self.base_url,
            "api_key": self.api_key,
            "model": self.model or (self.models[0] if self.models else ""),
        }
        # Vertex fields belong to Gemini; carrying them into a custom endpoint would confuse the
        # endpoint reporting and could make a later Gemini run resume against a stale project.
        if hasattr(base, "vertex_project"):
            changes["vertex_project"] = ""
            changes["vertex_location"] = ""
        return dataclasses.replace(base, **changes) if dataclasses.is_dataclass(base) else base


class CustomEndpointStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._endpoints: dict[str, CustomEndpoint] = {}
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        for entry in (raw or {}).get("endpoints", []):
            try:
                endpoint = CustomEndpoint(**{k: v for k, v in entry.items() if k in CustomEndpoint.__dataclass_fields__})
            except (TypeError, ValueError):
                continue
            if NAME_RE.match(endpoint.name):
                self._endpoints[endpoint.name] = endpoint

    def _save(self) -> None:
        payload = json.dumps({"endpoints": [asdict(e) for e in self._endpoints.values()]}, indent=2)
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            temp = self._path.with_suffix(".json.tmp")
            try:
                temp.write_text(payload, encoding="utf-8")
                temp.replace(self._path)
            except OSError:
                try:
                    temp.unlink(missing_ok=True)
                except OSError:
                    pass
                self._path.write_text(payload, encoding="utf-8")
        except OSError:
            pass  # a read-only config directory must not break a running server

    # -- api -----------------------------------------------------------------------
    def list(self) -> list[dict]:
        with self._lock:
            return [e.public() for e in self._endpoints.values()]

    def get(self, name: str) -> CustomEndpoint | None:
        with self._lock:
            return self._endpoints.get(name)

    def names(self) -> list[str]:
        with self._lock:
            return sorted(self._endpoints)

    def upsert(self, payload: dict) -> CustomEndpoint:
        """Create or update one endpoint. Raises ValueError on invalid input."""
        name = str(payload.get("name") or "").strip().lower()
        if not NAME_RE.match(name):
            raise ValueError(
                "name must start with a letter or digit and contain only a-z, 0-9, '-' or '_' (max 32)"
            )
        base_url = str(payload.get("base_url") or "").strip()
        if not base_url:
            raise ValueError("base_url is required")
        if not base_url.startswith(("http://", "https://")):
            raise ValueError("base_url must start with http:// or https://")

        models = payload.get("models") or []
        if isinstance(models, str):
            models = [m.strip() for m in models.replace(",", "\n").splitlines() if m.strip()]
        models = [str(m).strip() for m in models if str(m).strip()]

        existing = self.get(name)
        api_key = str(payload.get("api_key") or "").strip()
        if not api_key and existing is not None:
            # Keep the stored key when the caller did not send one: the UI never receives the key, so
            # an edit form would otherwise silently erase it on every save.
            api_key = existing.api_key

        endpoint = CustomEndpoint(
            name=name,
            base_url=base_url.rstrip("/"),
            api_key=api_key,
            models=models,
            model=str(payload.get("model") or (models[0] if models else "")).strip(),
            label=str(payload.get("label") or name).strip(),
            protocol=str(payload.get("protocol") or "openai").strip() or "openai",
        )
        with self._lock:
            self._endpoints[name] = endpoint
            self._save()
        return endpoint

    def remove(self, name: str) -> bool:
        with self._lock:
            existed = self._endpoints.pop(name, None) is not None
            if existed:
                self._save()
            return existed
