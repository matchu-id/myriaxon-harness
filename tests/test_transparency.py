"""The transparency dial: what is shown, at eight levels, and what is never shown.

The feature exists because a single verbose flag forces a bad choice -- quiet enough to work in is too quiet to
audit, loud enough to audit buries the answer. The dial is the answer, and these tests pin the three properties
that make it worth having rather than merely present.

  * **The level GATES, it does not filter.** A layer above the dial is never built or stored, so a low setting
    is genuinely quiet and no level can reveal something a higher one hides. A display-time filter would leak
    the same event to any other consumer of the buffer.
  * **A refusal is an event.** "Why did it not do the thing" is the question people arrive with, and a feed
    that only shows completed work cannot answer it.
  * **A credential never reaches the feed.** Redaction happens at EMISSION, reusing the audit log's own
    redactor rather than growing a second opinion about what a secret looks like.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from myriaxon import transparency as T  # noqa: E402


@pytest.fixture(autouse=True)
def _restore_level():
    """Every test gets the dial back where it found it, so a failing test cannot silence the next one."""
    dial = T.get()
    original = dial.level
    yield
    dial.set_level(original)


class TestTheEightLayers:
    def test_there_are_eight_plus_silent(self):
        assert T.MAX_LEVEL == 8
        assert len(T.LAYER_NAMES) == 9

    def test_every_layer_is_named_and_described(self):
        """A dial position with no name is a number, and a number does not tell you what you are seeing."""
        for number in range(T.SILENT, T.MAX_LEVEL + 1):
            assert T.LAYER_NAMES[number]
            assert T.LAYER_DESCRIPTIONS[number]

    def test_the_names_are_ordered_from_least_to_most_revealing(self):
        """The property that makes it a dial rather than a menu: each level includes the ones below it."""
        assert T.LAYER_NAMES[T.PULSE] == "pulse"
        assert T.LAYER_NAMES[T.FILES] == "files"
        assert T.LAYER_NAMES[T.DECISIONS] == "decisions"
        assert T.LAYER_NAMES[T.MEMORY] == "memory"

    def test_describe_returns_every_position_for_a_ui(self):
        described = T.describe()
        assert described["max"] == T.MAX_LEVEL
        assert len(described["layers"]) == T.MAX_LEVEL + 1
        assert all("description" in layer for layer in described["layers"])


class TestTheLevelGatesRatherThanFilters:
    def test_below_the_dial_nothing_is_returned(self):
        dial = T.get()
        dial.set_level(T.ACTIONS)
        assert T.emit(T.FILES, "file", summary="a write") is None
        assert T.emit(T.SYSTEM, "command", summary="a command") is None
        assert T.emit(T.DECISIONS, "decision", summary="a gate") is None

    def test_at_or_below_the_dial_the_event_comes_back(self):
        dial = T.get()
        dial.set_level(T.ACTIONS)
        assert T.emit(T.PULSE, "run") is not None
        assert T.emit(T.ACTIONS, "action") is not None

    def test_a_gated_event_is_not_in_the_buffer_either(self):
        """The gate is at emission, so nothing above the dial is stored -- not merely hidden from one reader."""
        dial = T.get()
        dial.set_level(T.PULSE)
        before = len(dial.recent(limit=1000))
        T.emit(T.MEMORY, "memory", summary="a durable write that must not be retained")
        assert len(dial.recent(limit=1000)) == before

    def test_silent_emits_nothing_at_all(self):
        dial = T.get()
        dial.set_level(T.SILENT)
        assert T.emit(T.PULSE, "run") is None

    def test_a_refusal_IS_an_event(self):
        """The half a log usually omits: what was refused, and why."""
        T.get().set_level(T.DECISIONS)
        event = T.decision("run_bash", reason="no sandbox is available", allowed=False)
        assert event is not None
        assert event.detail["allowed"] is False
        assert "no sandbox" in event.detail["reason"]

    def test_an_allowed_decision_is_recorded_too(self):
        T.get().set_level(T.DECISIONS)
        event = T.decision("write_file", allowed=True)
        assert event is not None and event.detail["allowed"] is True


class TestSecretsNeverReachTheFeed:
    def test_a_credential_in_an_argument_is_redacted(self):
        T.get().set_level(T.MEMORY)
        secret = "sk-live-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
        event = T.action("http_call", {"api_key": secret, "Authorization": f"Bearer {secret}"})
        blob = str(event.to_dict())
        assert secret not in blob
        assert event.detail["args"]["api_key"] == "<redacted>"

    def test_a_nested_credential_is_redacted(self):
        T.get().set_level(T.MEMORY)
        secret = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
        event = T.action("x", {"outer": {"inner": {"token": secret}}})
        assert secret not in str(event.to_dict())

    def test_ordinary_content_survives(self):
        """A redactor that eats everything teaches its reader to turn it off."""
        T.get().set_level(T.MEMORY)
        event = T.action("read_file", {"path": "/home/user/project/main.py", "offset": 40})
        assert event.detail["args"]["path"] == "/home/user/project/main.py"
        assert event.detail["args"]["offset"] == 40


class TestUntrustedTextIsMarked:
    def test_model_text_is_untrusted(self):
        """A path or a command that came from a model is data, and a surface must render it as data."""
        T.get().set_level(T.INTENT)
        assert T.intent("I will read the config").trusted is False

    def test_a_command_is_untrusted(self):
        T.get().set_level(T.SYSTEM)
        assert T.command("rm -rf /", exit_code=0).trusted is False

    def test_a_harness_decision_is_trusted(self):
        """The harness reporting its own gate outcome is the harness speaking, not the model."""
        T.get().set_level(T.DECISIONS)
        assert T.decision("run_bash", allowed=False).trusted is True


class TestMovingTheDial:
    def test_the_level_can_be_moved(self):
        dial = T.get()
        assert dial.set_level(5) == 5
        assert dial.level == 5

    def test_it_clamps_rather_than_rejecting(self):
        dial = T.get()
        assert dial.set_level(99) == T.MAX_LEVEL
        assert dial.set_level(-4) == T.SILENT

    def test_a_nonsense_value_leaves_the_dial_alone(self):
        """A slider that silently ignores a movement teaches the user the control is broken."""
        dial = T.get()
        dial.set_level(4)
        dial.set_level("not a number")
        assert dial.level == 4

    def test_moving_the_dial_is_itself_an_event(self):
        """Otherwise a reader is left wondering why the feed stopped."""
        dial = T.get()
        dial.set_level(T.PULSE)
        dial.set_level(T.FILES)
        recent = dial.recent(limit=5)
        assert any(e.kind == "transparency" for e in recent)


class TestSubscribers:
    def test_a_subscriber_receives_live_events(self):
        dial = T.get()
        dial.set_level(T.ACTIONS)
        queue = dial.subscribe()
        try:
            T.emit(T.ACTIONS, "action", summary="live")
            assert len(queue) >= 1
        finally:
            dial.unsubscribe(queue)

    def test_unsubscribing_stops_delivery(self):
        dial = T.get()
        dial.set_level(T.ACTIONS)
        queue = dial.subscribe()
        dial.unsubscribe(queue)
        T.emit(T.ACTIONS, "action", summary="after")
        assert len(queue) == 0

    def test_a_dead_subscriber_does_not_stop_the_run(self):
        """A feed must never be able to fail the work it is describing."""
        dial = T.get()
        dial.set_level(T.ACTIONS)

        class Exploding:
            def append(self, _item):
                raise RuntimeError("this subscriber is broken")

        broken = Exploding()
        dial._subscribers.append(broken)
        try:
            assert T.emit(T.ACTIONS, "action", summary="still works") is not None
        finally:
            if broken in dial._subscribers:
                dial._subscribers.remove(broken)

    def test_the_buffer_is_bounded(self):
        """This is a live feed, not a log: `audit.jsonl` is the durable record."""
        assert T.BUFFER_SIZE <= 1000


class TestTheSseFrame:
    def test_an_event_serialises_to_a_frame(self):
        T.get().set_level(T.ACTIONS)
        event = T.emit(T.ACTIONS, "action", summary="hello")
        frame = T.as_sse(event)
        assert frame.startswith("data: ")
        assert frame.endswith("\n\n")
        import json

        assert json.loads(frame[6:].strip())["summary"] == "hello"


class TestNoLayerRequirementDrifts:
    def test_every_layer_above_silent_that_needs_detail_names_it(self):
        """A layer cannot silently degrade into a generic event because nobody decided what belongs in it."""
        for number, required in T.LAYER_REQUIRES.items():
            assert number > T.SILENT
            assert required, f"layer {number} ({T.LAYER_NAMES[number]}) declares no required detail"
