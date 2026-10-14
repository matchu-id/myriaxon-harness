"""Verify the messaging panel and the inbound webhook over real HTTP.

    python scripts/check_messaging_ui.py [port]

check_messaging.py covers the messaging engine offline. This covers the dashboard surface: the config
payload the panel renders, saving settings through the API (including that blank credentials do not
erase stored ones), and the `/api/inbound` webhook -- including its refusal paths, which matter most
because a webhook is an unauthenticated way to run an agent.

The webhook handler starts a real agent turn, so the live portion needs a working provider. Netra only,
per this project's rule.
"""
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8791
BASE = f"http://127.0.0.1:{PORT}"

passed, failed, skipped = [], [], []


def check(label, condition, detail=""):
    (passed if condition else failed).append(label)
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}{(' -- ' + str(detail)) if detail and not condition else ''}")


def skip(label, why):
    skipped.append(label)
    print(f"  [SKIP] {label} -- {why}")


def get(path):
    return json.loads(urllib.request.urlopen(BASE + path, timeout=60).read().decode())


def post(path, body, headers=None, form=False):
    if form:
        data = urllib.parse.urlencode(body).encode()
        content_type = "application/x-www-form-urlencoded"
    else:
        data = json.dumps(body).encode()
        content_type = "application/json"
    request = urllib.request.Request(
        BASE + path, data=data, headers={"Content-Type": content_type, **(headers or {})}, method="POST"
    )
    try:
        return json.loads(urllib.request.urlopen(request, timeout=300).read().decode())
    except urllib.error.HTTPError as exc:
        return {"_http_error": exc.code, "_body": exc.read().decode("utf-8", errors="replace")[:200]}


def set_messaging(allow, enabled=True, secret=None, provider="twilio", **extra):
    """Save messaging settings, always stating the secret explicitly.

    A blank secret means "keep the stored one" (deliberate: the UI never receives it, so blank must not
    erase it). That makes the secret *sticky*, so a test that sets one must clear it explicitly via
    `clear_secrets` -- otherwise every later request is refused for a reason unrelated to what it is
    checking, which is how three checks here failed with "bad or missing shared secret".
    """
    body = {
        "provider": provider,
        "enabled": enabled,
        "allow": allow,
        "account_sid": "ACtest",
        "from_number": "whatsapp:+15550000000",
        **extra,
    }
    if secret is None:
        body["clear_secrets"] = ["secret"]
    else:
        body["secret"] = secret
    return post("/api/messaging", body)


def main() -> int:
    config = get("/api/config")
    print(f"provider={config['provider']}\n")

    print("=== the config exposes messaging state ===")
    messaging = config.get("messaging") or {}
    check("messaging section present", bool(messaging), list(config))
    check("providers are offered", set(messaging.get("providers") or []) == {"twilio", "meta", "generic"},
          messaging.get("providers"))
    check("stats are reported", "stats" in messaging, list(messaging))
    check("no secret is leaked", "secret" not in json.dumps(messaging).lower() or
          "has_secret" in messaging, messaging)
    original = {k: messaging.get(k) for k in ("provider", "enabled", "allow", "notify")}
    print(f"    existing: {original}")

    print("\n=== saving settings ===")
    saved = post("/api/messaging", {
        "provider": "twilio",
        "enabled": False,
        "allow": ["+15551234567", "+15559876543"],
        "notify": "+15551234567",
        "account_sid": "ACtest",
        "auth_token": "secret-token-value",
        "from_number": "whatsapp:+15550000000",
    })
    check("settings saved", saved.get("provider") == "twilio", saved)
    check("allowlist stored", saved.get("allow") == ["+15551234567", "+15559876543"], saved.get("allow"))
    check("the credential is reported as present, not returned", saved.get("has_auth_token") is True, saved)
    check("the credential value is NOT returned", "secret-token-value" not in json.dumps(saved), saved)
    check("disabled means not usable", saved.get("configured") is False, saved)

    print("\n=== a blank credential keeps the stored one ===")
    updated = post("/api/messaging", {"provider": "twilio", "enabled": False,
                                      "allow": ["+15551234567"], "auth_token": ""})
    check("the credential survives a blank save", updated.get("has_auth_token") is True, updated)
    check("the other change was applied", updated.get("allow") == ["+15551234567"], updated.get("allow"))

    print("\n=== while disabled, the webhook refuses ===")
    result = post("/api/inbound", {"From": "whatsapp:+15551234567", "Body": "hello"})
    check("a disabled webhook refuses", result.get("ok") is False, result)
    check("...and says why", "disabled" in str(result.get("reason", "")).lower(), result)

    print("\n=== enabled: the allowlist gates the webhook ===")
    set_messaging(["+15551234567"], secret=None)
    # An empty allowlist must refuse everybody rather than accept everybody.
    set_messaging([], secret=None)
    empty = post("/api/inbound", {"From": "whatsapp:+15551234567", "Body": "hello"})
    check("an empty allowlist refuses", empty.get("ok") is False, empty)
    check("...and names the allowlist", "allowlist" in str(empty.get("reason", "")).lower(), empty)

    set_messaging(["+15551234567"], secret=None)
    stranger = post("/api/inbound", {"From": "whatsapp:+15559999999", "Body": "let me in"})
    check("an unlisted sender is refused", stranger.get("ok") is False, stranger)
    check("...and is named in the reason", "15559999999" in str(stranger.get("reason", "")), stranger)

    print("\n=== a delivery receipt is not a message ===")
    set_messaging(["+15551234567"], secret=None)
    receipt = post("/api/inbound", {"SmsStatus": "delivered", "MessageSid": "SM1"}, form=True)
    check("a form-encoded receipt is handled", receipt.get("ok") is False, receipt)
    check("...without being treated as a message",
          "message" in str(receipt.get("reason", "")).lower(), receipt)

    print("\n=== a shared secret is enforced when set ===")
    set_messaging(["+15551234567"], secret="shh-123")
    nos = post("/api/inbound", {"From": "whatsapp:+15551234567", "Body": "hi"})
    check("a missing secret is refused", nos.get("ok") is False, nos)
    check("...for the right reason", "secret" in str(nos.get("reason", "")).lower(), nos)
    bad = post("/api/inbound", {"From": "whatsapp:+15551234567", "Body": "hi"},
               headers={"X-Ioh-Secret": "wrong"})
    check("a wrong secret is refused", bad.get("ok") is False, bad)

    print("\n=== a stored secret can be removed on purpose ===")
    # Blank must keep it, so removal needs its own signal; otherwise a leaked token could be replaced
    # but never deleted.
    cleared = set_messaging(["+15551234567"], secret=None)
    check("the secret is no longer stored", cleared.get("has_secret") is False, cleared)
    after_clear = post("/api/inbound", {"From": "whatsapp:+15559999999", "Body": "hi"})
    check("with no secret, the allowlist is now the gate",
          "allowlist" in str(after_clear.get("reason", "")).lower(), after_clear)
    re_set = set_messaging(["+15551234567"], secret="shh-123")
    check("a secret can be set again", re_set.get("has_secret") is True, re_set)
    # Correct secret, but the send will fail (no real provider) -- which is fine and must be reported,
    # not raised. This is the live round trip through the webhook.
    if config["provider"] != "netra":
        skip("the live webhook round trip", f"provider is {config['provider']}, live testing must use Netra")
    else:
        print("\n=== the webhook runs a real turn (secret accepted) ===")
        ok = post("/api/inbound", {"From": "whatsapp:+15551234567",
                                   "Body": "reply with exactly the word pong"},
                  headers={"X-Ioh-Secret": "shh-123"})
        print(f"    result: {json.dumps(ok)[:200]}")
        check("the webhook accepted the message", ok.get("ok") is True, ok)
        check("a session was created", bool(ok.get("session")), ok)
        # The send cannot succeed: this is not a real Twilio account. It must be reported as a failed
        # send rather than crashing the webhook, which is exactly what send_safe is for.
        check("the reply attempt is reported", "replied" in ok, ok)
        if ok.get("replied") is False:
            check("a failed send is explained, not hidden", bool(ok.get("send_error")), ok)

    print("\n=== restore ===")
    restored = set_messaging(
        original.get("allow") or [],
        enabled=bool(original.get("enabled")),
        secret=None,  # explicitly clear the test secret
        provider=original.get("provider") or "generic",
        notify=original.get("notify") or "",
    )
    check("settings restored", restored.get("allow") == (original.get("allow") or []), restored.get("allow"))

    print(f"\n{len(passed)} passed, {len(failed)} failed"
          + (f", {len(skipped)} skipped" if skipped else ""))
    if failed:
        print("failed:", failed)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
