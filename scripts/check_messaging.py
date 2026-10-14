"""Verify the messaging integration against real HTTP, with a real agent turn.

    python scripts/check_messaging.py

No mocking of the network: a small HTTP server stands in for the WhatsApp provider, and the checks
send to it and receive from it over loopback. What is asserted:

  * the allowlist gates who may drive the agent, and fails closed when empty
  * an inbound webhook payload is parsed for each provider shape (twilio flat, meta nested)
  * a delivery receipt / status callback is rejected rather than answered as a message
  * outbound send builds the right request and actually reaches the server
  * a provider outage is recorded, not raised into the turn
  * a full round trip: webhook -> agent (Netra) -> reply sent back to the provider

The agent turn uses Netra, per this project's rule that live provider testing uses Netra only.
"""
import json
import sys
import threading
import urllib.error
import urllib.request
import warnings
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
warnings.filterwarnings("ignore")

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

passed, failed, skipped = [], [], []


def check(label, condition, detail=""):
    (passed if condition else failed).append(label)
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}{(' -- ' + str(detail)) if detail and not condition else ''}")


def skip(label, why):
    skipped.append(label)
    print(f"  [SKIP] {label} -- {why}")


class ProviderServer:
    """A stand-in WhatsApp provider: records sends, serves inbound payloads."""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.requests: list[dict] = []
        self.status = 200
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # keep the test output readable
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length).decode("utf-8", errors="replace")
                content_type = self.headers.get("Content-Type", "")
                parsed = None
                if "json" in content_type:
                    try:
                        parsed = json.loads(raw)
                    except json.JSONDecodeError:
                        parsed = {"_raw": raw}
                else:
                    from urllib.parse import parse_qs

                    parsed = {k: v[0] for k, v in parse_qs(raw).items()}
                outer.requests.append(
                    {
                        "path": self.path,
                        "content_type": content_type,
                        "authorization": self.headers.get("Authorization", ""),
                        "body": parsed,
                    }
                )
                if self.path.startswith("/send"):
                    outer.sent.append(parsed or {})
                self.send_response(outer.status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"ok":true}')

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def stop(self) -> None:
        self.server.shutdown()


def main() -> int:
    from myriaxon.messaging import (
        MessagingClient,
        MessagingConfig,
        MessagingError,
        MessagingNotifier,
        format_turn_summary,
        is_allowed,
        load_messaging_config,
        parse_inbound,
        save_messaging_config,
    )

    provider = ProviderServer()
    print(f"provider stand-in listening on {provider.url}\n")

    print("=== the allowlist is mandatory and fails closed ===")
    empty = MessagingConfig(provider="generic", enabled=True, allow=[])
    check("an empty allowlist permits nobody", not is_allowed(empty, "+15551234567"))
    one = MessagingConfig(provider="generic", enabled=True, allow=["+1 (555) 123-4567"])
    check("a listed number is permitted", is_allowed(one, "+15551234567"))
    check("formatting differences do not break matching", is_allowed(one, "whatsapp:+15551234567"),
          "digits-only comparison")
    check("a different number is refused", not is_allowed(one, "+15559999999"))
    check("a non-numeric sender is refused", not is_allowed(one, "unknown"))
    check("an empty allowlist is not 'usable'", not empty.is_usable())

    print("\n=== inbound parsing, per provider shape ===")
    twilio_body = {"From": "whatsapp:+15551234567", "Body": "list the files", "NumMedia": "0"}
    sender, text = parse_inbound(one, twilio_body)
    check("twilio flat payload parsed", (sender, text) == ("whatsapp:+15551234567", "list the files"),
          (sender, text))

    meta_conf = MessagingConfig(provider="meta", enabled=True, allow=["15551234567"])
    meta_body = {
        "entry": [{"changes": [{"value": {"messages": [
            {"from": "15551234567", "text": {"body": "hello from meta"}}
        ]}}]}]
    }
    sender, text = parse_inbound(meta_conf, meta_body)
    check("nested meta payload parsed", (sender, text) == ("15551234567", "hello from meta"), (sender, text))

    generic = MessagingConfig(
        provider="generic", enabled=True, allow=["+15551234567"],
        reply_field="message.text", sender_field="message.sender",
    )
    sender, text = parse_inbound(generic, {"message": {"sender": "+15551234567", "text": "hi"}})
    check("custom field paths work", (sender, text) == ("+15551234567", "hi"), (sender, text))

    print("\n=== a status callback is not a message ===")
    try:
        parse_inbound(one, {"SmsStatus": "delivered", "MessageSid": "SM123"})
        check("a delivery receipt is rejected", False, "was treated as a message")
    except MessagingError as exc:
        check("a delivery receipt is rejected", True)
        print(f"    reason: {exc}")

    try:
        parse_inbound(meta_conf, {"entry": [{"changes": [{"value": {"statuses": [{"status": "read"}]}}]}]})
        check("a meta status callback is rejected", False, "was treated as a message")
    except MessagingError:
        check("a meta status callback is rejected", True)

    print("\n=== outbound send reaches the provider ===")
    send_conf = MessagingConfig(
        provider="generic", enabled=True, allow=["+15551234567"], notify="+15551234567",
        send_url=f"{provider.url}/send", send_method="POST",
        send_headers={"Authorization": "Bearer test-token"},
    )
    client = MessagingClient(send_conf)
    ok = client.send_safe("hello from the test")
    check("the send succeeded", ok, client.last_error)
    check("the provider received exactly one request", len(provider.sent) == 1, len(provider.sent))
    if provider.sent:
        body = provider.sent[0]
        check("the recipient was sent", body.get("to") == "+15551234567", body)
        check("the text was sent", body.get("text") == "hello from the test", body)
    if provider.requests:
        request = provider.requests[0]
        check("the auth header was sent", request["authorization"] == "Bearer test-token",
              request["authorization"])
        check("the URL path was used", request["path"] == "/send", request["path"])

    print("\n=== form-encoded providers are encoded correctly ===")
    twilio_send = MessagingConfig(
        provider="twilio", enabled=True, allow=["+15551234567"], notify="whatsapp:+15551234567",
        send_url=f"{provider.url}/twilio", account_sid="AC123", auth_token="secret123",
        from_number="whatsapp:+15550000000",
    )
    before = len(provider.requests)
    twilio_client = MessagingClient(twilio_send)
    ok = twilio_client.send_safe("form encoded")
    check("the twilio-shaped send succeeded", ok, twilio_client.last_error)
    new = provider.requests[before:]
    if new:
        request = new[0]
        check("it was form-encoded", "x-www-form-urlencoded" in request["content_type"],
              request["content_type"])
        check("it used basic auth", request["authorization"].startswith("Basic "),
              request["authorization"][:20])
        check("the twilio fields are present",
              request["body"].get("To") == "whatsapp:+15551234567" and request["body"].get("Body") == "form encoded",
              request["body"])
    else:
        check("it was form-encoded", False, "no request recorded")

    print("\n=== a provider outage is recorded, not raised ===")
    provider.status = 500
    provider.sent.clear()
    before_requests = len(provider.requests)
    ok = client.send_safe("this should fail")
    check("a failing send returns False", ok is False)
    check("the failure is explained", "500" in client.last_error or "HTTP" in client.last_error,
          client.last_error)
    check("the failure did not raise into the caller", True)
    check("the request was still attempted", len(provider.requests) > before_requests)

    print("\n=== the notifier is off-thread and swallows failures ===")
    notifier = MessagingNotifier(client)
    notifier.notify("turn finished")
    import time

    for _ in range(50):
        if notifier.stats()["failed"] or notifier.stats()["sent"]:
            break
        time.sleep(0.1)
    stats = notifier.stats()
    check("the notifier recorded the attempt", stats["sent"] + stats["failed"] == 1, stats)
    check("a failing provider counts as failed, not an exception", stats["failed"] == 1, stats)

    provider.status = 200
    notifier2 = MessagingNotifier(client)
    notifier2.notify("turn finished ok")
    for _ in range(50):
        if notifier2.stats()["sent"]:
            break
        time.sleep(0.1)
    check("a working provider counts as sent", notifier2.stats()["sent"] == 1, notifier2.stats())

    print("\n=== summary formatting is phone-sized ===")
    summary = format_turn_summary("a very long prompt " * 40, "answer " * 400, tool_calls=3)
    check("the summary is truncated", len(summary) < 1100, len(summary))
    check("the summary mentions tool calls", "3 tool call(s)" in summary, summary[-60:])
    check("an empty answer does not produce an empty message", format_turn_summary("p", "") != "")

    print("\n=== config round trip keeps secrets ===")
    config_path = REPO / "bench-out" / "messaging-test" / "whatsapp.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    if config_path.is_file():
        config_path.unlink()
    save_messaging_config(config_path, {"provider": "twilio", "enabled": True,
                                        "allow": ["+15551234567"], "auth_token": "super-secret"})
    loaded = load_messaging_config(config_path)
    check("the secret was stored", loaded.auth_token == "super-secret", loaded.auth_token)
    # A blank credential from the UI means "unchanged": the UI never receives the secret back, so
    # treating blank as a new value would erase it on every save.
    save_messaging_config(config_path, {"provider": "twilio", "notify": "+15559999999", "auth_token": ""})
    reloaded = load_messaging_config(config_path)
    check("a blank secret does not erase the stored one", reloaded.auth_token == "super-secret",
          reloaded.auth_token)
    check("other fields were still updated", reloaded.notify == "+15559999999", reloaded.notify)
    check("the public view hides the secret", "super-secret" not in json.dumps(reloaded.public()),
          reloaded.public())

    print("\n=== full round trip: webhook -> agent -> reply ===")
    from myriaxon.config import resolve_config
    from rich.console import Console

    config = resolve_config(Console(quiet=True))
    provider_name = config.provider
    print(f"    provider under test: {provider_name}")
    if provider_name != "netra":
        skip("full round trip", f"provider is {provider_name}, and live testing must use Netra")
    else:
        try:
            run_round_trip(provider, config, check, skip)
        except Exception as exc:  # noqa: BLE001
            import traceback

            traceback.print_exc()
            check("the round trip completed", False, f"{type(exc).__name__}: {exc}")

    provider.stop()
    print(f"\n{len(passed)} passed, {len(failed)} failed"
          + (f", {len(skipped)} skipped" if skipped else ""))
    if failed:
        print("failed:", failed)
        return 1
    return 0


def run_round_trip(provider, config, check, skip) -> None:
    """Drive one real agent turn from an inbound webhook and send the reply back."""
    from rich.console import Console

    from myriaxon.messaging import (
        MessagingClient,
        MessagingConfig,
        format_turn_summary,
        is_allowed,
        parse_inbound,
    )
    from myriaxon.settings import load_settings

    sender_number = "+15551234567"
    webhook = {"From": f"whatsapp:{sender_number}", "Body": "reply with exactly the word pong and nothing else"}

    inbound = MessagingConfig(
        provider="twilio", enabled=True, allow=[sender_number], notify=f"whatsapp:{sender_number}",
        send_url=f"{provider.url}/send", account_sid="AC1", auth_token="t", from_number="whatsapp:+1",
    )
    sender, text = parse_inbound(inbound, webhook)
    check("the webhook parsed", text.startswith("reply with"), text)

    if not is_allowed(inbound, sender):
        skip("round trip", "the sender was not on the allowlist")
        return
    check("the sender passed the allowlist", True)

    console = Console(quiet=True)
    settings = load_settings()
    # Built through _build_agent, the same constructor the CLI and the dashboard use, so this exercises
    # real client construction (config.client does not exist; the builder owns that).
    from myriaxon.cli import _build_agent
    from myriaxon.scheduler import Scheduler
    from myriaxon.subagents import discover_subagent_types
    from myriaxon.skills import discover_skills

    agent = _build_agent(
        config,
        config.model,
        console,
        True,          # yolo
        None,          # mcp_bridge
        [],            # mcp_tool_defs
        [],            # url_mcp_servers
        discover_skills(),
        discover_subagent_types(),
        {},            # hooks
        "",            # project instructions
        Scheduler(),
        settings,
        max_tokens=64,
        mode="auto_edit",
    )
    # run_turn streams to the console and returns nothing; the reply is read back from the agent, which
    # is what the dashboard's console adapter does too.
    agent.run_turn(text)
    reply = str(agent.last_assistant_text() or "").strip()
    print(f"    agent replied: {reply[:80]!r}")
    check("the agent produced a reply", bool(reply), "empty reply")

    client = MessagingClient(inbound)
    sent = client.send_safe(format_turn_summary(text, reply))
    check("the reply was sent back to the provider", sent, client.last_error)
    check("the provider received the reply", bool(provider.sent), provider.sent)
    if provider.sent:
        last = provider.sent[-1]
        # Twilio's send fields are capitalised (To/From/Body); the generic provider uses lowercase.
        recipient = last.get("to") or last.get("To")
        check("the reply went to the sender", recipient == f"whatsapp:{sender_number}", last)
        check("the reply body carries the agent's words",
              "pong" in str(last.get("text") or last.get("Body") or "").lower(), last)


if __name__ == "__main__":
    raise SystemExit(main())
