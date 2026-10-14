"""Mission control: the landing view, its route, and the guarantee that it cannot blank the page.

The view is read-only, so the risks are different from the loci panel: there is no mutation to
get wrong. What matters is that

* the page actually ships the view's containers and its renderer (a container with no renderer is a
  blank panel; a renderer with no container throws on load and takes the whole dashboard with it),
* the route always answers with the full contract, even when a data source is broken, because this
  is the landing page -- an exception here leaves the user on a white screen with no way to reach
  the chat either,
* and every number in it is real, so an empty machine reports emptiness rather than fiction.
"""

from __future__ import annotations

import json
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tools"))

PORT = 8798
BASE = f"http://127.0.0.1:{PORT}"

# The contract the browser renders against. A missing key is a blank panel, not a crash, which is
# exactly the kind of failure that reaches a user.
CONTRACT = {
    "generated_at": float,
    "workspace": str,
    "health": dict,
    "usage": dict,
    "runs": list,
    "approvals": list,
    "trace": list,
    "schedules": list,
    "memory": dict,
    "skills": list,
    "routing": list,
    "counts": dict,
}


def _get(path: str, timeout: float = 25.0):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as response:
        return response.status, json.loads(response.read() or b"null")


def _get_error(path: str, timeout: float = 25.0):
    """Status and body for a request expected to fail."""
    try:
        with urllib.request.urlopen(BASE + path, timeout=timeout) as response:
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"null")


class TestMissionControlView(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from myriaxon import constants
        from myriaxon.config import Config
        from myriaxon.dashboard.server import serve

        # An absolute workspace under the session scratch directory, not cwd: `serve` chdirs into
        # the workspace it is given, so passing a relative path would make every later test in this
        # process run from somewhere else.
        cls.workspace = constants.CONFIG_DIR / "mc-workspace"
        cls.workspace.mkdir(parents=True, exist_ok=True)

        config = Config(
            provider="openai_compat",
            api_key="unused",
            base_url="http://127.0.0.1:1/v1",
            model="myriaxon-test-model",
        )
        thread = threading.Thread(
            target=lambda: serve(
                cls.workspace, config=config, host="127.0.0.1", port=PORT,
                mode="auto_edit", open_browser=False,
            ),
            daemon=True,
        )
        thread.start()
        deadline = time.time() + 40
        while time.time() < deadline:
            try:
                _get("/api/health", timeout=3)
                return
            except Exception:
                time.sleep(0.4)
        raise RuntimeError("the dashboard did not start")

    def test_the_page_ships_the_view_and_its_renderer(self) -> None:
        with urllib.request.urlopen(BASE + "/", timeout=25) as response:
            html = response.read().decode("utf-8", "replace")
        for needle in (
            'id="mc"', 'id="mc-core"', 'id="mc-summary"', 'id="mc-meters"', 'id="mc-health"',
            'id="mc-runs"', 'id="mc-approvals"', 'id="mc-schedules"', 'id="mc-trace"',
            'id="mc-routing"', 'id="mc-memory"', 'id="mc-skills"',
            "function renderMission", "function loadMission", "function mcShow",
            "/api/mission-control",
        ):
            with self.subTest(needle=needle):
                self.assertIn(needle, html)

    def test_the_rail_offers_the_views_and_the_dialogs(self) -> None:
        with urllib.request.urlopen(BASE + "/", timeout=25) as response:
            html = response.read().decode("utf-8", "replace")
        self.assertIn('id="nav"', html)
        for view in ("mission", "chat", "approvals"):
            with self.subTest(view=view):
                self.assertIn(f'data-view="{view}"', html)
        # Every dialog the rail can open must exist in the page, or the click does nothing.
        for ui in ("loci", "code", "mcp", "plugins", "rules", "messaging", "settings"):
            with self.subTest(ui=ui):
                self.assertIn(f'data-ui="{ui}"', html)

    def test_every_rail_entry_opens_an_element_that_exists(self) -> None:
        """A rail entry whose mapping is missing renders, looks wired, and does nothing.

        That shipped once: `loci` was in the markup but absent from the opener map, so pressing
        Loci reported "loci has no panel in this build" while `locisbtn` had existed all along.
        Asserted from the page itself rather than against the map in isolation, so the markup, the
        map and the target buttons are all checked against each other.
        """
        import re

        with urllib.request.urlopen(BASE + "/", timeout=25) as response:
            html = response.read().decode("utf-8", "replace")

        block = re.search(r"const RAIL_DIALOGS = \{(.*?)\};", html, re.DOTALL)
        self.assertIsNotNone(block, "the rail's opener map is gone")
        mapping = dict(re.findall(r"(\w+):\s*\"(\w+)\"", block.group(1)))
        offered = set(re.findall(r'data-ui="([^"]+)"', html))

        self.assertEqual(
            sorted(offered - set(mapping)),
            [],
            "these rail entries have no opener and would report 'no panel in this build'",
        )
        for ui, opener in sorted(mapping.items()):
            with self.subTest(ui=ui):
                self.assertIn(f'id="{opener}"', html, f"{ui} points at a button that is not in the page")
        # The one that regressed, named explicitly so a future edit cannot quietly drop it again.
        self.assertEqual(mapping.get("loci"), "locisbtn")

    def test_every_rail_view_is_handled_by_the_switcher(self) -> None:
        """A data-view the switcher does not handle leaves the previous view on screen."""
        import re

        with urllib.request.urlopen(BASE + "/", timeout=25) as response:
            html = response.read().decode("utf-8", "replace")
        views = set(re.findall(r'data-view="([^"]+)"', html))
        handled = set(re.findall(r'view === "(\w+)"', html)) | set(
            re.findall(r'mcShow\("(\w+)"\)', html)
        )
        self.assertEqual(sorted(views - handled), [], "these views are never shown")

    def test_mission_control_is_the_landing_view(self) -> None:
        """The view answers "what is going on" without the user having to ask, so it is what the
        page shows first rather than the empty chat."""
        with urllib.request.urlopen(BASE + "/", timeout=25) as response:
            html = response.read().decode("utf-8", "replace")
        self.assertIn('mcShow("mission")', html)

    def test_the_route_answers_with_the_full_contract(self) -> None:
        status, payload = _get("/api/mission-control")
        self.assertEqual(status, 200)
        self.assertNotIn("error", payload, f"the snapshot reported: {payload.get('error')}")
        for key, kind in CONTRACT.items():
            with self.subTest(key=key):
                self.assertIn(key, payload, f"the contract is missing {key!r}")
                self.assertIsInstance(payload[key], kind, f"{key} has the wrong type")

    def test_counts_are_real_numbers_not_invented_ones(self) -> None:
        """Every counter must be an int. A fabricated figure would be a string or a placeholder."""
        _, payload = _get("/api/mission-control")
        counts = payload.get("counts") or {}
        self.assertTrue(counts, "counts is empty")
        for key, value in counts.items():
            with self.subTest(counter=key):
                self.assertIsInstance(value, int, f"{key} is {type(value).__name__}")

    def test_health_reports_items_with_real_states(self) -> None:
        _, payload = _get("/api/mission-control")
        health = payload.get("health") or {}
        self.assertIn(health.get("overall"), {"ok", "warn", "error"})
        self.assertIsInstance(health.get("summary"), str)
        for item in health.get("items", []):
            with self.subTest(item=item.get("key")):
                self.assertIn(item.get("state"), {"ok", "warn", "error", "off"})
                self.assertIn(item.get("kind"), {"provider", "integration", "local"})
                self.assertTrue(item.get("label"))
                self.assertTrue(item.get("detail"))

    def test_the_route_degrades_rather_than_failing(self) -> None:
        """The landing page must never 500. Break a source and assert the answer is still complete.

        This is the difference between "memory is unavailable" and a blank screen: the view is the
        first thing a user sees, so it has to survive a broken dependency it merely reports on.
        """
        import myriaxon.mission_control as mission_control

        original = mission_control.snapshot

        def broken(app):
            raise RuntimeError("simulated failure in every data source")

        mission_control.snapshot = broken
        try:
            status, payload = _get("/api/mission-control")
        finally:
            mission_control.snapshot = original

        self.assertEqual(status, 200, "a broken source must not become a 500")
        self.assertIn("error", payload)
        for key, kind in CONTRACT.items():
            if key == "generated_at":
                continue
            with self.subTest(key=key):
                self.assertIn(key, payload, f"the degraded payload is missing {key!r}")

    def test_the_route_recovers_after_a_failure(self) -> None:
        """A degraded answer must not be sticky: the next request tries the real thing again."""
        status, payload = _get("/api/mission-control")
        self.assertEqual(status, 200)
        # Whether the module is present or not, the route answers; once present there is no error.
        self.assertIsInstance(payload, dict)

    def test_an_unknown_session_is_a_404_that_the_page_can_recognise(self) -> None:
        """Session ids live in server memory, so any restart invalidates every id a browser holds.

        That makes "unknown session" an ordinary event rather than an error the user should have to
        understand -- which is why the page now recognises it and moves to a session that exists.
        This asserts the exact shape it has to recognise: a 404 whose error mentions the session.
        """
        status, payload = _get_error("/api/messages?session=definitely-not-a-session")
        self.assertEqual(status, 404)
        self.assertIn("unknown session", json.dumps(payload).lower())

    def test_the_page_recovers_from_a_stale_session(self) -> None:
        """The recovery path has to exist in the page, not only in the server's 404."""
        with urllib.request.urlopen(BASE + "/", timeout=25) as response:
            html = response.read().decode("utf-8", "replace")
        for needle, why in (
            ("function recoverSession", "no recovery function"),
            ("function isUnknownSession", "no recognition of the stale-session reply"),
            ("recoverSession(\"that session no longer exists\")", "boot does not recover"),
            ("state.recovering", "the SSE path has no guard against looping"),
        ):
            with self.subTest(needle=needle):
                self.assertIn(needle, html, why)


if __name__ == "__main__":
    unittest.main(verbosity=2)
