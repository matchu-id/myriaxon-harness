"""Closure-bound run capability: authority that is re-checked at every use and dies with the run.

## The gap this closes

Myriaxon has several things that outlive the work they belong to: a `ToolContext` passed into a plugin handler,
a background job object, a sub-agent handle, a context manager opened for a Locus run. Nothing in the codebase
could answer "is the run this belongs to still alive?" — so a plugin holding a context, or a thread that woke
after a cancellation, could keep acting on behalf of a run that had already ended.

That is the class OpenClaw's `AgentHarnessHostCapabilities` closes (verified in
`src/agents/harness/host-capability.ts`). Their object is frozen, closure-bound to **one admitted run**, and
every method calls `assertActive()` first, with a second assertion *around* every `await` — because the await is
exactly where authority can be lost. Their tests pin the whole family: "prepared execution aborts when the
capability closes", "deferred policy results after authority release are rejected", "constructing a host after
admission closes fails closed", "execution preparers are rejected after revocation".

## What is copied, and what is dropped

Copied: the frozen capability handle, `assertActive()` at every entry, the double-check around an await, a
fail-closed refusal to hand out a handle once closed, and reasons that are recorded rather than swallowed.

Dropped deliberately: their delegated-authority apparatus — `operationalRunInstance`, Gateway liveness,
lifecycle rotation, worker-thread claim receipts, node placement. Those exist because one Gateway hosts many
tenants across many processes. This is a `bool` and a `threading.Event`, which is what one process on one machine
actually needs. Their own capability ladder says to take the first *fitting* step.

## The one rule that matters

**Assert before the work, not after.** A check that runs after the side effect has already happened tells you
that you should not have done it.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable


class RunRevoked(RuntimeError):
    """Raised when work is attempted on behalf of a run that is no longer active.

    A distinct type rather than a bare `RuntimeError` so a caller can catch exactly this and treat it as "stop",
    as against a genuine failure of the work itself.
    """

    def __init__(self, capability: str, reason: str) -> None:
        super().__init__(f"run capability {capability!r} is no longer active: {reason}")
        self.capability = capability
        self.reason = reason


@dataclass
class RunCapability:
    """A handle to one run's authority. Re-checked at every use.

    Construct one at the start of a unit of work, pass it to anything that will act on that work's behalf, and
    `close()` it when the work ends, is cancelled, or fails. Anything holding the handle then fails `assert_active`
    rather than acting.
    """

    name: str
    run_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    _closed: bool = False
    _reason: str = ""
    _event: threading.Event = field(default_factory=threading.Event)
    _lock: threading.RLock = field(default_factory=threading.RLock)
    _checks: int = 0

    # ---- state

    @property
    def active(self) -> bool:
        """Whether this capability is still usable. Never raises -- for a caller that only wants to branch."""
        with self._lock:
            return not self._closed

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def reason(self) -> str:
        return self._reason

    @property
    def checks(self) -> int:
        """How many times authority has been asserted. Read by tests and by a status line."""
        return self._checks

    def event(self) -> threading.Event:
        """A `threading.Event` that is set when this capability closes.

        For a long-running loop that should stop promptly rather than at its next `assert_active`. Handing out
        the event rather than a callback keeps the loop in control of when it notices.
        """
        return self._event

    # ---- authority

    def assert_active(self) -> None:
        """Raise `RunRevoked` unless this run is still live.

        Called at the *entry* of anything that acts on the run's behalf, and again after any `await`-equivalent --
        a model call, a tool call, a sleep, a thread join. The window that matters is the one between checking and
        acting, and an await is where data changes hands.
        """
        with self._lock:
            self._checks += 1
            if self._closed:
                raise RunRevoked(self.name, self._reason or "the run ended")

    def guard(self, description: str = "") -> Callable[[], None]:
        """A zero-argument callable that asserts, for handing to code that takes a re-validator.

        OpenClaw calls these "revalidators": a closure that re-checks authority at the moment a prepared action
        is finally performed. Returning a callable rather than requiring the caller to hold the capability keeps
        the surface small and makes forgetting the check awkward rather than natural.
        """
        def _guard() -> None:
            self.assert_active()
        _guard.__doc__ = f"assert the run is still active before {description or 'acting'}"
        return _guard

    def run_guarded(self, action: Callable[[], Any], description: str = "") -> Any:
        """Assert, run `action`, assert again. Returns the action's result.

        The second assertion is the point. If `action` awaited anything -- a subprocess, a model call, a write --
        the run may have been cancelled while it ran, and the result must not be treated as valid. Raising here
        is the honest outcome: the work completed but its result is no longer admissible.
        """
        self.assert_active()
        result = action()
        self.assert_active()
        return result

    # ---- lifetime

    def close(self, reason: str = "the run ended") -> bool:
        """End the run. Idempotent; returns whether this call was the one that closed it.

        Never fails and never raises: closing must be possible from a `finally`, from a signal handler, and from
        a cancel path, and a close that could itself fail is a close nobody can rely on.
        """
        with self._lock:
            if self._closed:
                return False
            self._closed = True
            self._reason = reason
            self._event.set()
            return True

    def __enter__(self) -> "RunCapability":
        self.assert_active()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        # Closed on the way out whatever happened, including on an exception -- a run that failed must not leave
        # a live capability behind for a later holder to act through.
        reason = "the run raised" if exc_type is not None else "the run finished"
        self.close(reason)
        return False


def new_run_capability(name: str, run_id: str = "", **metadata: Any) -> RunCapability:
    """Create a capability for a unit of work. Convenience wrapper for the common construction."""
    return RunCapability(name=name, run_id=run_id, metadata=dict(metadata))


def assert_active(*capabilities: RunCapability) -> None:
    """Assert several capabilities at once, for work that needs all of them to still hold.

    A Locus run needs its own capability *and* whatever granted it authority. Checking them together makes the
    requirement explicit at the call site instead of emerging from whichever one happens to be checked first.
    """
    for capability in capabilities:
        if capability is not None:
            capability.assert_active()


def open_capability_file_guard(capability: RunCapability, path: Path) -> Callable[[], None]:
    """A revalidator for a file write prepared under a capability.

    The shape OpenClaw uses for `prepareMutableFileApproval`: the write is *prepared* (path resolved, parent
    checked, content built) while authority holds, and the returned guard must be called immediately before the
    write actually happens. Preparing and writing are separated because preparation is where the checks belong
    and the write is where the authority can have expired.
    """
    def _guard() -> None:
        capability.assert_active()
    return _guard
