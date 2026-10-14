"""Checkpoint index integrity under concurrency, and the console notice bug.

Two findings from the audit, both verified there by running them. Re-verified here as tests so they
cannot come back:

* `record_checkpoint` did a non-atomic read-modify-write of `index.json` with no lock. Measured: 32
  concurrent writers left 1, 1 and 3 surviving entries. A lost entry is not cosmetic -- it is the only
  record of where the `.bak` is, and the glob fallback in `list_checkpoints` fires only when the index
  is *empty*, so a partially-written index strands the backups on disk.
* `EventConsole.print` dropped every print after the first streamed delta, not just the duplicate
  final-answer echo, so warnings and panels vanished for the rest of the turn.
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from myriaxon import checkpoints  # noqa: E402


class TestCheckpointIndexIntegrity(unittest.TestCase):
    def test_concurrent_checkpoints_all_survive(self) -> None:
        """The measured failure: 32 writers, 1-3 surviving entries."""
        with tempfile.TemporaryDirectory(prefix="mx-ckpt-") as tmp:
            root = Path(tmp)
            writers = 24
            barrier = threading.Barrier(writers)
            failures: list[str] = []

            def write(index: int) -> None:
                target = root / f"file{index}.txt"
                target.write_text("before", encoding="utf-8")
                barrier.wait()          # maximise overlap rather than relying on scheduling luck
                try:
                    # `project_root` is passed explicitly: without it the function resolves the index
                    # against the process working directory, so the test would write to the real
                    # project's checkpoints and then fail looking for the file in its own temp dir.
                    checkpoints.record_checkpoint(
                        target, "original", "write_file", project_root=root)
                except Exception as exc:  # noqa: BLE001
                    failures.append(f"{type(exc).__name__}: {exc}")

            threads = [threading.Thread(target=write, args=(i,)) for i in range(writers)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=60)

            self.assertEqual(failures, [], "record_checkpoint raised")
            index = checkpoints._index_path(root)
            entries = json.loads(index.read_text(encoding="utf-8"))
            self.assertEqual(len(entries), writers,
                             f"{writers - len(entries)} checkpoint(s) lost from the index")

    def test_the_index_is_valid_json_after_concurrent_writes(self) -> None:
        """A truncated index reads as 'no checkpoints' and strands every backup."""
        with tempfile.TemporaryDirectory(prefix="mx-ckpt-") as tmp:
            root = Path(tmp)
            barrier = threading.Barrier(16)

            def write(index: int) -> None:
                target = root / f"f{index}.txt"
                target.write_text("x", encoding="utf-8")
                barrier.wait()
                checkpoints.record_checkpoint(target, "x", "edit_file", project_root=root)

            threads = [threading.Thread(target=write, args=(i,)) for i in range(16)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=60)

            index = checkpoints._index_path(root)
            parsed = json.loads(index.read_text(encoding="utf-8"))
            self.assertIsInstance(parsed, list)
            self.assertTrue(parsed)


class TestConsoleNotices(unittest.TestCase):
    def drain(self, subscriber) -> list:
        """Everything published since the last drain.

        `subscribe()` returns a QUEUE plus a replay list, not a callback. The first version of this
        test registered a bare function, which the bus has no path for, so it observed zero events --
        and a test that sees nothing looks exactly like the bug it is meant to catch.
        """
        out = []
        while True:
            try:
                event = subscriber.get_nowait()
            except Exception:  # noqa: BLE001 - queue.Empty
                break
            if event is None:
                break
            out.append(event)
        return out

    def test_notices_after_a_streamed_answer_still_arrive(self) -> None:
        """The measured failure: 2 notices before `stream_answer`, 0 of 4 comparable prints after."""
        from myriaxon.dashboard.console import EventConsole
        from myriaxon.dashboard.events import EventBus

        bus = EventBus()
        console = EventConsole(bus, session_id="t")
        queue, _ = bus.subscribe(replay=False)

        console.print("before one")
        console.print("before two")
        before = len(self.drain(queue))

        stream = getattr(console, "stream_answer", None)
        if stream is None:
            self.skipTest("this console has no streaming path")
        stream("The answer so far.")

        console.print("[warning]Response truncated at max_tokens.")
        console.print("[dim]— conversation compacted —[/]")
        after = len(self.drain(queue))

        self.assertGreaterEqual(before, 2, "notices before the stream were dropped")
        self.assertGreaterEqual(
            after, 2,
            "notices after a streamed answer were dropped -- warnings and panels vanish for the "
            "rest of the turn",
        )

    def test_the_final_answer_echo_is_still_suppressed(self) -> None:
        """The one thing that guard was written for: the CLI prints the answer at the end of a turn,
        and the dashboard must not show it twice."""
        from myriaxon.dashboard.console import EventConsole
        from myriaxon.dashboard.events import EventBus

        bus = EventBus()
        console = EventConsole(bus, session_id="t")
        queue, _ = bus.subscribe(replay=False)

        stream = getattr(console, "stream_answer", None)
        if stream is None:
            self.skipTest("this console has no streaming path")
        stream("The answer.")
        self.drain(queue)
        # The same words again: this is the echo, and it must not create a second message.
        console.print("The answer.")
        self.assertEqual(len(self.drain(queue)), 0,
                         "the duplicate final-answer echo was rendered twice")


if __name__ == "__main__":
    unittest.main(verbosity=2)
