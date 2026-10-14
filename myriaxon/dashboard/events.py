"""Thread-safe event fan-out for the dashboard's server-sent-event streams.

The agent runs synchronously on a worker thread and reports what it is doing as it goes (assistant
text, tool calls, tool results, errors). Each connected browser holds its own subscriber queue, so a
slow or closed client can never block the agent -- publishing drops into a per-subscriber queue and
never waits on the network.

Every subscriber receives the full history of events it missed before it connected (replay), so
opening the page mid-turn shows the conversation so far rather than starting blank.
"""
from __future__ import annotations

import itertools
import queue
import threading
import time
from collections import deque
from typing import Any


class Event(dict):
    """A single stream message. Kept as a dict so json.dumps works without a custom encoder."""

    @staticmethod
    def make(kind: str, seq: int, **payload: Any) -> "Event":
        event = Event(payload)
        event["type"] = kind
        event["seq"] = seq
        event["ts"] = time.time()
        return event


class EventBus:
    """One bus per session. Subscribers get replay plus live events, in order, exactly once."""

    # Ring buffer size: enough to reconstruct a long turn (thousands of token deltas) without
    # unbounded memory growth if a client disconnects and never returns.
    HISTORY_LIMIT = 4000

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counter = itertools.count(1)
        self._history: deque[Event] = deque(maxlen=self.HISTORY_LIMIT)
        self._subscribers: set[queue.Queue] = set()
        self._closed = False

    # -- publishing ---------------------------------------------------------------
    def publish(self, kind: str, **payload: Any) -> Event:
        with self._lock:
            event = Event.make(kind, next(self._counter), **payload)
            self._history.append(event)
            targets = list(self._subscribers)
        for subscriber in targets:
            try:
                subscriber.put_nowait(event)
            except queue.Full:
                # A subscriber that cannot keep up loses events rather than stalling the agent.
                pass
        return event

    def close(self) -> None:
        with self._lock:
            self._closed = True
            targets = list(self._subscribers)
        for subscriber in targets:
            try:
                subscriber.put_nowait(None)  # sentinel: stream finished
            except queue.Full:
                pass

    # -- subscribing --------------------------------------------------------------
    def subscribe(self, replay: bool = True) -> tuple[queue.Queue, list[Event]]:
        """Returns (queue, replay_events). The queue receives events after the replay list."""
        subscriber: queue.Queue = queue.Queue(maxsize=2000)
        with self._lock:
            missed = list(self._history) if replay else []
            self._subscribers.add(subscriber)
            if self._closed:
                subscriber.put_nowait(None)
        return subscriber, missed

    def unsubscribe(self, subscriber: queue.Queue) -> None:
        with self._lock:
            self._subscribers.discard(subscriber)

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)

    def history(self) -> list[Event]:
        with self._lock:
            return list(self._history)
