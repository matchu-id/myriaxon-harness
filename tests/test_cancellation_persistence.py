"""Cancellation and session persistence.

Both of these were documented limits rather than implemented features:

* `/api/cancel` set a flag that nothing read. The session reported "cancel requested" and the turn
  ran to completion -- worse than an unimplemented button, because the user believed it stopped.
* Sessions lived only in memory, so a restart erased every conversation and emptied a locus's memory
  at the moment it was supposed to be persistent.

The tests below pin the parts that are easy to get subtly wrong: that the flag reaches the stream,
that a cancelled turn is not recorded as a failure, and that a saved transcript comes back under
its own id without dragging a stale agent with it.
"""

from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from myriaxon import cancellation  # noqa: E402
from myriaxon.dashboard import store  # noqa: E402


class TestCancelToken(unittest.TestCase):
    def test_clear_token_does_not_stop_a_turn(self) -> None:
        token = cancellation.CancelToken()
        token.raise_if_cancelled("text so far")  # must not raise

    def test_requested_token_raises_with_the_partial_text(self) -> None:
        token = cancellation.CancelToken()
        token.request()
        with self.assertRaises(cancellation.TurnCancelled) as caught:
            token.raise_if_cancelled("half an answer")
        self.assertEqual(caught.exception.partial, "half an answer")

    def test_clear_makes_it_reusable(self) -> None:
        """A session cancels once and then runs another turn; the token must not stay poisoned."""
        token = cancellation.CancelToken()
        token.request()
        self.assertTrue(token.requested)
        token.clear()
        self.assertFalse(token.requested)
        token.raise_if_cancelled()

    def test_attach_reuses_and_clears(self) -> None:
        class Agent:
            pass

        agent = Agent()
        first = cancellation.attach(agent)
        first.request()
        second = cancellation.attach(agent)
        self.assertIs(second, first, "a second attach must not replace the token the loop holds")
        self.assertFalse(second.requested, "a new turn starts uncancelled")

    def test_attach_is_safe_on_a_class_that_refuses_attributes(self) -> None:
        class Slotted:
            __slots__ = ()

        token = cancellation.attach(Slotted())
        self.assertIsInstance(token, cancellation.CancelToken)


class TestCancelledTurnIsNotAFailure(unittest.TestCase):
    def test_the_event_loop_reaches_the_token(self) -> None:
        """The stream loops must actually consult the token.

        Asserted against the source because the alternative is a live provider call: the defect this
        guards against was a flag set and never read, and a test that does not look at the loop
        cannot tell an implemented cancel from a documented one.
        """
        for name in ("agent.py", "openai_agent.py", "gemini_agent.py"):
            with self.subTest(module=name):
                source = (REPO / "myriaxon" / name).read_text(encoding="utf-8")
                self.assertIn("raise_if_cancelled", source,
                              f"{name} does not check the cancel token in its stream loop")

    def test_the_console_does_not_report_a_cancelled_turn_as_an_error(self) -> None:
        source = (REPO / "myriaxon" / "dashboard" / "session.py").read_text(encoding="utf-8")
        self.assertIn("except cancellation.TurnCancelled", source)
        # The cancelled branch must come before the broad handler, or it would never be reached.
        cancelled_at = source.index("except cancellation.TurnCancelled")
        generic_at = source.index("except BaseException as exc:")
        self.assertLess(cancelled_at, generic_at,
                        "the specific handler must precede the broad one")
        self.assertIn('self.state.error = None', source[cancelled_at:cancelled_at + 600],
                      "a cancelled turn is not a failed turn")


class TestSessionPersistence(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="mx-store-")
        self._saved = store.constants.CONFIG_DIR
        store.constants.CONFIG_DIR = Path(self._tmp.name) / "config"

    def tearDown(self) -> None:
        store.constants.CONFIG_DIR = self._saved
        self._tmp.cleanup()

    def test_a_saved_session_comes_back(self) -> None:
        messages = [{"role": "user", "text": "hello"}, {"role": "assistant", "text": "hi"}]
        self.assertIsNotNone(store.save("abc123", state={"id": "abc123", "title": "T"},
                                        messages=messages))
        loaded = store.load("abc123")
        self.assertIsNotNone(loaded)
        self.assertEqual([m["text"] for m in loaded["messages"]], ["hello", "hi"])

    def test_tool_messages_are_not_stored(self) -> None:
        """A tool result is not conversation. Storing them multiplies the file for content the
        Trajectory tab already summarises."""
        store.save("abc", state={}, messages=[
            {"role": "user", "text": "q"},
            {"role": "tool", "text": "a very long tool result"},
            {"role": "assistant", "text": "a"},
        ])
        roles = [m["role"] for m in store.load("abc")["messages"]]
        self.assertEqual(roles, ["user", "assistant"])

    def test_an_absent_session_is_none_not_an_error(self) -> None:
        self.assertIsNone(store.load("never-existed"))

    def test_a_corrupt_file_reads_as_absent(self) -> None:
        """A half-written file must not be presented as the conversation."""
        store.sessions_dir().mkdir(parents=True, exist_ok=True)
        store.session_file("broken").write_text("{not json", encoding="utf-8")
        self.assertIsNone(store.load("broken"))

    def test_delete_removes_the_file(self) -> None:
        store.save("gone", state={}, messages=[])
        self.assertTrue(store.session_file("gone").is_file())
        self.assertTrue(store.delete("gone"))
        self.assertFalse(store.session_file("gone").is_file())
        self.assertIsNone(store.load("gone"))

    def test_load_all_is_newest_first_and_carries_ids(self) -> None:
        store.save("old", state={"id": "old"}, messages=[])
        time.sleep(0.02)
        store.save("new", state={"id": "new"}, messages=[])
        entries = store.load_all()
        self.assertEqual([e["state"]["id"] for e in entries][:2], ["new", "old"])

    def test_a_hostile_session_id_cannot_escape_the_directory(self) -> None:
        """Session ids reach here from a URL. A traversing id must not write outside the store."""
        store.save("../../evil", state={}, messages=[])
        written = list(store.sessions_dir().glob("*.json"))
        self.assertEqual(len(written), 1)
        self.assertEqual(written[0].parent, store.sessions_dir())

    def test_the_directory_follows_a_changed_config_dir(self) -> None:
        """Bound at import, a config change would keep writing to the old location -- which is how
        the test suite started building apps over the previous test's state."""
        first = store.sessions_dir()
        store.constants.CONFIG_DIR = Path(self._tmp.name) / "elsewhere"
        self.assertNotEqual(store.sessions_dir(), first)
        store.save("moved", state={}, messages=[])
        self.assertEqual(store.session_file("moved").parent, store.sessions_dir())


class TestSessionRestore(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="mx-restore-")
        self._saved = store.constants.CONFIG_DIR
        store.constants.CONFIG_DIR = Path(self._tmp.name) / "config"

    def tearDown(self) -> None:
        store.constants.CONFIG_DIR = self._saved
        self._tmp.cleanup()

    def _session(self, session_id: str = "sess1"):
        from myriaxon.dashboard.session import Session

        return Session(session_id, REPO, config=mock.Mock(provider="p", model="m"),
                       mode="auto_edit")

    def test_restore_brings_back_the_transcript(self) -> None:
        session = self._session()
        session.restore({
            "state": {"id": "sess1", "title": "Restored", "turn_count": 3, "locus": "cowork"},
            "messages": [{"role": "user", "text": "make a poster"},
                         {"role": "assistant", "text": "done"}],
            "trajectory": [{"tool": "design_poster", "args": {}, "is_error": False}],
        })
        self.assertEqual(session.state.title, "Restored")
        self.assertEqual(session.state.turn_count, 3)
        self.assertEqual(session.state.locus, "cowork")
        # No agent is built, so `messages()` must fall back to the history rather than returning [].
        self.assertEqual([m["text"] for m in session.messages()], ["make a poster", "done"])
        self.assertEqual(session.trajectory()[0]["tool"], "design_poster")

    def test_restore_does_not_build_an_agent(self) -> None:
        """An agent holds a client and an MCP bridge, which belong to a process, not a file."""
        session = self._session()
        session.restore({"state": {}, "messages": [{"role": "user", "text": "hi"}]})
        self.assertIsNone(session.agent)

    def test_restore_ignores_messages_that_are_not_conversation(self) -> None:
        session = self._session()
        session.restore({
            "state": {},
            "messages": [{"role": "tool", "text": "x"}, {"text": "no role"},
                         {"role": "user", "text": "kept"}],
        })
        self.assertEqual([m["text"] for m in session.messages()], ["kept"])

    def test_restore_survives_a_junk_payload(self) -> None:
        session = self._session()
        session.restore({"messages": "not a list", "state": None})
        self.assertEqual(session.messages(), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
