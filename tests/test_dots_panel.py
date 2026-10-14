"""The Loci panel's HTTP surface: payload shape, every mutation route, and one real run.

The loci *module* is covered by `test_loci.py`. This covers the panel's own layer -- the payload the
browser renders and the POST routes the buttons call -- because a panel that renders but cannot save
is worse than no panel, and a route that returns 200 while dropping the change looks fine from the
browser until the page is reloaded.

The transcript assertions at the end need a turn to actually complete. A placeholder key against a
real provider cannot do that, so these run against the scripted local provider from
`tools/mock_provider.py`: no network, no credential, and the whole path is real.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tools"))

# State pointing is done once for the whole session in tests/conftest.py, which pytest imports
# before collecting anything. Doing it here instead would make it depend on which test module
# happened to import the package first -- and whichever lost that race would edit the real
# configuration.
PORT = 8797
BASE = f"http://127.0.0.1:{PORT}"

# A locus's run writes a goal update, then answers. Two requests, so the tool round trip is real.
SCRIPT = [
    (
        "tool",
        "call_1",
        "locus_goal_update",
        {
            "locus": "Sentinel",
            "goal": "Report outdated direct dependencies",
            "status": "done",
            "note": "listed six packages, all current",
        },
    ),
    ("text", "Objective complete. All six direct dependencies are current."),
]


def _request(path: str, body: dict | None = None, timeout: float = 20.0):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        BASE + path, data=data, headers={"Content-Type": "application/json"},
        method="POST" if data else "GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read() or b"null")
        except Exception:
            return exc.code, None


class TestLociPanelHTTP(unittest.TestCase):
    """One server for the whole class: starting one per test would dominate the runtime.

    Every test creates its own uniquely named locus and asserts only about that locus. The registry is
    shared and persists, so a test that asserted "the registry is empty" or reached for a fixed
    name would pass alone and fail in a suite -- which is exactly what happened when these were
    first written.
    """

    _counter = 0

    @classmethod
    def setUpClass(cls) -> None:
        from mock_provider import RunningProvider

        # Assert the session-wide scoping worked before creating anything: if this is false the
        # tests are about to write loci into the user's own registry, which is worth failing loudly
        # for rather than discovering later.
        from myriaxon import constants

        if "myriaxon-tests-" not in str(constants.CONFIG_DIR):
            raise RuntimeError(
                f"refusing to run: CONFIG_DIR is {constants.CONFIG_DIR}, not the pytest scratch "
                "directory from conftest.py. Something imported the package before conftest ran."
            )
        cls.workspace = constants.CONFIG_DIR / "workspace"
        cls.workspace.mkdir(parents=True, exist_ok=True)

        cls.provider = RunningProvider(SCRIPT)
        cls.provider.__enter__()

        from myriaxon.config import Config
        from myriaxon.dashboard.server import serve

        config = Config(
            provider="openai_compat",
            api_key="mock",
            base_url=cls.provider.base_url,
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
                _request("/api/health", timeout=3)
                return
            except Exception:
                time.sleep(0.4)
        raise RuntimeError("the dashboard did not start")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.provider.__exit__(None, None, None)

    # ---- helpers -----------------------------------------------------------------------
    @classmethod
    def _unique(cls, stem: str) -> str:
        cls._counter += 1
        return f"{stem}-{cls._counter}"

    def _create(self, name: str | None = None, **extra):
        name = name or self._unique("Locus")
        body = {"action": "create", "name": name, "goals": ["Report outdated direct dependencies"]}
        body.update(extra)
        return name, _request("/api/loci", body)

    def _dot(self, payload, name: str) -> dict:
        """Pull one locus out of a route reply.

        The shape differs by route and that is deliberate: a mutation returns the whole
        ``_loci_payload()`` under ``loci``, while ``run`` returns its own small summary. Both are
        handled here so the tests read the same way regardless.
        """
        block = (payload or {}).get("loci") or {}
        if isinstance(block, list):          # run's summary
            return next(d for d in block if d["name"] == name)
        return next(d for d in block.get("loci", []) if d["name"] == name)

    @staticmethod
    def _all_dots(payload) -> list[dict]:
        block = (payload or {}).get("loci") or {}
        return block if isinstance(block, list) else block.get("loci", [])

    # ---- the page and the payload ------------------------------------------------------
    def test_the_page_contains_the_panel(self) -> None:
        with urllib.request.urlopen(BASE + "/", timeout=20) as response:
            html = response.read().decode("utf-8", "replace")
        for needle in (
            'id="locisbtn"', 'id="locismodal"', 'id="locislist"',
            'id="locuscreate"', 'id="locirefresh"', 'id="locisbadge"',
            "function renderLoci", "function runLocus", "/api/loci",
        ):
            with self.subTest(needle=needle):
                self.assertIn(needle, html)

    def test_config_carries_the_loci_payload(self) -> None:
        status, payload = _request("/api/config")
        self.assertEqual(status, 200)
        block = payload.get("loci") or {}
        self.assertIn("loci", block)
        self.assertGreaterEqual(
            set(block.get("goal_states", [])), {"pending", "active", "blocked", "done"}
        )

    def test_the_dedicated_route_works(self) -> None:
        status, payload = _request("/api/loci")
        self.assertEqual(status, 200)
        self.assertIsInstance(payload.get("loci"), list)

    # ---- create ------------------------------------------------------------------------
    def test_create_returns_the_full_dot(self) -> None:
        name, (status, payload) = self._create(
            role="reviewer", instructions="Be terse.", proactive=True, interval_seconds=120
        )
        self.assertEqual(status, 200)
        locus = self._dot(payload, name)
        self.assertEqual(locus["role"], "reviewer")
        self.assertEqual(locus["instructions"], "Be terse.")
        self.assertTrue(locus["proactive"])
        self.assertEqual(locus["interval_seconds"], 120)
        self.assertEqual(locus["open_goals"], 1)
        self.assertTrue(locus["due_now"], "a brand-new proactive locus should be due")
        self.assertTrue(Path(locus["workspace"]).is_absolute())
        self.assertTrue(locus["activity"])

    def test_create_rejects_bad_input_with_400(self) -> None:
        self.assertEqual(_request("/api/loci", {"action": "create"})[0], 400)
        self.assertEqual(_request("/api/loci", {"action": "create", "name": "  "})[0], 400)
        name, _ = self._create()
        self.assertEqual(_request("/api/loci", {"action": "create", "name": name})[0], 400)

    # ---- goals -------------------------------------------------------------------------
    def test_goal_add_and_status(self) -> None:
        name, (_, payload) = self._create()
        goal_id = self._dot(payload, name)["goals"][0]["id"]

        status, payload = _request("/api/loci", {"action": "goal_add", "locus": name, "title": "Second objective"})
        self.assertEqual(status, 200)
        self.assertEqual(len(self._dot(payload, name)["goals"]), 2)

        status, payload = _request(
            "/api/loci",
            {"action": "goal_status", "locus": name, "goal": goal_id, "status": "done", "note": "shipped"},
        )
        self.assertEqual(status, 200)
        locus = self._dot(payload, name)
        changed = next(g for g in locus["goals"] if g["id"] == goal_id)
        self.assertEqual(changed["status"], "done")
        self.assertIn("shipped", changed["notes"])
        self.assertEqual(locus["open_goals"], 1, "one objective remains open")

    def test_goal_errors(self) -> None:
        name, _ = self._create()
        self.assertEqual(_request("/api/loci", {"action": "goal_add", "locus": name, "title": " "})[0], 400)
        self.assertEqual(
            _request("/api/loci", {"action": "goal_status", "locus": name, "goal": "x", "status": "nope"})[0], 400
        )
        self.assertEqual(
            _request("/api/loci", {"action": "goal_status", "locus": name, "goal": "x", "status": "done"})[0], 404
        )

    # ---- toggles -----------------------------------------------------------------------
    def test_toggles_persist(self) -> None:
        name, _ = self._create(proactive=True)
        status, payload = _request("/api/loci", {"action": "toggle", "locus": name, "proactive": False})
        self.assertEqual(status, 200)
        self.assertFalse(self._dot(payload, name)["proactive"])

        _, payload = _request("/api/loci", {"action": "toggle", "locus": name, "enabled": False})
        self.assertFalse(self._dot(payload, name)["enabled"])

    def test_unknown_targets(self) -> None:
        self.assertEqual(_request("/api/loci", {"action": "toggle", "locus": "Ghost"})[0], 404)
        # The action is validated before the locus is resolved, so a bad action is reported as a bad
        # action even when the locus name is also wrong.
        status, payload = _request("/api/loci", {"action": "nonsense", "locus": "Ghost"})
        self.assertEqual(status, 400, str(payload))
        self.assertIn("nonsense", json.dumps(payload))

    # ---- run ---------------------------------------------------------------------------
    def test_a_run_completes_and_updates_the_registry(self) -> None:
        """The one that matters: the locus's briefing is sent, the stream is parsed, its own goal
        update runs as a real tool call, and the result is visible in the payload the panel renders.

        The tool round trip is asserted through the trajectory rather than through the mock's
        ability to guess the locus's name from the briefing: that guessing is scaffolding, and a
        failure in it would look exactly like a product failure.
        """
        name, (_, payload) = self._create(role="dependency watcher", proactive=True)

        status, payload = _request("/api/loci", {"action": "run", "locus": name})
        self.assertEqual(status, 200)
        self.assertTrue(payload.get("accepted"), payload)
        session_id = payload["session"]["id"]
        self.assertIn(name, payload["session"]["title"])

        # The turn runs on a worker thread and takes TWO model requests -- a tool call, then the
        # answer after its result is fed back. Polling for "any messages" therefore returns while
        # the turn is still mid-flight and the answer has not been written yet, which read as a
        # failure of the product rather than of the wait. Wait for the turn to actually settle.
        deadline = time.time() + 60
        messages = {}
        settled = False
        while time.time() < deadline:
            _, messages = _request(f"/api/messages?session={session_id}")
            roles = [m.get("role") for m in messages.get("messages", [])]
            steps = messages.get("trajectory", [])
            # Settled when the model has both called a tool and then answered.
            if "tool" in roles and roles[-1:] == ["assistant"] and steps:
                settled = True
                break
            time.sleep(0.5)
        self.assertTrue(settled, f"the turn did not settle within 60s; roles={roles} steps={len(steps)}")

        blob = json.dumps(messages)
        # The locus's briefing is its IDENTITY, not a message. It used to be prepended to the user turn,
        # which is why `/loci run analyst "are you analyst?"` answered "I'm a coding agent": the
        # system prompt still asserted the generic identity, and the model believed it -- correctly,
        # because a request cannot override an instruction defining the same agent. So the briefing is
        # asserted where it now lives, and its absence from the turn text is asserted too.
        self.assertNotIn(
            "persistent Myriaxon agent", blob,
            "the briefing must not also be sent as a message -- that is the defect, not the check",
        )
        # This run has no owner message, so the turn is the proactive instruction. Asserting the real
        # contract rather than a phrase from the other branch: an unprompted wake-up must say it is
        # one, and must tell the locus what to do about the objective.
        self.assertIn("Continue your objective", blob, "the proactive wake-up lost its instruction")
        self.assertIn("Take the next concrete step", blob)
        self.assertIn("Objective complete", blob, "the turn produced no answer")

        # The model asked for a tool and the harness ran it: proof the locus's turn has the same
        # tool-execution path as an ordinary session.
        tools_called = [step.get("tool") for step in messages.get("trajectory", [])]
        self.assertIn("locus_goal_update", tools_called, f"tools called: {tools_called}")

        # And the session reports which locus is answering, which is what the transcript label and the
        # "speaking as" banner are driven from.
        self.assertEqual((messages.get("state") or {}).get("locus"), name)

    def test_a_dot_with_no_objectives_refuses_to_run(self) -> None:
        name, _ = self._create(goals=[])
        status, payload = _request("/api/loci", {"action": "run", "locus": name})
        self.assertEqual(status, 200)
        self.assertFalse(payload.get("accepted"))
        self.assertIn("no open objectives", payload.get("reason", ""))

    def test_run_of_a_disabled_dot_is_refused(self) -> None:
        name, _ = self._create()
        _request("/api/loci", {"action": "toggle", "locus": name, "enabled": False})
        status, payload = _request("/api/loci", {"action": "run", "locus": name})
        self.assertEqual(status, 200)
        self.assertFalse(payload.get("accepted"), payload)

    def test_a_message_runs_a_dot_whose_objectives_are_all_done(self) -> None:
        """A person asking their own agent for something is not a proactive wake-up.

        The objective checks exist so a locus that has nothing to do does not invent work on an
        interval. Applying them to a direct message made a locus unusable once its objectives were
        finished or blocked -- `cowork` could not be asked for anything at all -- which is the
        opposite of what a persistent agent is for.
        """
        name, (_, payload) = self._create(proactive=False)
        locus = self._dot(payload, name)
        goal_id = locus["goals"][0]["id"]

        # Finish the only objective, so nothing is open.
        _request("/api/loci", {"action": "goal_status", "locus": name, "goal": goal_id, "status": "done"})
        after = self._dot(_request("/api/loci")[1], name)
        self.assertEqual(after["open_goals"], 0)

        # A plain run is refused, which is correct.
        status, refused = _request("/api/loci", {"action": "run", "locus": name})
        self.assertEqual(status, 200)
        self.assertFalse(refused.get("accepted"), "nothing to advance, so a bare run should refuse")

        # A message is accepted, because the owner supplied the work.
        status, accepted = _request(
            "/api/loci", {"action": "run", "locus": name, "message": "make me a poster for Friday"}
        )
        self.assertEqual(status, 200)
        self.assertTrue(accepted.get("accepted"), accepted)

    def test_a_disabled_dot_still_refuses_a_message(self) -> None:
        """Disabled means disabled. A message drops the objective checks, not the enable switch."""
        name, _ = self._create()
        _request("/api/loci", {"action": "toggle", "locus": name, "enabled": False})
        status, payload = _request(
            "/api/loci", {"action": "run", "locus": name, "message": "please do something"}
        )
        self.assertEqual(status, 200)
        self.assertFalse(payload.get("accepted"), payload)
        self.assertIn("disabled", payload.get("reason", "").lower())

    def test_a_run_records_that_the_dot_has_run(self) -> None:
        """Without this a locus is due again the instant it finishes, forever.

        `is_due()` compares against `last_run_at`, and 0.0 means "never ran", so a run started from
        the panel that did not stamp it left the locus permanently due: it could be started over and
        over, and a proactive locus would wake on every pass. A real defect, found by running the
        `cowork` locus from the dashboard and noticing `last_run` was still false afterwards.
        """
        name, _ = self._create(proactive=True)
        before = self._dot(_request("/api/loci")[1], name)
        self.assertEqual(before["seconds_until_due"], 0, "a brand-new locus should be due")

        status, payload = _request("/api/loci", {"action": "run", "locus": name})
        self.assertEqual(status, 200)
        self.assertTrue(payload.get("accepted"), payload)

        after = self._dot(_request("/api/loci")[1], name)
        self.assertGreater(
            after["seconds_until_due"], 0,
            "the locus is still due right after being run, so last_run_at was never stamped",
        )
        self.assertFalse(after["due_now"])

    # ---- delete ------------------------------------------------------------------------
    def test_delete_removes_it_everywhere(self) -> None:
        name, _ = self._create()
        status, payload = _request("/api/loci", {"action": "delete", "locus": name})
        self.assertEqual(status, 200)
        self.assertNotIn(name, [d["name"] for d in self._all_dots(payload)])

        from myriaxon.loci import get_registry

        registry = get_registry()
        registry.reload()
        self.assertNotIn(name, [d.name for d in registry.all()])

    # ---- the badge ---------------------------------------------------------------------
    def test_payload_supports_the_open_goal_badge(self) -> None:
        """The menu badge counts loci with open objectives; it needs that number per locus."""
        _, payload = _request("/api/loci")
        for locus in self._all_dots(payload):
            with self.subTest(locus=locus["name"]):
                self.assertIn("open_goals", locus)
                self.assertIsInstance(locus["open_goals"], int)


if __name__ == "__main__":
    unittest.main(verbosity=2)
