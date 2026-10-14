"""Cooperative cancellation for a turn that is already streaming.

Before this, `/api/cancel` set a flag that nothing ever read. The session reported "cancel
requested" and the turn ran to completion anyway -- which is worse than an unimplemented button,
because the user believes it stopped.

The mechanism is deliberately small: an agent holds a `threading.Event`, the stream loops check it
between chunks, and raising :class:`TurnCancelled` unwinds out of the loop. Unwinding rather than
breaking matters, because the provider's stream is an open HTTP response; leaving the `with` block
by exception is what closes the connection, and closing the connection is what actually stops the
tokens being generated and billed. A `break` would return normally from inside the context manager
and, depending on the SDK, could leave the response draining in the background.

What cancellation guarantees, stated exactly:

* No further chunks are *consumed* past the current one.
* The partial answer is kept. Text that arrived before the cancel is real output the user watched
  appear; discarding it would be surprising, and the transcript should show what was actually said.
* A cancelled turn is recorded as cancelled, not as an error and not as a completed answer.
"""

from __future__ import annotations

import threading


class TurnCancelled(Exception):
    """Raised inside a stream loop when the turn has been cancelled.

    Carries the partial text so far, so the caller can keep what the user already saw rather than
    throwing away text that was on screen a moment ago.
    """

    def __init__(self, partial: str = "") -> None:
        super().__init__("the turn was cancelled")
        self.partial = partial or ""


class CancelToken:
    """A one-shot cancellation flag an agent's stream loop can watch.

    A plain `threading.Event` behind a named type, because the *name* is the documentation: a bare
    Event on an agent could be anything, while `cancel_token.request()` at a call site says what is
    happening. The check is a single attribute read, which is what makes it affordable once per
    streamed chunk.
    """

    __slots__ = ("_event",)

    def __init__(self) -> None:
        self._event = threading.Event()

    def request(self) -> None:
        self._event.set()

    def clear(self) -> None:
        self._event.clear()

    @property
    def requested(self) -> bool:
        return self._event.is_set()

    def raise_if_cancelled(self, partial: str = "") -> None:
        """Stop the turn if cancellation was requested. Cheap enough for every chunk."""
        if self._event.is_set():
            raise TurnCancelled(partial)


def attach(agent: object, event: threading.Event | None = None) -> CancelToken:
    """Give an agent a cancel token, reusing one it already has.

    Called on every turn rather than at construction, so an agent built by any path -- CLI, browser,
    or a sub-agent -- ends up with one. An agent that lacks the attribute is the failure this
    prevents: the stream loop would raise AttributeError mid-turn.
    """
    token = getattr(agent, "cancel_token", None)
    if isinstance(token, CancelToken):
        token.clear()
        return token
    token = CancelToken()
    try:
        object.__setattr__(agent, "cancel_token", token)
    except Exception:  # noqa: BLE001 - a slotted class would refuse; the caller only needs the token
        pass
    return token
