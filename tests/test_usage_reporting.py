"""Usage reporting in the chat status line.

The report was "I still don't see this" about the tokens/steps/cache line. Three separate faults,
each of which alone would have hidden it:

* `renderTurnInfo` was only called on `turn_end`, so opening a session or reloading the page showed
  an empty status line.
* `/api/messages` did not carry usage at all, so there was nothing for `attach` to render.
* `_persist()` ran *before* `_usage_snapshot()`, and `_usage_snapshot` is what writes `state.usage` --
  so the saved file held an all-zero block. The right shape with no numbers is worse than no block,
  because it looks authoritative.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from myriaxon.dashboard.session import Session, SessionState  # noqa: E402


def make_session() -> Session:
    return Session("usage-test", REPO, config=mock.Mock(provider="p", model="m"), mode="auto_edit")


class TestStateCarriesUsage(unittest.TestCase):
    def test_a_fresh_state_has_an_empty_usage_dict(self) -> None:
        state = SessionState(id="x", provider="p", model="m")
        self.assertEqual(state.usage, {})
        self.assertIn("usage", state.to_dict())

    def test_usage_round_trips_through_to_dict(self) -> None:
        """The session list is built from `to_dict`, so the status line reads it from there."""
        state = SessionState(id="x", provider="p", model="m",
                             usage={"total_tokens": 42, "request_count": 1})
        self.assertEqual(state.to_dict()["usage"]["total_tokens"], 42)

    def test_to_dict_snapshots_rather_than_aliasing(self) -> None:
        state = SessionState(id="x", provider="p", model="m", usage={"n": 1})
        exported = state.to_dict()
        exported["usage"]["n"] = 99
        self.assertEqual(state.usage["n"], 1, "a caller must not be able to edit live state")


class TestUsageSnapshot(unittest.TestCase):
    def test_no_agent_falls_back_to_the_stored_snapshot(self) -> None:
        """A restored session has no agent; its spending must not read as zero."""
        session = make_session()
        session.state.usage = {"total_tokens": 1234, "request_count": 7}
        self.assertIsNone(session.agent)
        self.assertEqual(session.usage()["total_tokens"], 1234)

    def test_no_agent_and_no_stored_usage_is_empty_not_an_error(self) -> None:
        self.assertEqual(make_session().usage(), {})

    def test_live_totals_are_computed_and_stored_on_the_state(self) -> None:
        from myriaxon.usage import UsageTotals

        session = make_session()
        totals = UsageTotals()
        totals.add(1000, 200, 900, 100)
        session.agent = mock.Mock(usage_totals=totals)

        snapshot = session.usage()
        self.assertEqual(snapshot["total_tokens"], 1200)
        self.assertEqual(snapshot["cache_read_tokens"], 900)
        # reads / (reads + uncached input) -- against input alone the rate can exceed 100%.
        self.assertAlmostEqual(snapshot["cache_hit_rate"], 900 / 1900, places=6)
        self.assertIn("steps", snapshot)
        self.assertEqual(
            session.state.usage["total_tokens"], 1200,
            "the snapshot must be kept on the state so it survives a restart",
        )

    def test_a_provider_reporting_no_cache_yields_zero_not_a_fabricated_rate(self) -> None:
        from myriaxon.usage import UsageTotals

        session = make_session()
        totals = UsageTotals()
        totals.add(500, 50)
        session.agent = mock.Mock(usage_totals=totals)
        self.assertEqual(session.usage()["cache_hit_rate"], 0.0)


class TestPersistOrder(unittest.TestCase):
    def test_persist_happens_after_the_usage_snapshot(self) -> None:
        """Asserted on the source: the ordering is the whole defect, and a runtime test would need a
        real provider turn to reach this code."""
        source = (REPO / "myriaxon" / "dashboard" / "session.py").read_text(encoding="utf-8")
        body = source[source.index("def _run_turn"):source.index("def _persist")]
        snapshot_at = body.index("usage = self._usage_snapshot()")
        persist_at = body.index("self._persist()")
        self.assertLess(
            snapshot_at, persist_at,
            "the snapshot must be taken before the save, or the file stores an all-zero block",
        )

    def test_the_route_carries_usage_without_building_an_agent(self) -> None:
        """`/api/messages` is a GET. Building an agent there would start MCP servers on every read,
        which is why the state copy exists instead.

        Comments are stripped before the check: the route's own comment explains that it must *not*
        call `_ensure_agent`, and a naive substring search matched the explanation.
        """
        source = (REPO / "myriaxon" / "dashboard" / "server.py").read_text(encoding="utf-8")
        route = source[source.index('if route == "/api/messages"'):]
        route = route[:route.index("if route ==", 10)]
        code = "\n".join(
            line for line in route.splitlines() if not line.strip().startswith("#")
        )
        self.assertIn('"usage": session.usage()', code)
        self.assertNotIn("_ensure_agent", code,
                         "a read-only GET must not build an agent")


if __name__ == "__main__":
    unittest.main(verbosity=2)
