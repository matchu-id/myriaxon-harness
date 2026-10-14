"""Steering: redirect a turn that is already running, without cancelling it.

## The gap, precisely

Myriaxon could **cancel** a turn — `cancellation.py` provides `CancelToken` and `attach`, and the stream loop watches
it. It could not **redirect** one. So a correction ("no, use the other file") had to wait for the turn to finish,
which on a long tool-using turn means watching it complete the wrong thing first.

Both new rivals have this: PiHarness exposes `inner.steer(message)` through `harness.steer`, and Opencode's
`promoteSteers` promotes a durable `steer` input **at the next provider-turn boundary** of an active drain
(`packages/core/src/session/input.ts`).

## Why this is not the follow-up queue

A follow-up message is delivered **after** the current turn. A steer changes what the current turn is **doing**. The
timing is the whole feature: a steer that arrived after the turn would be a follow-up, and Myriaxon already had a
version of that for Loci and heartbeats. What was missing is delivery *mid-turn*.

## Why the queue is drained at the step boundary and nowhere else

The step boundary is already load-bearing: `run_budget.check()` runs there and nowhere else, for a reason recorded
in `agent.py` — interrupting mid-step leaves a `tool_use` block with no matching `tool_result`, and the API then
rejects the transcript on every later turn. A steer inserted anywhere else would corrupt the conversation in the same
way. So it is appended **between** steps, as a real user turn, and the next request carries it.

## The decision that carries the most risk

**A steer must carry its OWN source.** `goal_authority` decides whether a turn may complete a goal by reading its
provenance. If a steer were appended as a plain user turn, then a steer produced by a scheduler, a sub-agent or a Locus
would arrive looking like something a person typed — and would silently acquire the authority to mark work complete.
That is exactly the hole `goal_authority` was built to close, so `Steer` requires a source and `drain()` returns it
with each message rather than flattening them into `user` turns.

`SOURCE_HUMAN` is never a default. An unlabelled steer resolves to `SOURCE_SYSTEM`, the same inversion
`goal_authority.source_of` already applies and for the same reason: a producer must *supply* human authority rather
than inherit it by leaving a field blank.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

# Re-exported so a caller does not have to import two modules to label a steer correctly.
from .goal_authority import (  # noqa: F401
    SOURCE_DOT,
    SOURCE_HUMAN,
    SOURCE_ROUND,
    SOURCE_SCHEDULE,
    SOURCE_SUBAGENT,
    SOURCE_SYSTEM,
    source_of,
)

# A steer queue is bounded. An unbounded one is a way for a misbehaving producer to grow a turn without limit, and
# past a handful of competing corrections the model cannot honour them anyway.
MAX_QUEUED = 8

# A single steer is bounded too: it is a correction, not a document.
MAX_STEER_CHARS = 4_000


@dataclass(frozen=True)
class Steer:
    """One mid-turn correction, with the provenance that decides its authority."""

    text: str
    source: str
    queued_at: float = 0.0
    # Set when the steer was produced by a specific producer, for the log. Not used for authority.
    origin: str = ""

    def render(self) -> str:
        return f"[{self.source}] {self.text[:120]}"


@dataclass
class SteerQueue:
    """Steers waiting for the next step boundary.

    Deliberately a plain object rather than a thread-safe one: the producers are the CLI's own input path, the
    scheduler's daemon and a Locus's run loop, all of which already coordinate through the agent. Adding a lock here
    would suggest a concurrency guarantee this does not make, and the real serialisation point is the step boundary.
    """

    _queued: list[Steer] = field(default_factory=list)
    # How many steers have ever been accepted, so a caller can tell "none arrived" from "all were drained".
    accepted: int = 0
    dropped: int = 0
    last_drain_at: float = 0.0

    def offer(self, text: str, source: str = "", *, origin: str = "") -> tuple[bool, str]:
        """Queue a steer. Returns `(accepted, reason)`.

        Refusals are named rather than silent: a producer whose steer was dropped needs to know, or it will believe
        the agent was told something it never heard.
        """
        cleaned = " ".join((text or "").split())
        if not cleaned:
            self.dropped += 1
            return False, "an empty steer was refused: there is nothing to deliver"
        if len(cleaned) > MAX_STEER_CHARS:
            cleaned = cleaned[:MAX_STEER_CHARS]
        if len(self._queued) >= MAX_QUEUED:
            self.dropped += 1
            return False, (
                f"the steer queue already holds {MAX_QUEUED}, which is its limit. The model cannot honour more "
                "competing corrections than that in one step, so this one was refused rather than queued to be "
                "ignored."
            )
        # `source_of` maps an unknown or blank source to `system`, never to `human`. A steer that does not say where
        # it came from must not arrive with the authority of a person.
        self._queued.append(Steer(
            text=cleaned,
            source=source_of(source),
            queued_at=time.time(),
            origin=str(origin or ""),
        ))
        self.accepted += 1
        return True, ""

    def pending(self) -> int:
        return len(self._queued)

    def drain(self) -> list[Steer]:
        """Take everything queued. Idempotent: a second call returns nothing.

        Returning a list rather than appending to the transcript here keeps the two decisions separate — what was
        steered, and how a turn records it — so a caller that wants to log a steer without delivering it can.
        """
        taken = list(self._queued)
        self._queued.clear()
        if taken:
            self.last_drain_at = time.time()
        return taken

    def peek(self) -> list[Steer]:
        """What is waiting, without taking it. For a status line."""
        return list(self._queued)

    def clear(self) -> int:
        """Drop everything queued. Returns how many were dropped, so a caller can report it rather than guess."""
        count = len(self._queued)
        self.dropped += count
        self._queued.clear()
        return count

    def render(self) -> str:
        if not self._queued:
            return (
                f"no steers waiting ({self.accepted} accepted, {self.dropped} dropped this run)"
            )
        lines = [f"{len(self._queued)} steer(s) waiting, delivered at the next step boundary:"]
        for steer in self._queued:
            lines.append(f"  {steer.render()}")
        return "\n".join(lines)


def as_user_turn(steer: Steer) -> dict:
    """A steer as a message the provider will accept.

    Shaped exactly like any other user turn, because that is what the API requires — the *provenance* is carried
    alongside it in the `Steer`, not encoded into the message. Wrapping the text in a marker the model must parse
    would be a second, weaker way of stating the same fact, and a model can be talked out of reading a marker.
    """
    return {"role": "user", "content": steer.text}


def provenance_note(steers: list[Steer]) -> str:
    """A line for the transcript log, naming who steered.

    Their sources are recorded rather than assumed because a steer from a scheduler or a sub-agent must be
    distinguishable afterwards from one a person typed — that distinction is what `goal_authority` reads.
    """
    if not steers:
        return ""
    if len(steers) == 1:
        return f"steered mid-turn by {steers[0].source}"
    sources = ", ".join(sorted({s.source for s in steers}))
    return f"steered mid-turn by {sources} ({len(steers)} message(s))"
