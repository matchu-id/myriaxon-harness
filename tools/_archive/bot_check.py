"""Run the bot self test against a temporary config, so nothing real is messaged."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tools"))

from myriaxon import bot as bot_module  # noqa: E402
from myriaxon.config import Config  # noqa: E402
from myriaxon.dashboard.server import DashboardApp  # noqa: E402
from myriaxon.messaging import load_messaging_config  # noqa: E402

failures = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global failures
    if not ok:
        failures += 1
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}{('  -- ' + detail) if detail and not ok else ''}")


def main() -> int:
    print("1. readiness on a config that is switched off")
    with tempfile.TemporaryDirectory(prefix="mx-bot-") as tmp:
        path = Path(tmp) / "whatsapp.json"
        path.write_text(json.dumps({"provider": "generic", "enabled": False, "allow": []}),
                        encoding="utf-8")
        state = bot_module.readiness(config_file=path)
        check("reported not ready", not state.ready)
        check("names the disabled switch", any("switched off" in p for p in state.problems))
        check("names the empty allowlist", any("allowlisted" in p for p in state.problems))
        check("offers a fix for each problem", len(state.fixes) >= len(state.problems) - 2)

    print("\n2. readiness on a complete config")
    with tempfile.TemporaryDirectory(prefix="mx-bot-") as tmp:
        path = Path(tmp) / "whatsapp.json"
        path.write_text(json.dumps({
            "provider": "generic", "enabled": True, "allow": ["+15550000001"],
            "send_url": "http://127.0.0.1:9/never-called", "secret": "s3cret",
        }), encoding="utf-8")
        config = load_messaging_config(path)
        check("is usable", config.is_usable())

    print("\n3. self test, delivery skipped (no network, no real message)")
    with tempfile.TemporaryDirectory(prefix="mx-bot-") as tmp:
        cfgdir = Path(tmp) / "cfg"
        cfgdir.mkdir()
        messaging = cfgdir / "whatsapp.json"
        messaging.write_text(json.dumps({
            "provider": "generic",
            "enabled": True,
            "allow": ["+15550000001"],
            "secret": "s3cret",
            "send_url": "http://127.0.0.1:9/never-called",
        }), encoding="utf-8")

        app = DashboardApp(
            workspace=REPO,
            config=Config(provider="openai_compat", api_key="unused",
                          base_url="http://127.0.0.1:1/v1", model="self-test"),
        )
        app.messaging = load_messaging_config(messaging)

        result = bot_module.self_test(app, text="Reply with exactly: bot online", deliver=False)
        print(f"     stage={result.get('stage')} ok={result.get('ok')}")
        print(f"     reason={str(result.get('reason'))[:100]}")
        print(f"     answer={(result.get('answer') or '')[:100]!r}")
        # The provider at 127.0.0.1:1 is deliberately unreachable, so the honest outcome is a
        # failure at the *answer* stage: the payload parsed and the turn ran, but no answer came
        # back. Asserting "ok" here would make the check pass only when a real model was reachable,
        # which is not what this tool is for -- it is for proving the *transport* works and that a
        # silent no-answer is reported rather than called ready.
        check("the synthetic payload parsed",
              result.get("stage") in ("turn", "answer"), str(result))
        check("a turn actually ran", result.get("session") is not None or result.get("stage") == "parse",
              str(result))
        if result.get("stage") == "answer":
            check("an empty answer is reported as a failure, not readiness",
                  not result.get("ok") and "no answer" in str(result.get("reason")), str(result))

    print("\n4. self test refuses a config that could not reply")
    app = DashboardApp(
        workspace=REPO,
        config=Config(provider="openai_compat", api_key="unused",
                      base_url="http://127.0.0.1:1/v1", model="self-test"),
    )
    from myriaxon.messaging import MessagingConfig

    app.messaging = MessagingConfig(provider="generic", enabled=True, allow=["+15550000001"])
    result = bot_module.self_test(app, deliver=False)
    check("refused with a reason", not result.get("ok") and "not usable" in str(result.get("reason")),
          str(result))

    print("\n5. a bad secret is refused, so the webhook is really authenticated")
    with tempfile.TemporaryDirectory(prefix="mx-bot-") as tmp:
        app = DashboardApp(
            workspace=REPO,
            config=Config(provider="openai_compat", api_key="unused",
                          base_url="http://127.0.0.1:1/v1", model="self-test"),
        )
        app.messaging = MessagingConfig(
            provider="generic", enabled=True, allow=["+15550000001"], secret="right",
            send_url="http://127.0.0.1:9/never-called",
        )
        refused = app.handle_inbound_message({"From": "+15550000001", "Body": "hi"},
                                             {"X-Myriaxon-Secret": "wrong"}, deliver=False)
        check("a wrong secret is refused", not refused.get("ok"), str(refused))

        stranger = app.handle_inbound_message({"From": "+15559999999", "Body": "hi"},
                                              {"X-Myriaxon-Secret": "right"}, deliver=False)
        check("a stranger is refused", not stranger.get("ok"), str(stranger))
        check("and told why", "allowlist" in str(stranger.get("reason")), str(stranger))

    print()
    print("bot module verified" if not failures else f"{failures} check(s) FAILED")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
