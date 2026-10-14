"""Dashboard sessions on disk, so a restart does not erase every conversation.

Session ids and transcripts lived only in memory. That had two visible consequences:

* A locus's conversation could not survive a restart. Recreating the session under the same id kept
  the *identity* honest -- the panel could still address it -- but the transcript behind that id was
  gone, so "so make it portrait instead" had nothing to refer to after a restart. The earlier fix
  that made a locus remember its session therefore only worked until the next restart.
* Every browser conversation vanished on restart, and a reload mid-conversation left a session list
  that had been silently emptied.

One JSON file per session, written after each turn and read back at startup. Deliberately not a
database: the shape is a handful of sessions holding a list of messages, the write is one turn's
worth of data, and a corrupt file must degrade to "that one session is unavailable" rather than to
a library error at import time.

What is stored is the transcript and the state a UI needs to describe it. What is *not* stored is
anything that belongs to a live agent -- an API client, a stream, an MCP bridge. Those are rebuilt
on demand, which is what keeps this file small and readable.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from .. import constants

# Roles worth keeping. A tool result is not part of the conversation a person reads back, and
# storing them would multiply the file size by ten for content already summarised in the trajectory.
KEPT_ROLES = ("user", "assistant")

# Messages kept per session. A long session is worth restoring in full for the agent's benefit, but
# an unbounded file is a liability; the cap is high enough that it is only ever reached by a session
# nobody would scroll through.
MAX_MESSAGES = 400


def sessions_dir() -> Path:
    """Where session files live, resolved on every call.

    Deliberately a function rather than a module constant. A constant binds `CONFIG_DIR` at import
    time, so a process that changes the config directory afterwards -- the test suite does, and any
    caller honouring a runtime `MYRIAXON_CONFIG_DIR` does -- would keep writing sessions to the old
    location. That is exactly how it failed: a fresh app in a test built over the previous test's
    scratch directory and restored its sessions, so the app was not fresh.
    """
    return Path(constants.CONFIG_DIR) / "sessions"


def session_file(session_id: str) -> Path:
    safe = "".join(ch for ch in str(session_id) if ch.isalnum() or ch in "-_")[:64]
    return sessions_dir() / f"{safe}.json"


def save(
    session_id: str,
    *,
    state: Any,
    messages: list[dict],
    trajectory: list[dict] | None = None,
) -> Path | None:
    """Write one session. Returns the path, or None when it could not be written.

    Never raises: a session that cannot be saved is a lost transcript, not a failed turn, and
    raising here would surface a disk problem as a chat error.
    """
    try:
        sessions_dir().mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "saved_at": time.time(),
            "state": _state_dict(state),
            "messages": [
                {"role": str(m.get("role")), "text": str(m.get("text") or "")}
                for m in (messages or [])[-MAX_MESSAGES:]
                if str(m.get("role")) in KEPT_ROLES
            ],
            "trajectory": _trajectory(trajectory),
        }
        target = session_file(session_id)
        # Written to a temporary file and moved into place: a crash midway through the write must
        # not leave a truncated JSON file that reads as a corrupt session forever after.
        temp = target.with_suffix(".json.tmp")
        temp.write_text(json.dumps(payload, indent=1), encoding="utf-8", newline="\n")
        temp.replace(target)
        return target
    except Exception:  # noqa: BLE001
        return None


def load(session_id: str) -> dict | None:
    """Read one session, or None when absent or unreadable."""
    path = session_file(session_id)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        # A corrupt file is reported as absent rather than repaired by guesswork. The alternative --
        # a partial transcript presented as the whole conversation -- is worse than a missing one.
        return None
    return data if isinstance(data, dict) else None


def load_all(limit: int = 50) -> list[dict]:
    """Every readable session, newest first, each carrying its id."""
    if not sessions_dir().is_dir():
        return []
    entries: list[dict] = []
    for path in sessions_dir().glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(data, dict):
            continue
        data.setdefault("state", {})
        data["state"]["id"] = data["state"].get("id") or path.stem
        entries.append(data)
    entries.sort(key=lambda d: float(d.get("saved_at") or 0), reverse=True)
    return entries[:limit]


def delete(session_id: str) -> bool:
    path = session_file(session_id)
    try:
        if path.is_file():
            path.unlink()
            return True
    except OSError:
        pass
    return False


def _state_dict(state: Any) -> dict:
    """A JSON-safe view of a session's state."""
    if state is None:
        return {}
    if is_dataclass(state):
        try:
            return json.loads(json.dumps(asdict(state), default=str))
        except Exception:  # noqa: BLE001
            pass
    for attribute in ("to_dict",):
        method = getattr(state, attribute, None)
        if callable(method):
            try:
                return json.loads(json.dumps(method(), default=str))
            except Exception:  # noqa: BLE001
                pass
    out: dict = {}
    for key in ("id", "title", "provider", "model", "mode", "turn_count", "locus", "created_at",
                "updated_at"):
        value = getattr(state, key, None)
        if value is not None:
            out[key] = value if isinstance(value, (str, int, float, bool)) else str(value)
    return out


def _trajectory(trajectory: list[dict] | None) -> list[dict]:
    """The tool calls, trimmed to what the Trajectory tab renders."""
    out: list[dict] = []
    for step in (trajectory or [])[-200:]:
        if not isinstance(step, dict):
            continue
        out.append({
            "tool": str(step.get("tool") or ""),
            "args": step.get("args") if isinstance(step.get("args"), dict) else {},
            "is_error": bool(step.get("is_error")),
            "result": str(step.get("result") or "")[:2000],
            "duration_ms": step.get("duration_ms"),
        })
    return out
