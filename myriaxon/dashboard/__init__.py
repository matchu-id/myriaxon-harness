"""The Myriaxon dashboard: a local web UI over the same agent the CLI runs.

Named "Myriaxon" in the interface. Everything here is standard library -- no web framework, no
build step, no external service -- so it runs wherever the CLI runs.
"""
from .session import Session, SessionManager
from .events import EventBus, Event
from .console import EventConsole

__all__ = ["Session", "SessionManager", "EventBus", "Event", "EventConsole"]
