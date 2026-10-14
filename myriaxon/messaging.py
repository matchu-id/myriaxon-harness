"""Messaging integration: drive the agent from a chat app and notify it when a turn finishes.

Deliberately provider-shaped rather than provider-locked. WhatsApp has no single API; the same "send a
message" operation is Twilio's REST call, Meta's Cloud API POST, or a plain webhook on a self-hosted
bridge. All of them are HTTP, so this module speaks HTTP and lets the *provider* describe the shape:

    whatsapp.json
    {
      "provider": "twilio" | "meta" | "generic",
      "enabled": true,
      "allow": ["+15551234567"],          # who may drive the agent. Empty means nobody.
      "notify": "+15551234567",           # where turn summaries go, if anywhere
      "send_url": "https://...",          # generic only
      "send_method": "POST",
      "send_headers": {"Authorization": "Bearer ..."},
      "send_body": "{\\"to\\": \\"{to}\\", \\"text\\": \\"{text}\\"}",
      "reply_field": "Body",              # which JSON field carries an inbound message
      "sender_field": "From",
      "secret": "..."                     # optional shared secret for inbound webhooks
    }

Design decisions worth stating:

* **An allowlist is mandatory and defaults to nobody.** An agent that runs shell commands, reachable by
  anyone who finds the number, is a remote code execution service. An empty `allow` list refuses every
  inbound message rather than accepting all of them, so a misconfiguration fails closed.
* **Outbound notification is fire-and-forget and never breaks a turn.** A dead provider must not
  surface as an agent error, so failures are reported to a callback and swallowed.
* **No third-party SDK.** `urllib` only, matching the rest of the harness, so nothing new is needed to
  use it.
"""
from __future__ import annotations

import json
import os
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import run_state

PROVIDERS = ("twilio", "meta", "generic")

# How each provider expects a send. Kept as data so adding a provider is a table entry, not a branch
# buried in the send function.
PROVIDER_DEFAULTS: dict[str, dict] = {
    "twilio": {
        "send_url": "https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json",
        "send_method": "POST",
        "content_type": "form",
        "body": {"To": "{to}", "From": "{from}", "Body": "{text}"},
        "auth": "basic",
        "reply_field": "Body",
        "sender_field": "From",
    },
    "meta": {
        "send_url": "https://graph.facebook.com/v21.0/{phone_number_id}/messages",
        "send_method": "POST",
        "content_type": "json",
        "body": {"messaging_product": "whatsapp", "to": "{to}", "type": "text", "text": {"body": "{text}"}},
        "auth": "bearer",
        "reply_field": "text.body",
        "sender_field": "from",
    },
    "generic": {
        "send_url": "",
        "send_method": "POST",
        "content_type": "json",
        "body": {"to": "{to}", "text": "{text}"},
        "auth": "none",
        "reply_field": "Body",
        "sender_field": "From",
    },
}


class MessagingError(RuntimeError):
    pass


def _dig(data: dict, path: str) -> Any:
    """Read `a.b.c` out of nested dicts, returning None if any step is missing.

    Meta's webhook nests the text at `text.body`; Twilio's is flat. Both are expressed the same way.
    """
    current: Any = data
    for part in path.split("."):
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list) and part.isdigit():
            index = int(part)
            current = current[index] if index < len(current) else None
        else:
            return None
    return current


@dataclass
class MessagingConfig:
    provider: str = "generic"
    enabled: bool = False
    allow: list[str] = field(default_factory=list)
    notify: str = ""
    secret: str = ""
    send_url: str = ""
    send_method: str = ""
    send_headers: dict = field(default_factory=dict)
    send_body: Any = None
    content_type: str = ""
    account_sid: str = ""
    auth_token: str = ""
    phone_number_id: str = ""
    access_token: str = ""
    from_number: str = ""
    reply_field: str = ""
    sender_field: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> "MessagingConfig":
        allowed = data.get("allow") or []
        if isinstance(allowed, str):
            allowed = [a.strip() for a in allowed.split(",") if a.strip()]
        config = cls(
            provider=str(data.get("provider") or "generic").lower(),
            enabled=bool(data.get("enabled")),
            allow=[str(a).strip() for a in allowed],
            notify=str(data.get("notify") or ""),
            secret=str(data.get("secret") or ""),
            send_url=str(data.get("send_url") or ""),
            send_method=str(data.get("send_method") or "").upper(),
            send_headers={str(k): str(v) for k, v in (data.get("send_headers") or {}).items()},
            send_body=data.get("send_body"),
            content_type=str(data.get("content_type") or ""),
            account_sid=str(data.get("account_sid") or ""),
            auth_token=str(data.get("auth_token") or ""),
            phone_number_id=str(data.get("phone_number_id") or ""),
            access_token=str(data.get("access_token") or ""),
            from_number=str(data.get("from_number") or data.get("from") or ""),
            reply_field=str(data.get("reply_field") or ""),
            sender_field=str(data.get("sender_field") or ""),
        )
        if config.provider not in PROVIDERS:
            config.provider = "generic"
        return config

    def resolved(self) -> dict:
        """Provider defaults merged with the explicit values, placeholders substituted."""
        defaults = dict(PROVIDER_DEFAULTS[self.provider])
        merged = {**defaults}
        for key in ("send_url", "send_method", "content_type", "reply_field", "sender_field"):
            if getattr(self, key):
                merged[key] = getattr(self, key)
        if self.send_body is not None:
            merged["body"] = self.send_body

        substitutions = {
            "account_sid": self.account_sid,
            "phone_number_id": self.phone_number_id,
            "from": self.from_number,
        }
        url = str(merged.get("send_url") or "")
        for key, value in substitutions.items():
            url = url.replace("{" + key + "}", value)
        merged["send_url"] = url
        return merged

    def public(self) -> dict:
        """Everything except the credentials, for the dashboard."""
        return {
            "provider": self.provider,
            "enabled": self.enabled,
            "allow": self.allow,
            "notify": self.notify,
            "send_url": self.send_url or PROVIDER_DEFAULTS[self.provider]["send_url"],
            "has_secret": bool(self.secret),
            "has_auth_token": bool(self.auth_token),
            "has_access_token": bool(self.access_token),
            "from_number": self.from_number,
            "account_sid": self.account_sid,
            "phone_number_id": self.phone_number_id,
            "providers": list(PROVIDERS),
            "configured": self.is_usable(),
        }

    def is_usable(self) -> bool:
        if not self.enabled or not self.allow:
            return False
        if self.provider == "twilio":
            return bool(self.account_sid and self.auth_token and self.from_number)
        if self.provider == "meta":
            return bool(self.phone_number_id and self.access_token)
        return bool(self.resolved().get("send_url"))


def _normalise(number: str) -> str:
    """Compare numbers by digits only.

    Twilio sends `whatsapp:+15551234567`, Meta sends `15551234567`, and a human writes
    `+1 (555) 123-4567`. Comparing the raw strings would refuse a caller who is on the allowlist, so
    the comparison strips everything that is not a digit.
    """
    return "".join(ch for ch in str(number) if ch.isdigit())


def is_allowed(config: MessagingConfig, sender: str) -> bool:
    """Whether this sender may drive the agent. Fails closed on an empty or absent list."""
    if not config.allow:
        return False
    wanted = _normalise(sender)
    if not wanted:
        return False
    return any(_normalise(entry) == wanted for entry in config.allow)


def parse_inbound(config: MessagingConfig, payload: dict, headers: dict | None = None) -> tuple[str, str]:
    """Extract (sender, text) from a provider webhook body.

    Raises MessagingError when the payload does not carry a message -- providers send delivery
    receipts and status callbacks to the same URL, and those are not messages to answer.
    """
    if not isinstance(payload, dict):
        raise MessagingError("payload is not an object")

    # Meta nests messages in entry[].changes[].value.messages[].
    if config.provider == "meta":
        entries = payload.get("entry") or []
        for entry in entries:
            for change in (entry.get("changes") or []):
                value = change.get("value") or {}
                for message in (value.get("messages") or []):
                    sender = message.get("from") or ""
                    text = _dig(message, "text.body") or message.get("body") or ""
                    if sender and text:
                        return str(sender), str(text)
        raise MessagingError("no inbound message in the Meta payload (status callback?)")

    reply_field = config.reply_field or PROVIDER_DEFAULTS[config.provider]["reply_field"]
    sender_field = config.sender_field or PROVIDER_DEFAULTS[config.provider]["sender_field"]

    sender = _dig(payload, sender_field)
    text = _dig(payload, reply_field)
    # Twilio puts everything flat in a form-encoded body; a generic bridge may nest under "message".
    if text is None:
        text = _dig(payload, "message.text") or payload.get("text")
    if sender is None:
        sender = payload.get("sender") or payload.get("from")
    if not sender or not text:
        raise MessagingError(
            f"no inbound message: looked for sender in {sender_field!r} and text in {reply_field!r}"
        )
    return str(sender), str(text)


def _render(template: Any, values: dict) -> Any:
    """Substitute {placeholders} through a nested structure."""
    if isinstance(template, str):
        rendered = template
        for key, value in values.items():
            rendered = rendered.replace("{" + key + "}", str(value))
        return rendered
    if isinstance(template, dict):
        return {key: _render(value, values) for key, value in template.items()}
    if isinstance(template, list):
        return [_render(item, values) for item in template]
    return template


def _side_effect_key(recipient: str, text: str) -> str:
    """A stable identity for "this message to this person".

    A hash of the body rather than the body itself: the ledger is written to disk on every step, and a
    run that sends twenty long messages would otherwise carry all twenty into every snapshot. Truncated
    to 16 hex characters, which is far past the point where an accidental collision is a real risk for a
    per-run ledger of at most a few hundred entries.
    """
    import hashlib

    digest = hashlib.sha256(f"{recipient}\x00{text}".encode("utf-8", "replace")).hexdigest()[:16]
    return f"message:{recipient}:{digest}"


def _current_run_state():
    """The run this send belongs to, or None when no Locus run is in progress.

    Read from the environment rather than threaded through every caller: `send` is reached from the
    agent's tools, the notifier and the webhook, and adding a parameter to all of them to carry a value
    that is ambient during a run would put a run-state argument into code that has nothing to do with
    runs. Returns None outside a run, which makes the deduplication inert rather than wrong.
    """
    run_id = os.environ.get("MYRIAXON_RUN_ID") or ""
    if not run_id:
        return None
    try:
        from .run_state import load_state

        return load_state(run_id)
    except Exception:  # noqa: BLE001 - deduplication is an optimisation, never a reason to fail a send
        return None


class MessagingClient:
    """Sends messages through the configured provider. Never raises out of `send_safe`."""

    def __init__(self, config: MessagingConfig, timeout: float = 20.0) -> None:
        self.config = config
        self.timeout = timeout
        self.last_error = ""

    def send(self, text: str, to: str = "") -> str:
        """Send `text`. Returns the provider's response body. Raises MessagingError on failure.

        Idempotent under a resumed run. A message is the clearest example of an outbound side effect
        that must not happen twice: re-sending is visible to a real person and cannot be taken back.
        The ledger lives in the run's durable state (`run_state`), and the key is derived from the
        recipient plus the body, because those are what make two sends "the same send" -- not the time
        they were sent or which step of the run produced them.

        The guard is opt-in through the state file existing: with no run in progress this behaves
        exactly as it did before, so a normal interactive send is never suppressed. Suppressing a real
        message would be a worse failure than sending a duplicate one.
        """
        recipient = to or self.config.notify
        if not recipient:
            raise MessagingError("no recipient: set `notify` or pass a number")
        if not text:
            raise MessagingError("refusing to send an empty message")

        # Deduplication, before the send and not after: the crash this defends against is the one that
        # lands between "the provider accepted it" and "we wrote down that it was sent".
        key = _side_effect_key(recipient, text)
        state = _current_run_state()
        if run_state.already_done(state, key):
            return f"skipped: this message was already sent to {recipient} in this run"
        # Recorded BEFORE the send, deliberately. The window is then "recorded but maybe not sent",
        # which loses one message in a rare crash; the other order gives "sent but not recorded", which
        # sends it again on resume. Losing a message is recoverable by a human; a duplicate to a real
        # client is not.
        run_state.record_side_effect(state, key)

        merged = self.config.resolved()
        url = str(merged.get("send_url") or "")
        if not url:
            raise MessagingError(
                f"no send URL configured for provider {self.config.provider!r} "
                "(set send_url, or account_sid / phone_number_id for a built-in provider)"
            )

        values = {"to": recipient, "text": text, "from": self.config.from_number}
        body_template = merged.get("body") or {}
        rendered = _render(body_template, values)
        content_type = merged.get("content_type") or "json"

        headers = {"Content-Type": "application/json", **self.config.send_headers}
        data: bytes
        if content_type == "form":
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            flat = {k: ("" if v is None else str(v)) for k, v in rendered.items()}
            # A nested form value cannot be encoded; flatten one level for Meta-style payloads that a
            # user has forced to form encoding.
            data = urllib.parse.urlencode(flat).encode()
        else:
            data = json.dumps(rendered).encode()

        auth = merged.get("auth")
        if auth == "basic":
            if not (self.config.account_sid and self.config.auth_token):
                raise MessagingError("twilio needs account_sid and auth_token")
            import base64

            token = base64.b64encode(
                f"{self.config.account_sid}:{self.config.auth_token}".encode()
            ).decode()
            headers["Authorization"] = f"Basic {token}"
        elif auth == "bearer":
            if not self.config.access_token:
                raise MessagingError("this provider needs access_token")
            headers["Authorization"] = f"Bearer {self.config.access_token}"

        request = urllib.request.Request(
            url, data=data, headers=headers, method=merged.get("send_method") or "POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            raise MessagingError(f"provider returned HTTP {exc.code}: {detail}") from exc
        except Exception as exc:  # noqa: BLE001
            raise MessagingError(f"{type(exc).__name__}: {exc}") from exc

    def send_safe(self, text: str, to: str = "") -> bool:
        """Send, recording rather than raising a failure.

        Used for turn notifications: a messaging outage must not turn a successful agent turn into an
        error in the transcript.
        """
        try:
            self.send(text, to)
            self.last_error = ""
            return True
        except MessagingError as exc:
            self.last_error = str(exc)
            return False


# ---------------------------------------------------------------------------------------------
# config file

def load_messaging_config(path: Path) -> MessagingConfig:
    if not path.is_file():
        return MessagingConfig()
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError):
        return MessagingConfig()
    return MessagingConfig.from_dict(data if isinstance(data, dict) else {})


def save_messaging_config(path: Path, data: dict) -> MessagingConfig:
    """Merge `data` into the stored config.

    Credential handling needs care in both directions:

    * A **blank** credential field means "unchanged", because the UI never receives the secret back and
      would otherwise wipe it every time any other field was edited.
    * `clear_secrets: ["secret", "auth_token", "access_token"]` removes one on purpose. Without this
      there was no way to unset a credential at all -- blank kept it, so a leaked or rotated token
      could be replaced but never removed.
    """
    existing: dict = {}
    if path.is_file():
        try:
            existing = json.loads(path.read_text(encoding="utf-8-sig")) or {}
        except (json.JSONDecodeError, OSError):
            existing = {}

    data = dict(data)
    for key in data.pop("clear_secrets", None) or []:
        existing.pop(str(key), None)
        data.pop(str(key), None)

    for secret_key in ("secret", "auth_token", "access_token"):
        if secret_key in data and not data[secret_key]:
            data.pop(secret_key)

    existing.update(data)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(existing, indent=2), encoding="utf-8")
    return MessagingConfig.from_dict(existing)


def format_turn_summary(prompt: str, answer: str, tool_calls: int = 0, limit: int = 900) -> str:
    """A short, phone-readable summary of a finished turn."""
    text = (answer or "").strip()
    if len(text) > limit:
        text = text[:limit].rstrip() + "…"
    lines = []
    if prompt:
        first = prompt.strip().splitlines()[0][:120]
        lines.append(f"you: {first}")
    lines.append(text or "(no reply)")
    if tool_calls:
        lines.append(f"[{tool_calls} tool call(s)]")
    return "\n".join(lines)


class MessagingNotifier:
    """Sends a summary after each turn, on a worker thread.

    Off the turn's thread because the provider call is network I/O: blocking the agent to deliver a
    notification would make the notification cost more than it is worth.
    """

    def __init__(self, client: MessagingClient, console=None) -> None:
        self.client = client
        self.console = console
        self._lock = threading.Lock()
        self.sent = 0
        self.failed = 0

    def notify(self, text: str) -> None:
        if not self.client.config.is_usable():
            return

        def worker() -> None:
            ok = self.client.send_safe(text)
            with self._lock:
                if ok:
                    self.sent += 1
                else:
                    self.failed += 1
            if not ok and self.console is not None:
                from rich.markup import escape

                self.console.print(f"[warning]WhatsApp notify failed: {escape(self.client.last_error)}[/]")

        threading.Thread(target=worker, daemon=True, name="myriaxon-whatsapp-notify").start()

    def stats(self) -> dict:
        with self._lock:
            return {"sent": self.sent, "failed": self.failed}
