"""End-to-end: a real agent turn against a local OpenAI-compatible provider.

The rest of the suite checks the pieces. This checks that they compose: provider config ->
client -> agent -> streamed response -> tool call -> tool result fed back -> final answer ->
persisted transcript, plus the dashboard's separate agent builder.

It exists because every release-blocking defect in this codebase lived on that path and was
invisible to unit tests -- a variable that was never assigned, a module that was never imported,
an argument read from the wrong message. A suite that only tests the pieces will pass while the
product cannot start.

Offline by construction: the provider is a scripted local server (`tools/mock_provider.py`), so
this needs no API key, costs nothing, and is deterministic. It drives the same `_build_agent`
that `main()` calls rather than constructing an agent by hand, so a break anywhere in provider
dispatch or client construction fails here.
"""

from __future__ import annotations

import io
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tools"))


def _run_e2e() -> tuple[int, list[tuple[str, bool, str]]]:
    """Run the check script and return (failure_count, results).

    The script is the single source of truth for what is asserted -- duplicating the assertions
    here would let the two drift, which is the failure mode this whole exercise is about.
    """
    import e2e_check

    e2e_check.results.clear()
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        e2e_check.main()
    failures = [r for r in e2e_check.results if not r[1]]
    return len(failures), list(e2e_check.results)


class TestEndToEndTurn(unittest.TestCase):
    """One run, asserted from several angles. The turn is expensive (a real subprocess-free HTTP
    round trip), so it runs once for the class rather than per test."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.failure_count, cls.results = _run_e2e()

    def test_the_turn_path_works_end_to_end(self) -> None:
        failures = [f"{label}: {detail}" for label, ok, detail in self.results if not ok]
        self.assertEqual(failures, [], f"{len(failures)} end-to-end check(s) failed")
        self.assertGreater(len(self.results), 20, "the check script reported implausibly few checks")

    def test_a_tool_call_actually_ran(self) -> None:
        """The whole point of the harness: the model asked for a tool and it happened."""
        by_label = {label: ok for label, ok, _ in self.results}
        for label in (
            "agent builds through _build_agent",
            "run_turn completes without raising",
            "the tool call created the file on disk",
            "the tool result was fed back (two requests)",
            "the transcript ends with the assistant",
        ):
            with self.subTest(check=label):
                self.assertTrue(by_label.get(label), f"missing or failing: {label}")

    def test_capabilities_are_exposed_to_the_model(self) -> None:
        by_label = {label: ok for label, ok, _ in self.results}
        # The locus tools are DEFERRED rather than always offered -- withheld to save ~700 tokens on
        # every request, loaded by `request_tools`. So the property to assert is reachability, not
        # presence: a label demanding presence would re-break the saving, and asserting nothing would
        # let the group become unreachable unnoticed.
        self.assertTrue(by_label.get("the unlock tool is offered"),
                        "request_tools is missing, so no deferred group is reachable")
        self.assertTrue(by_label.get("locus tools are withheld but loaded on request"),
                        "the locus tools are neither offered nor reachable")
        self.assertTrue(by_label.get("no delegation tools in single-agent mode"),
                        "delegation must stay off unless multi-agent mode is requested")


if __name__ == "__main__":
    unittest.main(verbosity=2)
