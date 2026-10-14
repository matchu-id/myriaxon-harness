"""End-to-end verification of the Ollama integration, against the running dashboard.

Written because a compile check and a passing test suite do not prove a feature works. Every defect in
this area so far passed both: `ollama` missing from the dashboard's provider table, `row.append(name)`
without a `name`, and a `404 model not found` from an empty model field. All three were found by
looking at the running system.

So this drives the real HTTP surface and asserts on the responses:

  1. the vendor list advertises ollama
  2. `/api/ollama` reports models with a usable context
  3. switching provider through the real chat command succeeds
  4. the model can be set to a specific local model
  5. a real turn completes with no error
  6. the page carries the panel markup and its CSS

Usage:
    python tools/ollama_e2e.py [--base http://127.0.0.1:8765]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

PASS = "  ok   "
FAIL = "  FAIL "


def call(base: str, path: str, payload: dict | None = None, timeout: float = 300.0):
    url = base + path
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"},
        method="POST" if data is not None else "GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
            return response.status, body
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001
        return 0, f"{type(exc).__name__}: {exc}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8765")
    parser.add_argument("--workspace", default=".")
    parser.add_argument("--message", default="Reply with exactly: E2E OK")
    args = parser.parse_args()

    failures: list[str] = []

    def check(label: str, condition: bool, detail: str = "") -> None:
        print(f"{PASS if condition else FAIL}{label}" + (f"  -- {detail}" if detail else ""))
        if not condition:
            failures.append(label)

    # 1 ------------------------------------------------------------------------------------
    status, body = call(args.base, "/api/config", timeout=60)
    config = json.loads(body) if status == 200 else {}
    providers = config.get("providers") or []
    check("vendor list advertises ollama", "ollama" in providers, f"providers={providers}")

    # 2 ------------------------------------------------------------------------------------
    status, body = call(args.base, "/api/ollama", timeout=90)
    probe = json.loads(body) if status == 200 else {}
    models = probe.get("models") or []
    check("ollama endpoint answers", status == 200, f"HTTP {status}")
    check("models are listed", bool(models), f"{len(models)} model(s)")
    check(
        "every model reports a usable context",
        bool(models) and all(m.get("context_usable") for m in models),
        ", ".join(f"{m['name'][:18]}={m.get('context_usable')}" for m in models[:3]),
    )
    check(
        "a capped model says both numbers",
        all(
            (not m.get("context_declared")) or m["context_usable"] <= m["context_declared"]
            for m in models
        ),
        "usable must never exceed declared",
    )

    # 3 ------------------------------------------------------------------------------------
    status, body = call(args.base, "/api/sessions", {"workspace": args.workspace}, timeout=90)
    created = json.loads(body) if status == 200 else {}
    session = (created.get("session") or created).get("id", "")
    check("a session can be created", bool(session), f"id={session}")

    status, body = call(args.base, "/api/chat", {"session": session, "message": "/provider ollama"}, timeout=120)
    switch = json.loads(body) if status == 200 else {}
    output = str(switch.get("output", ""))
    check(
        "/provider ollama is accepted",
        status == 200 and "unknown provider" not in output,
        output.replace("\n", " | ")[:120],
    )
    check(
        "the switch reports a usable state",
        "model    " in output and output.split("model    ")[1].split("\n")[0].strip() != "",
        "a switch that leaves the model empty makes the next turn 404",
    )

    # 4 ------------------------------------------------------------------------------------
    target = models[0]["name"] if models else ""
    if target:
        status, body = call(args.base, "/api/model", {"session": session, "model": target}, timeout=90)
        check("the model can be set", status == 200, f"HTTP {status} -> {target[:40]}")

    # 5 ------------------------------------------------------------------------------------
    status, body = call(args.base, "/api/chat", {"session": session, "message": args.message}, timeout=120)
    check("a turn is accepted", status == 200, f"HTTP {status}")

    deadline = time.time() + 300
    final: dict = {}
    while time.time() < deadline:
        time.sleep(5)
        status, body = call(args.base, "/api/sessions", timeout=60)
        if status != 200:
            continue
        rows = json.loads(body)
        rows = rows.get("sessions", rows) if isinstance(rows, dict) else rows
        mine = [r for r in rows if r.get("id") == session]
        if mine and not mine[0].get("busy"):
            final = mine[0]
            break

    check("the turn finished", bool(final), "no session state after 300s")
    check(
        "the turn produced no error",
        final.get("error") in (None, ""),
        str(final.get("error"))[:160],
    )
    check(
        "the session stayed on ollama",
        final.get("provider") == "ollama",
        f"provider={final.get('provider')} model={final.get('model')}",
    )
    # `turn_count` counts agent turns only -- `session.py` increments it after `run_turn` returns, and a
    # slash command takes the command branch and never reaches it. So exactly one message was sent and
    # exactly one turn is expected. Asserting `>= 2` here was wrong when this check was first written,
    # which is worth stating: the failure was in the assertion, not the code.
    check("exactly the one real turn was counted", final.get("turn_count") == 1, f"turns={final.get('turn_count')}")

    # 6 ------------------------------------------------------------------------------------
    status, page = call(args.base, "/", timeout=60)
    check("the page loads", status == 200, f"HTTP {status}")
    for needle, label in (
        ('id="ollamapanel"', "panel markup"),
        ('id="ollamalist"', "panel list container"),
        ("row.append(name, info)", "the model name is attached"),
        ("max-width: 420px", "the compact panel width"),
    ):
        check(f"page carries {label}", needle in page)

    print()
    if failures:
        print(f"{len(failures)} check(s) failed:")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
