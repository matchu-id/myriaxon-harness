"""Starting, checking and self-testing the messaging bot.

The bot has no daemon of its own: it is the `/api/inbound` webhook route on the web server, so
starting the bot means starting a server. What was missing was any way to *find that out* -- there
was no command that reported whether the bot was ready, and no way to prove the pipeline worked
without arranging a real WhatsApp message from a real phone.

This module supplies both:

* :func:`readiness` -- every reason the bot would refuse a message right now, in plain words, plus
  the exact command that fixes each one.
* :func:`self_test` -- posts a synthetic provider payload through the *real*
  `handle_inbound_message`, so authentication, the allowlist, parsing and the turn all execute. The
  delivery is checked too, and a failure to deliver is reported rather than hidden, because "the
  agent answered but you never saw it" is the failure that looks like nothing happened.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import constants
from .messaging import MessagingClient, MessagingConfig, load_messaging_config, parse_inbound

# A payload shaped like what a provider posts, used only by the self test. `generic` takes the
# plainest form, so the test exercises the config's own field names instead of a provider quirk.
SELF_TEST_TEXT = "Reply with exactly: bot online"


@dataclass
class Readiness:
    """What would happen if a message arrived right now, and what to do about it."""

    ready: bool = False
    problems: list[str] = field(default_factory=list)
    fixes: list[str] = field(default_factory=list)
    provider: str = ""
    allowed_numbers: list[str] = field(default_factory=list)
    has_secret: bool = False
    notify: str = ""

    def lines(self) -> list[str]:
        """The report, as plain text. Returned rather than printed so the CLI and the dashboard can
        show the same words."""
        out: list[str] = []
        out.append(f"provider        : {self.provider or '(unset)'}")
        out.append(f"allowed senders : {', '.join(self.allowed_numbers) if self.allowed_numbers else 'nobody'}")
        out.append(f"shared secret   : {'set' if self.has_secret else 'not set (webhook is open)'}")
        out.append(f"notify          : {self.notify or '(nowhere)'}")
        out.append("")
        if self.ready:
            out.append("ready: a message from an allowed number will be answered.")
        else:
            out.append("not ready yet:")
            for problem in self.problems:
                out.append(f"  - {problem}")
            if self.fixes:
                out.append("")
                out.append("to fix:")
                for fix in self.fixes:
                    out.append(f"  {fix}")
        return out


def readiness(config: MessagingConfig | None = None, config_file: Path | None = None) -> Readiness:
    """Everything standing between the current configuration and a working bot.

    Every problem comes with the command that clears it, because the failure mode this replaces is a
    webhook that answers 200 with `{"ok": false, "reason": ...}` and nobody reading it.
    """
    config = config or load_messaging_config(config_file or constants.MESSAGING_FILE)
    state = Readiness(
        provider=config.provider,
        allowed_numbers=list(config.allow),
        has_secret=bool(config.secret),
        notify=config.notify,
    )

    if not config.enabled:
        state.problems.append("inbound messages are switched off")
        state.fixes.append("turn on `Accept inbound messages` in the dashboard's Messaging panel,")
        state.fixes.append("or set \"enabled\": true in whatsapp.json")

    if not config.allow:
        state.problems.append(
            "no numbers are allowlisted, so every message is refused "
            "(this is deliberate: an agent that runs shell commands must not answer strangers)"
        )
        state.fixes.append("add your number under `allow` in whatsapp.json, e.g. \"+15551234567\"")

    if config.provider == "twilio":
        missing = [n for n, v in (("account_sid", config.account_sid),
                                  ("auth_token", config.auth_token),
                                  ("from_number", config.from_number)) if not v]
        if missing:
            state.problems.append(f"twilio credentials incomplete: {', '.join(missing)}")
            state.fixes.append("fill them in under Messaging -> Twilio credentials")
    elif config.provider == "meta":
        missing = [n for n, v in (("phone_number_id", config.phone_number_id),
                                  ("access_token", config.access_token)) if not v]
        if missing:
            state.problems.append(f"meta credentials incomplete: {', '.join(missing)}")
            state.fixes.append("fill them in under Messaging -> Meta Cloud API credentials")
    else:
        if not config.resolved().get("send_url"):
            state.problems.append("generic provider has no send_url, so replies cannot be delivered")
            state.fixes.append("set `send_url` in whatsapp.json to your bridge's endpoint")

    if not config.secret:
        state.problems.append(
            "no shared secret, so anyone who can reach the webhook can drive the agent "
            "(the allowlist still applies, but a secret is the outer gate)"
        )
        state.fixes.append("set a `secret`, then send it as the X-Myriaxon-Secret header")

    state.ready = not state.problems
    return state


def self_test(app: Any, text: str = SELF_TEST_TEXT, deliver: bool = True) -> dict:
    """Push a synthetic message through the real inbound path.

    `deliver=False` stops short of the network call, which is what you want to check the agent
    wiring without sending yourself a WhatsApp message. It is reported explicitly rather than
    silently skipped.
    """
    config = app.messaging
    if not config.enabled:
        return {"ok": False, "stage": "config", "reason": "messaging is disabled"}
    if not config.allow:
        return {"ok": False, "stage": "config", "reason": "no numbers are allowlisted"}
    if not config.is_usable():
        return {
            "ok": False,
            "stage": "config",
            "reason": "the provider is not usable: credentials are incomplete for "
                      f"{config.provider!r}, so a reply could not be delivered",
        }

    sender = config.allow[0]
    payload = _synthetic_payload(config, sender, text)

    # Parsing is checked separately from the turn, so a payload-shape mistake is reported as itself
    # rather than as a mysterious refusal further down.
    try:
        parsed_sender, parsed_text = parse_inbound(config, payload)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "stage": "parse", "reason": f"{type(exc).__name__}: {exc}",
                "payload": payload}
    if parsed_text != text:
        return {
            "ok": False,
            "stage": "parse",
            "reason": f"the text did not survive parsing: sent {text!r}, read {parsed_text!r}",
            "payload": payload,
        }

    headers = {"X-Myriaxon-Secret": config.secret} if config.secret else {}
    result = app.handle_inbound_message(payload, headers, deliver=deliver)
    result["stage"] = "turn"
    result["delivered"] = bool(result.get("replied"))

    if not result.get("ok"):
        return result

    # A turn that "completed" with no answer is not a success. The transport can be exercised
    # perfectly while the provider is unreachable, and reporting that as ready would be the same
    # class of lie this module exists to remove -- so the answer is checked, not the return code.
    if not str(result.get("answer") or "").strip():
        result["ok"] = False
        result["reason"] = (
            "the message was accepted and a turn ran, but the agent produced no answer. The usual "
            "cause is the model provider, not the bot: check `myriaxon --version`-style credentials "
            "with `myriaxon auth`, or the provider's own error above."
        )
        result["stage"] = "answer"
        return result

    if not deliver:
        result["note"] = ("the agent answered; delivery was skipped for this test, so no message "
                          "was sent to a real number")
    return result


def _synthetic_payload(config: MessagingConfig, sender: str, text: str) -> dict:
    """A provider-shaped payload using this config's own field names.

    Built from the configured `sender_field`/`reply_field` rather than hardcoded, so the test proves
    the *configured* mapping works -- a hardcoded payload would pass while a real provider's field
    names were mismapped.
    """
    resolved = config.resolved()
    sender_field = str(resolved.get("sender_field") or "From")
    reply_field = str(resolved.get("reply_field") or "Body")
    # Twilio prefixes the channel on the From; the allowlist comparison strips non-digits, so either
    # form parses. Send the plain number the user configured.
    return {sender_field: sender, reply_field: text}


def describe(config: MessagingConfig | None = None) -> str:
    """One line for the dashboard's connection panel."""
    config = config or load_messaging_config(constants.MESSAGING_FILE)
    if not config.enabled:
        return "disabled"
    if not config.is_usable():
        return f"{config.provider} — incomplete"
    return f"{config.provider} — {len(config.allow)} allowed"
