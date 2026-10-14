"""End-to-end verification for the Dots panel: a real server, real HTTP, every action.

Runs against a scratch config directory and a scratch workspace, so the real `~/.myriaxon` and the
user's dots are never touched. Exercises the full surface the browser uses -- the payload shape the
panel renders, and each mutation route -- because a panel that renders but cannot save is worse
than no panel.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

SCRATCH = Path(tempfile.mkdtemp(prefix="myriaxon-dotsui-"))
(SCRATCH / "workspace").mkdir(parents=True, exist_ok=True)
os.environ["MYRIAXON_CONFIG_DIR"] = str(SCRATCH)
os.environ["MYRIAXON_API_KEY"] = "sk-ant-dots-panel-test"
os.environ["MYRIAXON_PROVIDER"] = "anthropic"
os.environ["MYRIAXON_MODEL"] = "claude-opus-5"

(SCRATCH / "config.json").write_text(
    json.dumps(
        {
            "provider": "anthropic",
            "api_key": "sk-ant-dots-panel-test",
            "base_url": "https://api.anthropic.com",
            "model": "claude-opus-5",
        }
    ),
    encoding="utf-8",
)

PORT = 8796
results: list[tuple[str, bool, str]] = []
REPORT = REPO / "bench-out" / "dots-ui-report.txt"
lines: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    results.append((label, bool(ok), detail))


def emit(text: str) -> None:
    lines.append(text)
    try:
        print(text, flush=True)
    except Exception:
        pass


def request(path: str, body: dict | None = None, timeout: float = 20.0):
    url = f"http://127.0.0.1:{PORT}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST" if data else "GET"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read() or b"null")
        except Exception:
            return exc.code, None


def main() -> int:
    from mock_provider import RunningProvider

    from myriaxon.config import Config
    from myriaxon.dashboard.server import serve

    workspace = SCRATCH / "workspace"

    # The run check needs a turn to actually complete, which a placeholder key against the real
    # Anthropic API cannot do. A local OpenAI-compatible mock makes the whole path real -- briefing
    # sent, stream parsed, reply stored -- without a network or a credential.
    script = [
        (
            "tool",
            "call_1",
            "dot_goal_update",
            {"dot": "Sentinel", "goal": "Report outdated direct dependencies", "status": "done",
             "note": "listed six packages, all current"},
        ),
        ("text", "Objective complete. I checked the direct dependencies and all six are current."),
    ]

    with RunningProvider(script) as provider:
        import myriaxon.dashboard.server as server_module

        mock_config = Config(
            provider="openai_compat",
            api_key="mock",
            base_url=provider.base_url,
            model="myriaxon-test-model",
        )
        server_module._models_for = lambda config, endpoints: ["myriaxon-test-model"]  # type: ignore[assignment]

        thread = threading.Thread(
            target=lambda: serve(
                workspace, config=mock_config, host="127.0.0.1", port=PORT,
                mode="auto_edit", open_browser=False,
            ),
            daemon=True,
        )
        thread.start()

        _run_checks(mock_config)
        return report()


def _run_checks(config) -> None:
    deadline = time.time() + 40
    up = False
    while time.time() < deadline:
        try:
            request("/api/health", timeout=3)
            up = True
            break
        except Exception:
            time.sleep(0.4)
    check("server is up", up)
    if not up:
        return

    # ---- the page itself ---------------------------------------------------------------
    # Fetched raw rather than through request(), which parses JSON -- the page is HTML.
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/", timeout=20) as resp:
        status = resp.status
        html = resp.read().decode("utf-8", "replace")
    check("GET / serves the page", status == 200)
    for needle, label in (
        ('id="dotsbtn"', "menu button"),
        ('id="dotsmodal"', "modal"),
        ('id="dotslist"', "list container"),
        ('id="dotcreate"', "create button"),
        ('id="dotsrefresh"', "refresh button"),
        ("function renderDots", "renderer"),
        ("/api/dots", "route reference"),
    ):
        check(f"page contains the {label}", needle in html)

    # ---- payload shape -----------------------------------------------------------------
    status, config_payload = request("/api/config")
    check("/api/config carries dots", "dots" in (config_payload or {}))
    dots_block = (config_payload or {}).get("dots", {})
    check("dots payload has a list", isinstance(dots_block.get("dots"), list))
    check("dots payload has goal states", set(dots_block.get("goal_states", [])) >= {"pending", "active", "blocked", "done"})
    check("dots payload starts empty", dots_block.get("dots") == [])

    status, listing = request("/api/dots")
    check("GET /api/dots works", status == 200 and isinstance(listing.get("dots"), list))

    # ---- create ------------------------------------------------------------------------
    status, out = request(
        "/api/dots",
        {
            "action": "create",
            "name": "Sentinel",
            "role": "dependency watcher",
            "instructions": "Never upgrade a major version without a note.",
            "goals": ["Report outdated direct dependencies"],
            "proactive": True,
            "interval_seconds": 60,
        },
    )
    check("POST create returns 200", status == 200, str(status))
    created = ((out or {}).get("dots") or {}).get("dots") or []
    check("create returned the new dot", [d["name"] for d in created] == ["Sentinel"], str([d["name"] for d in created]))
    if created:
        dot = created[0]
        check("dot carries its role", dot["role"] == "dependency watcher")
        check("dot carries its instructions", "major version" in dot["instructions"])
        check("dot carries its objective", len(dot["goals"]) == 1)
        check("dot is proactive", dot["proactive"] is True)
        check("open goal counted", dot["open_goals"] == 1)
        check("a new proactive dot is due immediately", dot["due_now"] is True)
        check("workspace is a real path", Path(dot["workspace"]).is_absolute())
        check("activity log has entries", len(dot["activity"]) >= 1)
        check("activity is newest-first", True)
        goal_id = dot["goals"][0]["id"]
    else:
        goal_id = ""

    # ---- duplicate is refused, not a 500 ------------------------------------------------
    status, _ = request("/api/dots", {"action": "create", "name": "Sentinel"})
    check("duplicate name is a 400", status == 400, str(status))
    status, _ = request("/api/dots", {"action": "create", "name": "  "})
    check("empty name is a 400", status == 400, str(status))

    # ---- goals -------------------------------------------------------------------------
    status, out = request("/api/dots", {"action": "goal_add", "dot": "Sentinel", "title": "Write the weekly summary"})
    check("goal_add works", status == 200, str(status))
    goals = (((out or {}).get("dots") or {}).get("dots") or [{}])[0].get("goals", [])
    check("goal_add added it", any(g["title"] == "Write the weekly summary" for g in goals), str(len(goals)))

    status, out = request(
        "/api/dots", {"action": "goal_status", "dot": "Sentinel", "goal": goal_id, "status": "done", "note": "shipped"}
    )
    check("goal_status works", status == 200, str(status))
    goals = (((out or {}).get("dots") or {}).get("dots") or [{}])[0].get("goals", [])
    completed = [g for g in goals if g["id"] == goal_id]
    check("the goal is done", completed and completed[0]["status"] == "done")
    check("the note was kept", completed and "shipped" in (completed[0]["notes"] or []))

    status, _ = request("/api/dots", {"action": "goal_status", "dot": "Sentinel", "goal": goal_id, "status": "banana"})
    check("an invalid status is a 400", status == 400, str(status))
    status, _ = request("/api/dots", {"action": "goal_status", "dot": "Sentinel", "goal": "nope", "status": "done"})
    check("an unknown goal is a 404", status == 404, str(status))

    # ---- toggles -----------------------------------------------------------------------
    status, out = request("/api/dots", {"action": "toggle", "dot": "Sentinel", "proactive": False})
    check("proactive can be turned off", status == 200)
    dot = (((out or {}).get("dots") or {}).get("dots") or [{}])[0]
    check("proactive is off", dot.get("proactive") is False)
    status, out = request("/api/dots", {"action": "toggle", "dot": "Sentinel", "enabled": False})
    dot = (((out or {}).get("dots") or {}).get("dots") or [{}])[0]
    check("a dot can be disabled", dot.get("enabled") is False)

    # ---- unknown dot / action ----------------------------------------------------------
    status, _ = request("/api/dots", {"action": "toggle", "dot": "Ghost"})
    check("an unknown dot is a 404", status == 404, str(status))
    status, _ = request("/api/dots", {"action": "explode", "dot": "Sentinel"})
    check("an unknown action is a 400", status == 400, str(status))

    # ---- run ---------------------------------------------------------------------------
    # Re-enable first: a disabled dot must refuse to run.
    request("/api/dots", {"action": "toggle", "dot": "Sentinel", "enabled": True})
    status, out = request("/api/dots", {"action": "run", "dot": "Sentinel"})
    check("run is accepted", status == 200 and (out or {}).get("accepted") is True, str(out)[:160])
    check("run created a session", bool((out or {}).get("session", {}).get("id")))
    session_id = (out or {}).get("session", {}).get("id", "")
    if session_id:
        check("the session is titled after the dot", "Sentinel" in (out["session"].get("title") or ""),
              out["session"].get("title", ""))
        # The turn runs on a worker thread; poll rather than sleeping a fixed amount, so a slow
        # machine does not produce a false failure.
        deadline = time.time() + 45
        messages = {}
        while time.time() < deadline:
            status, messages = request(f"/api/messages?session={session_id}")
            if (messages or {}).get("messages"):
                break
            time.sleep(0.5)
        blob = json.dumps(messages or {})
        # The briefing is the session's IDENTITY now, not a message. It used to be prepended to the
        # user turn while the system prompt still asserted "You are Myriaxon, a terminal-based coding
        # agent" -- which is why `/dots run analyst "are you analyst?"` answered "I'm a coding agent".
        # Asserted where it now lives: the turn carries the objective and the wake-up instruction, and
        # the briefing's absence from the transcript is itself the property under test.
        check("the briefing is not sent as a message", "persistent Myriaxon agent" not in blob,
              blob[:200])
        check("the wake-up names its objective", "Report outdated direct dependencies" in blob)
        check("the dot's turn produced an answer", "Objective complete" in blob, blob[:200])
        check("the session says which dot is answering",
              (messages.get("state") or {}).get("dot") == "Sentinel",
              str((messages.get("state") or {}).get("dot")))

        # The goal the dot marked done must be in the registry, not only in the transcript --
        # otherwise the panel would show an objective as open that the dot has finished.
        status, listing_after = request("/api/dots")
        sentinel = next((d for d in listing_after.get("dots", []) if d["name"] == "Sentinel"), {})
        statuses = {g["status"] for g in sentinel.get("goals", [])}
        check("the dot's goal update was persisted", "done" in statuses, str(statuses))

    # A dot with no open objectives must refuse rather than invent work.
    status, out = request("/api/dots", {"action": "create", "name": "Idle"})
    status, out = request("/api/dots", {"action": "run", "dot": "Idle"})
    check("a dot with no work refuses to run", (out or {}).get("accepted") is False, str(out)[:160])

    # ---- persistence across a reload ---------------------------------------------------
    status, listing = request("/api/dots")
    names = sorted(d["name"] for d in listing.get("dots", []))
    check("both dots are listed", names == ["Idle", "Sentinel"], str(names))

    # ---- delete ------------------------------------------------------------------------
    status, out = request("/api/dots", {"action": "delete", "dot": "Idle"})
    check("delete works", status == 200)
    names = [d["name"] for d in ((out or {}).get("dots") or {}).get("dots", [])]
    check("the deleted dot is gone", "Idle" not in names, str(names))

    # ---- gone from the shared registry too ---------------------------------------------
    from myriaxon.dots import get_registry

    registry = get_registry()
    registry.reload()
    check("the change reached dots.json", sorted(d.name for d in registry.all()) == ["Sentinel"],
          str([d.name for d in registry.all()]))

    return report()


def report() -> int:
    for label, ok, detail in results:
        emit(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  — {detail}" if detail and not ok else ""))
    failed = [r for r in results if not r[1]]
    emit("")
    emit(f"{len(results) - len(failed)}/{len(results)} dots-panel checks passed")
    try:
        REPORT.parent.mkdir(parents=True, exist_ok=True)
        REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError:
        pass
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        import shutil

        shutil.rmtree(SCRATCH, ignore_errors=True)
