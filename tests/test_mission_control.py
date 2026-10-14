"""Mission control's data layer: the frozen contract, honest empties, and a snapshot that cannot raise.

The view this feeds is a report about the machine, so the tests are about the two things that
make such a report trustworthy:

* **the contract** -- every key present, every nested object the declared type, on a real
  application object that has just been constructed and has done nothing. A UI is being written
  against this shape, and an absent list is a JavaScript crash rather than an empty panel.
* **honesty under failure** -- when a source is broken the snapshot still returns a complete,
  well-formed payload and *names the failure* in ``health.items`` instead of quietly showing
  zeros. A dashboard that 500s because one SQLite file is locked is worse than one that says
  "memory unavailable"; a dashboard that shows a confident zero is worse than both.

Everything runs offline against the real ``DashboardApp`` and a scratch locus registry, so no test
here touches ``~/.myriaxon`` (``tests/conftest.py`` points the config directory at a temporary
directory for the whole session; :meth:`MissionControlTestCase.setUpClass` asserts that before
anything else runs).
"""

from __future__ import annotations

import json
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from myriaxon import constants, loci, mcp_client, mission_control
from myriaxon import settings as settings_module
from myriaxon.config import Config
from myriaxon.dashboard import SessionManager
from myriaxon.dashboard.server import DashboardApp

# The contract, verbatim. Kept as data so the test reads as the contract rather than as a pile of
# assertions, and so a field added to the payload without being added here fails loudly.
TOP_LEVEL = {
    "generated_at": (int, float),
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

HEALTH_STATES = {"ok", "warn", "error", "off"}
HEALTH_KINDS = {"provider", "integration", "local"}
RUN_STATUSES = {"running", "idle", "error"}
RUN_KINDS = {"chat", "locus"}
TRACE_KINDS = {"tool", "ok", "warn", "error", "mem", "turn"}
COUNT_KEYS = {"loci", "open_goals", "sessions", "skills", "mcp_servers", "plugins", "rules"}


class _MinimalApp:
    """The least an application can be and still be a host for this view.

    Deliberately not a ``DashboardApp``: the CLI should be able to render the same report, so
    nothing in the data layer may depend on the server's own class.
    """

    def __init__(self, workspace: Path, config: Config) -> None:
        self.workspace = workspace
        self.config = config
        self.sessions = SessionManager(workspace)


class MissionControlTestCase(unittest.TestCase):
    """A real dashboard app in a scratch workspace, plus a private locus registry.

    The registry is injected by patching the accessor, not by assigning ``loci._registry`` --
    see ``test_loci.py`` for why (the panel tests build a real dashboard in this same process,
    and assigning the module global would hand them a scratch registry their API never created).
    """

    @classmethod
    def setUpClass(cls) -> None:
        # Assert the session-wide scoping worked before a single locus is created: if this is false
        # these tests are about to write into the developer's own state directory, which is worth
        # failing loudly for rather than discovering afterwards.
        if "myriaxon-tests-" not in str(constants.CONFIG_DIR):
            raise RuntimeError(
                f"refusing to run: CONFIG_DIR is {constants.CONFIG_DIR}, not the pytest scratch "
                "directory from conftest.py. Something imported the package before conftest ran."
            )

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory(prefix="mx-mission-control-")
        self.workspace = Path(self._tmp.name) / "workspace"
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.app = DashboardApp(
            self.workspace,
            config=Config(api_key="sk-ant-test-placeholder", provider="anthropic", model="claude-opus-5"),
        )
        self.registry = loci.LocusRegistry(Path(self._tmp.name) / "loci.json")
        self._saved_get_registry = mission_control.get_registry
        mission_control.get_registry = lambda: self.registry

    def tearDown(self) -> None:
        mission_control.get_registry = self._saved_get_registry
        self._tmp.cleanup()

    # ---- contract assertions -----------------------------------------------------------
    def assert_contract(self, snap: dict) -> None:
        """Walk the whole payload. Used by every test, including the broken-source ones."""
        self.assertIsInstance(snap, dict)
        self.assertEqual(set(snap), set(TOP_LEVEL), "top-level keys must be exactly the contract")
        for key, expected in TOP_LEVEL.items():
            self.assertIsInstance(snap[key], expected, f"{key} has the wrong type")

        health = snap["health"]
        self.assertIn(health["overall"], {"ok", "warn", "error"}, "overall is not one of the three states")
        self.assertIsInstance(health["summary"], str)
        self.assertTrue(health["summary"].strip(), "health.summary must say something")
        self.assertIsInstance(health["items"], list)
        for item in health["items"]:
            self.assertEqual(set(item), {"key", "label", "kind", "state", "detail"})
            self.assertIsInstance(item["key"], str)
            self.assertIsInstance(item["label"], str)
            self.assertIn(item["kind"], HEALTH_KINDS)
            self.assertIn(item["state"], HEALTH_STATES)
            self.assertIsInstance(item["detail"], str)

        usage = snap["usage"]
        for key in ("requests", "input_tokens", "output_tokens", "total_tokens", "sessions",
                    "turns", "tools_called", "errors"):
            self.assertIsInstance(usage[key], int, f"usage.{key} is not an int")
            self.assertGreaterEqual(usage[key], 0)
        self.assertIsInstance(usage["started_at"], float)
        self.assertLessEqual(usage["started_at"], snap["generated_at"])
        self.assertEqual(usage["total_tokens"], usage["input_tokens"] + usage["output_tokens"])
        self.assertIsInstance(usage["by_model"], list)
        for row in usage["by_model"]:
            self.assertEqual(set(row), {"model", "requests", "input_tokens", "output_tokens"})
            self.assertIsInstance(row["model"], str)
            self.assertIsInstance(row["requests"], int)

        for run in snap["runs"]:
            self.assertEqual(
                set(run),
                {"id", "title", "kind", "locus", "provider", "model", "mode", "busy", "turns",
                 "started_at", "updated_at", "elapsed_s", "progress", "steps", "status", "error"},
            )
            self.assertIsInstance(run["id"], str)
            self.assertIsInstance(run["title"], str)
            self.assertIn(run["kind"], RUN_KINDS)
            self.assertIsInstance(run["locus"], str)
            self.assertIsInstance(run["busy"], bool)
            self.assertIsInstance(run["turns"], int)
            self.assertIsInstance(run["started_at"], float)
            self.assertIsInstance(run["updated_at"], float)
            self.assertIsInstance(run["elapsed_s"], int)
            self.assertGreaterEqual(run["elapsed_s"], 0)
            self.assertTrue(run["progress"] is None or isinstance(run["progress"], float))
            self.assertIsInstance(run["steps"], int)
            self.assertIn(run["status"], RUN_STATUSES)
            self.assertTrue(run["error"] is None or isinstance(run["error"], str))
            if run["kind"] == "locus":
                self.assertTrue(run["locus"], "a locus run must name its locus")

        for approval in snap["approvals"]:
            self.assertEqual(set(approval), {"id", "what", "why", "kind", "locus", "since"})
            for key in ("id", "what", "why", "kind", "locus"):
                self.assertIsInstance(approval[key], str)
            self.assertIsInstance(approval["since"], float)

        self.assertLessEqual(len(snap["trace"]), mission_control.TRACE_LIMIT)
        stamps = []
        for entry in snap["trace"]:
            self.assertEqual(set(entry), {"at", "source", "kind", "text", "run"})
            self.assertIsInstance(entry["at"], float)
            self.assertIsInstance(entry["source"], str)
            self.assertIn(entry["kind"], TRACE_KINDS)
            self.assertIsInstance(entry["text"], str)
            self.assertIsInstance(entry["run"], str)
            stamps.append(entry["at"])
        self.assertEqual(stamps, sorted(stamps, reverse=True), "trace must be newest first")

        for row in snap["schedules"]:
            self.assertEqual(
                set(row),
                {"locus", "role", "enabled", "proactive", "interval_seconds",
                 "seconds_until_due", "due_now", "open_goals", "next_objective"},
            )
            self.assertIsInstance(row["locus"], str)
            self.assertIsInstance(row["role"], str)
            self.assertIsInstance(row["enabled"], bool)
            self.assertIsInstance(row["proactive"], bool)
            self.assertIsInstance(row["interval_seconds"], int)
            self.assertIsInstance(row["seconds_until_due"], int)
            self.assertIsInstance(row["due_now"], bool)
            self.assertIsInstance(row["open_goals"], int)
            self.assertIsInstance(row["next_objective"], str)

        memory = snap["memory"]
        self.assertEqual(set(memory), {"sessions_indexed", "recent"})
        self.assertIsInstance(memory["sessions_indexed"], int)
        for entry in memory["recent"]:
            self.assertEqual(set(entry), {"project", "when", "snippet"})
            self.assertIsInstance(entry["project"], str)
            self.assertIsInstance(entry["when"], float)
            self.assertIsInstance(entry["snippet"], str)

        for skill in snap["skills"]:
            self.assertEqual(set(skill), {"name", "scope", "description"})
            for key in ("name", "scope", "description"):
                self.assertIsInstance(skill[key], str)

        total_share = 0.0
        for row in snap["routing"]:
            self.assertEqual(set(row), {"model", "requests", "share"})
            self.assertIsInstance(row["model"], str)
            self.assertIsInstance(row["requests"], int)
            self.assertIsInstance(row["share"], float)
            total_share += row["share"]
        if snap["routing"]:
            self.assertAlmostEqual(total_share, 1.0, places=3, msg="shares must account for every request")

        counts = snap["counts"]
        self.assertEqual(set(counts), COUNT_KEYS)
        for key, value in counts.items():
            self.assertIsInstance(value, int, f"counts.{key} is not an int")
            self.assertGreaterEqual(value, 0)


class TestContract(MissionControlTestCase):
    def test_an_empty_app_satisfies_the_whole_contract(self) -> None:
        # The scratch config directory is shared with every other test module, so the two files
        # that accumulate across a run (the memory index and the audit log) are pointed at paths
        # that do not exist. "Empty" has to mean empty for this test to be about the empty case.
        with mock.patch.object(
            constants, "MEMORY_DB_FILE", Path(self._tmp.name) / "absent-memory.db"
        ), mock.patch.object(
            constants, "AUDIT_LOG_FILE", Path(self._tmp.name) / "absent-audit.jsonl"
        ):
            snap = mission_control.snapshot(self.app)

        self.assert_contract(snap)
        self.assertEqual(snap["workspace"], str(self.workspace))
        self.assertEqual(snap["runs"], [], "a freshly built app has no sessions")
        self.assertEqual(snap["approvals"], [], "nothing is waiting on a person yet")
        self.assertEqual(snap["schedules"], [], "no loci exist yet")
        self.assertEqual(snap["trace"], [], "nothing has happened yet")
        self.assertEqual(snap["counts"]["loci"], 0)
        self.assertEqual(snap["counts"]["open_goals"], 0)
        self.assertEqual(snap["counts"]["sessions"], 0)
        self.assertEqual(snap["usage"]["requests"], 0)
        self.assertEqual(snap["usage"]["total_tokens"], 0)
        self.assertEqual(snap["usage"]["sessions"], 0)
        self.assertEqual(snap["routing"], [], "no model has handled anything yet")
        self.assertEqual(snap["memory"]["sessions_indexed"], 0)
        self.assertEqual(snap["memory"]["recent"], [])

    def test_generated_at_is_now_and_usage_starts_at_process_start(self) -> None:
        before = time.time()
        snap = mission_control.snapshot(self.app)
        after = time.time()
        self.assertGreaterEqual(snap["generated_at"], before)
        self.assertLessEqual(snap["generated_at"], after)
        self.assertAlmostEqual(snap["usage"]["started_at"], self.app.started_at, places=3)

    def test_every_provider_is_probed(self) -> None:
        snap = mission_control.snapshot(self.app)
        keys = {item["key"] for item in snap["health"]["items"] if item["kind"] == "provider"}
        self.assertEqual(
            keys,
            {f"provider:{p}" for p in ("anthropic", "openai_compat", "openrouter", "deepseek",
                                       "gemini", "netra")},
        )
        # The active provider is listed first: the ring diagram renders the head of the list.
        self.assertEqual(snap["health"]["items"][0]["key"], "provider:anthropic")

    def test_the_snapshot_is_fast_enough_for_a_poll(self) -> None:
        mission_control.snapshot(self.app)  # warm the SDK/writability caches, as a running server is
        started = time.perf_counter()
        mission_control.snapshot(self.app)
        elapsed = time.perf_counter() - started
        self.assertLess(elapsed, 0.5, f"snapshot took {elapsed * 1000:.0f}ms on an idle machine")

    def test_it_never_creates_the_memory_database_it_reports_on(self) -> None:
        """A status endpoint that writes is how a mystery file gets blamed on something else."""
        absent = Path(self._tmp.name) / "memory-not-created.db"
        with mock.patch.object(constants, "MEMORY_DB_FILE", absent):
            snap = mission_control.snapshot(self.app)
        self.assertFalse(absent.exists(), "the snapshot created the memory database it was reading")
        self.assertEqual(snap["memory"]["sessions_indexed"], 0)
        self.assert_contract(snap)

    def test_a_minimal_app_object_is_enough(self) -> None:
        """Nothing here may require DashboardApp specifically: a workspace, a config and a
        session manager are the whole interface, so the CLI can read the same view."""
        minimal = _MinimalApp(self.workspace, Config(api_key="sk-ant-test-placeholder", provider="anthropic"))
        snap = mission_control.snapshot(minimal)
        self.assert_contract(snap)
        self.assertEqual(snap["workspace"], str(self.workspace))
        self.assertEqual(snap["counts"]["sessions"], 0)
        self.assertEqual(snap["counts"]["loci"], 0)


class TestBrokenSources(MissionControlTestCase):
    """A dashboard that 500s on a locked database is worse than one that says so."""

    def test_every_source_raising_still_returns_a_complete_payload(self) -> None:
        def explode(*_args, **_kwargs):
            raise RuntimeError("source exploded")

        with mock.patch.multiple(
            mission_control,
            load_mcp_config=explode,
            load_messaging_config=explode,
            discover_skills=explode,
            load_settings=explode,
            get_registry=explode,
            _read_memory=explode,
            _read_audit=explode,
            _plugin_state=explode,
        ), mock.patch.object(self.app.sessions, "list", explode):
            snap = mission_control.snapshot(self.app)

        self.assert_contract(snap)
        # Every failure is named rather than hidden: this is the difference between a degraded
        # view and one that lies about the machine.
        details = " ".join(item["detail"] for item in snap["health"]["items"])
        self.assertIn("source exploded", details)
        self.assertNotEqual(snap["health"]["overall"], "ok", "a broken source cannot read as healthy")
        self.assertEqual(snap["counts"]["loci"], 0)
        self.assertEqual(snap["skills"], [])
        self.assertEqual(snap["memory"], {"sessions_indexed": 0, "recent": []})

    def test_an_unreadable_memory_database_degrades_with_a_reason(self) -> None:
        # A directory where a SQLite file is expected: opening it raises, which is the shape of a
        # locked, corrupt or permission-denied database from this code's point of view.
        not_a_database = Path(self._tmp.name) / "memory.db"
        not_a_database.mkdir()
        with mock.patch.object(constants, "MEMORY_DB_FILE", not_a_database):
            snap = mission_control.snapshot(self.app)
        self.assert_contract(snap)
        item = next(i for i in snap["health"]["items"] if i["key"] == "memory")
        self.assertNotEqual(item["state"], "ok")
        self.assertNotEqual(item["detail"], "")
        self.assertEqual(snap["memory"]["sessions_indexed"], 0)

    def test_an_unreadable_audit_log_does_not_cost_the_whole_trace(self) -> None:
        directory = Path(self._tmp.name) / "audit.jsonl"
        directory.mkdir()
        locus = self.registry.create("Sentinel", goals=["Watch the deps"])
        with mock.patch.object(constants, "AUDIT_LOG_FILE", directory):
            snap = mission_control.snapshot(self.app)
        self.assert_contract(snap)
        self.assertTrue(
            any(entry["source"] == locus.name for entry in snap["trace"]),
            "the locus's own activity must still reach the trace",
        )

    def test_a_partially_written_audit_line_is_skipped_not_fatal(self) -> None:
        log = Path(self._tmp.name) / "audit.jsonl"
        good = {"ts": time.time(), "cwd": str(self.workspace), "tool": "read_file",
                "args": {"path": "a.py"}, "is_error": False}
        log.write_text(
            json.dumps(good) + "\n" + json.dumps(good) + "\n" + '{"ts": 1.0, "tool": "trunc',
            encoding="utf-8",
        )
        with mock.patch.object(constants, "AUDIT_LOG_FILE", log):
            snap = mission_control.snapshot(self.app)
        self.assert_contract(snap)
        audit_rows = [entry for entry in snap["trace"] if entry["source"] == "audit"]
        self.assertEqual(len(audit_rows), 2)
        item = next(i for i in snap["health"]["items"] if i["key"] == "audit")
        self.assertEqual(item["state"], "ok")
        self.assertIn("skipped", item["detail"], "the dropped line is reported, not silently lost")

    def test_a_hostile_session_object_cannot_break_the_snapshot(self) -> None:
        class Hostile:
            @property
            def state(self):
                raise RuntimeError("state exploded")

            @property
            def busy(self):
                raise RuntimeError("busy exploded")

            def trajectory(self):
                raise RuntimeError("trajectory exploded")

        with mock.patch.object(self.app.sessions, "get", lambda _sid: Hostile()):
            session = self.app.new_session()
            snap = mission_control.snapshot(self.app)

        self.assert_contract(snap)
        self.assertEqual(len(snap["runs"]), 1, "the session still exists and must still be listed")
        self.assertEqual(snap["runs"][0]["id"], session.id)
        self.assertEqual(snap["runs"][0]["steps"], 0)

    def test_a_catastrophic_failure_still_returns_the_contract(self) -> None:
        """The outermost guarantee: /api/mission-control answers with the right shape or not at
        all, and 'not at all' is a 500 nobody can read."""
        with mock.patch.object(
            mission_control, "_build_snapshot", side_effect=RuntimeError("assembly exploded")
        ):
            snap = mission_control.snapshot(self.app)
        self.assert_contract(snap)
        self.assertEqual(snap["health"]["overall"], "error")
        self.assertIn("assembly exploded", snap["health"]["items"][0]["detail"])
        self.assertEqual(snap["workspace"], str(self.workspace))
        self.assertEqual(snap["runs"], [])


class TestRealData(MissionControlTestCase):
    def test_a_dot_with_a_goal_reaches_schedules_and_counts(self) -> None:
        locus = self.registry.create(
            "Sentinel",
            role="dependency watcher",
            goals=["Report outdated direct dependencies"],
            proactive=True,
            interval_seconds=120,
        )
        snap = mission_control.snapshot(self.app)
        self.assert_contract(snap)

        self.assertEqual(snap["counts"]["loci"], 1)
        self.assertEqual(snap["counts"]["open_goals"], 1)
        self.assertEqual(len(snap["schedules"]), 1)
        row = snap["schedules"][0]
        self.assertEqual(row["locus"], locus.name)
        self.assertEqual(row["role"], "dependency watcher")
        self.assertTrue(row["enabled"])
        self.assertTrue(row["proactive"])
        self.assertEqual(row["interval_seconds"], 120)
        self.assertEqual(row["open_goals"], 1)
        self.assertEqual(row["next_objective"], "Report outdated direct dependencies")
        # A brand-new proactive locus with open work is due immediately (loci.Locus.is_due).
        self.assertTrue(row["due_now"])
        self.assertEqual(row["seconds_until_due"], 0)

    def test_a_dot_with_no_objectives_is_not_scheduled(self) -> None:
        self.registry.create("Idle")
        snap = mission_control.snapshot(self.app)
        self.assertEqual(snap["counts"]["loci"], 1)
        self.assertEqual(snap["schedules"], [], "a locus with nothing to do is not scheduled work")

    def test_a_blocked_objective_is_waiting_on_a_person(self) -> None:
        locus = self.registry.create("Sentinel", goals=["Pick a schema"])
        goal = locus.goals[0]
        locus.set_goal_status(goal.id, "blocked", note="Which schema version is acceptable?")
        self.registry.persist(locus)

        snap = mission_control.snapshot(self.app)
        self.assert_contract(snap)
        self.assertEqual(len(snap["approvals"]), 1)
        approval = snap["approvals"][0]
        self.assertEqual(approval["what"], "Pick a schema")
        self.assertIn("Which schema version is acceptable?", approval["why"])
        self.assertEqual(approval["locus"], "Sentinel")
        self.assertEqual(approval["kind"], "goal")
        # A blocked goal is never handed to the model, so it is not "next" work either.
        self.assertEqual(snap["schedules"][0]["next_objective"], "")

    def test_dot_activity_reaches_the_trace_and_the_counts(self) -> None:
        locus = self.registry.create("Sentinel", goals=["Watch the deps"])
        locus.log("retry", "transient provider failure -- retrying in 4s")
        self.registry.persist(locus)

        snap = mission_control.snapshot(self.app)
        self.assert_contract(snap)
        mine = [entry for entry in snap["trace"] if entry["source"] == "Sentinel"]
        self.assertTrue(mine, "the locus's log must appear in the trace")
        self.assertTrue(any(entry["kind"] == "warn" for entry in mine), "a retry is a warning, not an ok")

    def test_a_session_appears_as_a_run(self) -> None:
        session = self.app.new_session()
        session.state.title = "Fix the parser"
        session.state.turn_count = 3
        snap = mission_control.snapshot(self.app)
        self.assert_contract(snap)

        self.assertEqual(snap["counts"]["sessions"], 1)
        self.assertEqual(snap["usage"]["sessions"], 1)
        self.assertEqual(snap["usage"]["turns"], 3)
        run = snap["runs"][0]
        self.assertEqual(run["id"], session.id)
        self.assertEqual(run["title"], "Fix the parser")
        self.assertEqual(run["kind"], "chat")
        self.assertEqual(run["locus"], "")
        self.assertEqual(run["status"], "idle")
        self.assertTrue(
            any(entry["kind"] == "turn" and entry["run"] == session.id for entry in snap["trace"]),
            "a session with turns must show a turn entry the feed can filter on",
        )

    def test_an_errored_session_is_listed_as_a_run_and_as_waiting_work(self) -> None:
        session = self.app.new_session()
        session.state.error = "AuthenticationError: invalid api key"
        snap = mission_control.snapshot(self.app)
        self.assert_contract(snap)
        self.assertEqual(snap["runs"][0]["status"], "error")
        self.assertEqual(snap["runs"][0]["error"], "AuthenticationError: invalid api key")
        self.assertEqual(snap["usage"]["errors"], 1)
        self.assertTrue(any(a["id"] == f"session:{session.id}" for a in snap["approvals"]))

    def test_a_locus_run_is_labelled_as_a_dot(self) -> None:
        """`DashboardApp.run_locus` titles its session "<name> · locus"; that is the only marker."""
        locus = self.registry.create("Sentinel", goals=["Watch the deps"])
        session = self.app.new_session()
        session.state.title = f"{locus.name} · locus"
        snap = mission_control.snapshot(self.app)
        run = snap["runs"][0]
        self.assertEqual(run["kind"], "locus")
        self.assertEqual(run["locus"], "Sentinel")
        # Progress is only reported where real progress exists: a locus's completed share of goals.
        self.assertEqual(run["progress"], 0.0)

    def test_health_reports_a_missing_api_key_as_not_ok(self) -> None:
        """The one failure a person can fix in a minute must not be reported as ready."""
        app = DashboardApp(
            self.workspace,
            config=Config(api_key="", provider="anthropic", model="claude-opus-5"),
        )
        # No key anywhere: not in the environment, not in any saved config file.
        absent_config = Path(self._tmp.name) / "config.json"
        env = {name: "" for name in (
            "MYRIAXON_API_KEY", "GEMINI_API_KEY", "DEEPSEEK_API_KEY", "OPENROUTER_API_KEY",
            "NETRA_API_KEY", "GOOGLE_GENAI_USE_VERTEXAI", "GOOGLE_CLOUD_PROJECT", "GCLOUD_PROJECT",
        )}
        with mock.patch.object(constants, "CONFIG_FILE", absent_config), mock.patch.dict(
            "os.environ", env
        ):
            snap = mission_control.snapshot(app)
        self.assert_contract(snap)
        item = next(i for i in snap["health"]["items"] if i["key"] == "provider:anthropic")
        self.assertEqual(item["state"], "error")
        self.assertIn("no API key", item["detail"])
        self.assertNotEqual(snap["health"]["overall"], "ok")
        self.assertIn("0 of 6 providers ready", snap["health"]["summary"])

    def test_a_saved_config_key_counts_as_configured(self) -> None:
        """The credential can live in config.json rather than the environment."""
        config_file = Path(self._tmp.name) / "config.json"
        config_file.write_text(
            json.dumps({"provider": "deepseek", "api_key": "sk-saved", "model": "deepseek-v4-flash"}),
            encoding="utf-8",
        )
        app = DashboardApp(
            self.workspace,
            config=Config(api_key="", provider="openai_compat", model="local-model",
                          base_url="http://127.0.0.1:1234/v1"),
        )
        env = {name: "" for name in ("MYRIAXON_API_KEY", "GEMINI_API_KEY", "DEEPSEEK_API_KEY",
                                     "OPENROUTER_API_KEY", "NETRA_API_KEY")}
        with mock.patch.object(constants, "CONFIG_FILE", config_file), mock.patch.dict(
            "os.environ", env
        ):
            snap = mission_control.snapshot(app)
        detail = next(i for i in snap["health"]["items"] if i["key"] == "provider:deepseek")["detail"]
        self.assertIn("config.json", detail)
        # A keyless local endpoint is a warning about that provider, never an error.
        local = next(i for i in snap["health"]["items"] if i["key"] == "provider:openai_compat")
        self.assertEqual(local["state"], "warn")

    def test_skills_are_reported_with_their_scope(self) -> None:
        snap = mission_control.snapshot(self.app)
        self.assert_contract(snap)
        self.assertTrue(snap["skills"], "the built-in skill ships with the package")
        self.assertEqual(snap["counts"]["skills"], len(snap["skills"]))
        scopes = {skill["scope"] for skill in snap["skills"]}
        self.assertTrue(scopes <= {"builtin", "user", "project", "unknown"}, scopes)

    def test_mcp_servers_are_counted_from_the_real_config(self) -> None:
        mcp_file = Path(self._tmp.name) / "mcp_servers.json"
        mcp_file.write_text(
            json.dumps({
                "stdio_servers": [{"name": "files", "command": "python", "args": []}],
                "url_servers": [{"name": "remote", "url": "https://example.invalid/mcp"}],
            }),
            encoding="utf-8",
        )
        # `mcp_client` binds the path at import time, so the file it reads is its own module
        # attribute -- patching `constants` would redirect nothing and the test would pass on a
        # lie.
        with mock.patch.object(mcp_client, "MCP_SERVERS_FILE", mcp_file):
            snap = mission_control.snapshot(self.app)
        self.assert_contract(snap)
        self.assertEqual(snap["counts"]["mcp_servers"], 2)
        item = next(i for i in snap["health"]["items"] if i["key"] == "mcp")
        self.assertIn("files", item["detail"])
        self.assertIn("remote", item["detail"])

    def test_permission_rules_are_counted_from_settings(self) -> None:
        settings_file = Path(self._tmp.name) / "settings.json"
        settings_file.write_text(
            json.dumps({"permissions": {"run_bash": "ask", "write_file": "allow", "*": "ask"}}),
            encoding="utf-8",
        )
        with mock.patch.object(settings_module, "USER_SETTINGS_FILE", settings_file):
            snap = mission_control.snapshot(self.app)
        self.assert_contract(snap)
        self.assertEqual(snap["counts"]["rules"], 3)

    def test_nothing_is_written_to_the_real_config_dir(self) -> None:
        # Pointing the state directory is conftest's job; this asserts it actually took effect, so
        # a module that imported the package before conftest ran cannot pass silently by writing
        # into the developer's own ~/.myriaxon.
        self.assertIn("myriaxon-tests-", str(constants.CONFIG_DIR))
        real = Path.home() / ".myriaxon"
        try:
            before = sorted(p.name for p in real.iterdir()) if real.is_dir() else []
        except OSError:  # an unreadable home directory is not this test's business
            self.skipTest("the real state directory is not readable from this sandbox")
        mission_control.snapshot(self.app)
        after = sorted(p.name for p in real.iterdir()) if real.is_dir() else []
        self.assertEqual(before, after, "the snapshot changed the user's real state directory")


class TestRenderLines(MissionControlTestCase):
    def test_render_lines_is_a_non_empty_list_of_strings_mentioning_the_counts(self) -> None:
        self.registry.create("Sentinel", role="watcher", goals=["Watch the deps"])
        snap = mission_control.snapshot(self.app)
        lines = mission_control.render_lines(snap)

        self.assertIsInstance(lines, list)
        self.assertTrue(lines)
        self.assertGreaterEqual(len(lines), 10, "the summary should be worth reading")
        for line in lines:
            self.assertIsInstance(line, str)
            self.assertNotIn("\n", line, "each entry is one line")

        text = "\n".join(lines)
        self.assertIn(str(self.workspace), text)
        self.assertIn("Sentinel", text)
        self.assertIn("loci 1", text, "the counts line must state the locus count")
        for label in ("open goals", "sessions", "skills", "mcp", "plugins", "rules"):
            self.assertIn(label, text, f"the counts line is missing {label}")
        self.assertIn(snap["health"]["summary"], text)

    def test_render_lines_survives_a_hand_built_or_empty_payload(self) -> None:
        """It reads whatever `snapshot()` produced, and it is the CLI's status readout: a shape
        the formatter did not expect must not turn `myriaxon status` into a traceback."""
        for payload in (
            {},
            {"health": None, "usage": {}, "runs": None},
            {"counts": {"loci": "x"}},
            {"health": "boom", "runs": {"a": 1}, "trace": "nope", "skills": 5},
            {"trace": [None, 7, {"at": "soon", "kind": "nonsense"}]},
            {"workspace": 3, "generated_at": "not a time", "routing": [{"share": "half"}]},
        ):
            with self.subTest(payload=payload):
                lines = mission_control.render_lines(payload)
                self.assertIsInstance(lines, list)
                self.assertTrue(lines)
                self.assertTrue(all(isinstance(line, str) for line in lines))
                self.assertIn("Counts", "\n".join(lines))


if __name__ == "__main__":
    unittest.main(verbosity=2)
