"""The dashboard page and the server must agree on the names in every payload.

Why this exists: the rename from "dots" to "loci" was finished on the server (every payload says `loci` / `locus`) but not in
the page, which kept reading `dots` and `dot`. The page did not crash, it just silently rendered nothing: the Loci list said "No
bots yet" after a bot had been added, Mission control counted 0, and a notice read "Added the undefined bot". Unit tests of
the server cannot see that, and nothing ran the page's JavaScript.

This test starts a real server, takes the real payloads, and checks that every field the page reads for loci is in them. It
also fails if one of the old names comes back. It needs no browser; the browser-driven check of the same screens is
`tools/verify_dashboard_ui.py`.
"""

from __future__ import annotations

import json
import re
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

PORT = 8799
BASE = f"http://127.0.0.1:{PORT}"


def _call(path: str, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(BASE + path, data=data, headers={"Content-Type": "application/json"},
                                     method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"null")


class TestPageMatchesServer(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from myriaxon import constants
        from myriaxon.config import Config
        from myriaxon.dashboard.server import serve
        from myriaxon.dashboard.ui import INDEX_HTML

        if "myriaxon-tests-" not in str(constants.CONFIG_DIR):
            raise RuntimeError(f"refusing to run against {constants.CONFIG_DIR}: not the pytest scratch directory")
        cls.html = INDEX_HTML
        workspace = constants.CONFIG_DIR / "contract-workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        config = Config(provider="openai_compat", api_key="mock", base_url="http://127.0.0.1:9/v1", model="m")
        threading.Thread(
            target=lambda: serve(workspace, config=config, host="127.0.0.1", port=PORT, mode="auto_edit",
                                 open_browser=False),
            daemon=True,
        ).start()
        deadline = time.time() + 40
        while time.time() < deadline:
            try:
                _call("/api/health")
                break
            except Exception:  # noqa: BLE001
                time.sleep(0.4)
        else:
            raise RuntimeError("the dashboard did not start")

        # One bot from a template (the response the page reads `out.locus` from), with one open objective, blocked, so that
        # it appears in every Mission control list the page renders from loci.
        status, cls.template_reply = _call("/api/loci", {"action": "template", "template": "ops"})
        assert status == 200, cls.template_reply
        loci = _call("/api/loci")[1]["loci"]
        name = cls.template_reply["locus"]
        bot = next(item for item in loci if item["name"] == name)
        _call("/api/loci", {"action": "goal_status", "locus": name, "goal": bot["goals"][0]["id"],
                            "status": "blocked", "note": "needs a person"})
        # A session, so that Mission control has a run to list (the page reads `run.locus` from each).
        _call("/api/sessions", {})
        cls.mission = _call("/api/mission-control")[1]
        cls.loci_payload = _call("/api/loci")[1]
        cls.config_payload = _call("/api/config")[1]

    # ---- the page reads these names, so the server must send them -----------------------------------------------
    def test_the_loci_list_is_under_loci(self) -> None:
        self.assertIsInstance(self.loci_payload.get("loci"), list)
        self.assertTrue(self.loci_payload["loci"], "the bot that was just added is not in the list")
        self.assertIsInstance(self.config_payload.get("loci", {}).get("loci"), list,
                              "the page reads config.loci.loci for the rail badge")

    def test_adding_a_template_reports_the_bot_by_locus(self) -> None:
        self.assertTrue(self.template_reply.get("locus"), "the page prints out.locus in its notice")
        self.assertIn("created", self.template_reply)

    def test_mission_control_counts_loci(self) -> None:
        self.assertGreaterEqual(self.mission["counts"].get("loci", 0), 1, "the Loci tile reads counts.loci")

    def test_mission_control_rows_name_the_locus(self) -> None:
        approvals = self.mission["approvals"]
        self.assertTrue(approvals, "a blocked objective must appear under 'Waiting on you'")
        self.assertTrue(all(row.get("locus") for row in approvals if row.get("kind") == "goal"),
                        "the Open button reads a.locus")
        schedules = self.mission["schedules"]
        self.assertTrue(schedules and all(row.get("locus") for row in schedules), "the schedule list reads s.locus")
        runs = self.mission["runs"]
        self.assertTrue(runs and all("locus" in row for row in runs), "the runs list reads run.locus")

    # ---- and the page must not read the old names ---------------------------------------------------------------
    def test_the_page_reads_the_new_names(self) -> None:
        for expected in ("(config.loci || {}).loci", "info.loci", "out.locus", "counts.loci", "a.locus",
                         "run.locus", "s.locus", "button.dataset.locus"):
            self.assertIn(expected, self.html, f"the page no longer reads {expected}")

    def test_the_page_does_not_read_the_old_names(self) -> None:
        stale = re.findall(r"(?:\b(?:info|counts|out|run|a|s)\.dots?\b|\bdataset\.dots?\b|\.loci\s*\|\|\s*\{\}\)\.dots\b)", self.html)
        self.assertEqual(stale, [], "the page reads a payload field that the server no longer sends")


if __name__ == "__main__":
    unittest.main()
