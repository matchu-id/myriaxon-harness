"""A failed turn must reach the browser.

The defect this guards against was found by running a turn with an unreachable provider: the except
block meant to report the failure raised

    TypeError: EventBus.publish() got multiple values for argument 'kind'

because `publish(kind, **payload)` uses `kind` for the event name while the payload also passed
`kind=`. So the handler whose entire purpose is "reported to the UI, never swallowed" swallowed it,
and a failed turn left the user watching a spinner with no explanation.

That is why this asserts the *event arrives*, not that the code path was reached: a test that only
called the failing code and checked `state.error` would have passed while the browser saw nothing.
"""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from myriaxon.config import Config  # noqa: E402
from myriaxon.dashboard.events import Event, EventBus  # noqa: E402
from myriaxon.dashboard.session import Session  # noqa: E402


class TestEventBusPublish(unittest.TestCase):
    def test_kind_becomes_the_event_type(self) -> None:
        bus = EventBus()
        event = bus.publish("error", message="boom")
        self.assertEqual(event["type"], "error")
        self.assertEqual(event["message"], "boom")

    def test_a_payload_key_called_kind_is_rejected_loudly(self) -> None:
        """The collision, pinned so the name cannot be reused by accident.

        It raises rather than overwriting the event name, which is the correct behaviour: silently
        renaming `kind` would make an event indistinguishable from its own payload. Anyone tempted
        to pass a payload `kind` will hit this test rather than a silent failure at runtime.
        """
        bus = EventBus()
        with self.assertRaises(TypeError):
            bus.publish("error", kind="ValueError")


class TestFailureReachesTheBus(unittest.TestCase):
    def test_a_provider_failure_publishes_an_error_event(self) -> None:
        config = Config(
            provider="openai_compat",
            api_key="unused",
            # Port 1 is reserved and refuses instantly, so this fails fast without real network use.
            base_url="http://127.0.0.1:1/v1",
            model="no-such-model",
        )
        session = Session("error-event-test", REPO, config=config, mode="auto_edit")
        queue_, _replay = session.bus.subscribe()

        session.submit("say something")

        events: list[Event] = []
        deadline = time.time() + 60
        while time.time() < deadline:
            try:
                item = queue_.get(timeout=0.5)
            except Exception:
                if not session.busy:
                    break
                continue
            if item is None:
                break
            events.append(item)
            if item.get("type") == "error":
                break
            if item.get("type") == "turn_end" and not session.busy:
                # One more moment for anything queued behind it.
                time.sleep(0.3)
                while not queue_.empty():
                    extra = queue_.get_nowait()
                    if extra is not None:
                        events.append(extra)
                break

        kinds = [e.get("type") for e in events]
        errors = [e for e in events if e.get("type") == "error"]
        self.assertTrue(
            errors,
            f"no error event reached the bus; events seen: {kinds}. A failed turn that reports "
            "nothing is precisely the bug this test exists for.",
        )
        payload = errors[0]
        self.assertIn("message", payload)
        self.assertIn("exc_type", payload, "the exception class must have somewhere to live")
        self.assertNotEqual(
            payload.get("exc_type"), "error",
            "exc_type is the exception class; 'error' is the event type",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
