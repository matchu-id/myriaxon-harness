"""Conflict detection on shared artifacts: notice when two members are about to fight over one file.

Level 4 requirement 4.11. Two members writing the same file is not a merge problem to solve later -- it is a
problem to DETECT, because the second writer silently destroys the first writer's work and neither of them
finds out.

WHY THIS IS DETECTION AND NOT LOCKING

A lock is the tempting design and it is the wrong one here. A lock held by a member that dies is a lock
nobody releases, which turns one member's crash into a stopped swarm -- and this harness has already been
bitten by exactly that shape, which is why the board uses leases with expiry rather than locks. So nothing
here blocks anyone. It answers two questions and leaves the decision to the caller:

  * **Has this file changed since I read it?** A content digest taken at read time and checked at write time
    catches the case that actually loses work: A reads, B writes, A writes back, B's change is gone.
  * **Is somebody else working on this file right now?** An advisory intent, recorded on the board, so a
    member can choose a different file rather than discover the collision afterwards.

WHAT IT DOES NOT DO

It cannot see a write that happens between the check and the write, because that would need a lock -- see
above. The window is milliseconds and closing it would cost the property that makes the board survivable.
Recorded rather than hidden: this narrows the collision window, it does not eliminate it, and a caller that
needs certainty needs a lock with an expiry, which is a different design.

Nor does it merge. It reports "this changed underneath you", which is the information a member needs to
decide whether to redo the work or hand it back.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from pathlib import Path

from .atomic_io import atomic_write_text
from .locus_swarm import ensure_layout, safe_id, shared_dir

INTENT_FILENAME = "artifact-intents.json"

# How long an intent is believed. Deliberately the same order as a claim lease: long enough to cover a
# piece of work, short enough that a member that dies stops reserving the file.
INTENT_SECONDS = 900.0

# Artifacts are identified by their path relative to a workspace, so two members using different absolute
# roots for the same logical file still collide. Absolute paths are normalised to a trailing form rather
# than used raw, because the same file reached by two different roots is the collision nobody sees coming.
MAX_TRACKED = 500


def _path() -> Path:
    ensure_layout()
    return shared_dir() / INTENT_FILENAME


def digest(path: Path | str) -> str:
    """A content digest, or "" when the file cannot be read.

    Empty means "unknown", NOT "unchanged". A caller that treats an unreadable file as unchanged would skip
    the check exactly when the file is most likely to be contended.
    """
    target = Path(path)
    try:
        return hashlib.sha256(target.read_bytes()).hexdigest()
    except OSError:
        return ""


def artifact_key(path: Path | str, *, root: Path | str | None = None) -> str:
    """A stable name for an artifact, relative to a root when one is given.

    Relative because the point is to collide: `/home/a/out.svg` and `/mnt/c/out.svg` are the same artifact to
    two members, and an absolute key would let each keep its own private opinion about who holds it.
    """
    target = Path(path)
    if root is not None:
        try:
            target = target.resolve().relative_to(Path(root).resolve())
        except (ValueError, OSError):
            # Outside the root: fall back to the name, which still collides for the common case of two
            # members naming the same file the same way.
            target = Path(target.name)
    return safe_id(str(target).replace("\\", "/").lstrip("/")) or "artifact"


def _load() -> dict:
    import json

    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(intents: dict) -> None:
    import json

    atomic_write_text(_path(), json.dumps(intents, indent=2, ensure_ascii=False, sort_keys=True))


def declare(path: Path | str, member: str, *, root: Path | str | None = None,
            note: str = "", seconds: float = INTENT_SECONDS) -> tuple[bool, str]:
    """Say you are about to work on an artifact. Returns (ok, problem-or-warning).

    A conflict is reported as a WARNING with ok=True rather than a refusal. Refusing would make one member's
    crash stop another member's work, and the caller is better placed to decide: rewriting the same file may
    be exactly the intent, or it may be the mistake this exists to catch.
    """
    who = safe_id(member) if (member or "").strip() else ""
    if not who:
        return False, "`member` is required: an unattributed claim cannot be resolved"

    now = time.time()
    key = artifact_key(path, root=root)

    # Read the existing claim for THIS key first, so the warning can see another member's live claim -- and
    # then drop it, so it does not survive into the stored intents being re-written.
    #
    # Two mistakes were made here and both were caught by tests, which is why the order is spelled out:
    #
    #   * the filter was `... and k != key or k == key`, whose precedence meant the `or` branch reinstated
    #     the very entry it had excluded. An expired claim was therefore still found by the warning check,
    #     and a member was warned about a reservation that had already lapsed -- the exact "a dead member
    #     blocks the file forever" failure the expiry exists to prevent.
    #   * removing the key before reading `held` silenced the warning entirely, so a genuine concurrent
    #     claim went unreported. Read the claim, then drop it.
    existing = _load()
    prior = existing.get(key) or {}
    held = prior if (isinstance(prior, dict) and float(prior.get("expires_at", 0)) > now) else {}

    warning = ""
    if held and safe_id(str(held.get("member", ""))) != who:
        warning = (
            f"{held.get('member')} declared work on {key} "
            f"{int(now - float(held.get('claimed_at', now)))}s ago"
            + (f" ({held.get('note')})" if held.get("note") else "")
            + ". Two members writing one file means the second write erases the first. Take a different "
              "artifact, or coordinate on the board first."
        )

    # Now drop this key and every expired entry, so nothing stale is carried forward.
    intents = {
        k: v for k, v in existing.items()
        if isinstance(v, dict) and float(v.get("expires_at", 0)) > now and k != key
    }

    intents[key] = {
        "member": who,
        "claimed_at": float(held.get("claimed_at", now)) if held.get("member") == who else now,
        "expires_at": now + float(seconds),
        "note": (note or "").strip()[:200],
        # Recorded so a later writer can tell "nothing happened since I read it" from "somebody wrote".
        "digest": digest(path),
    }
    if len(intents) > MAX_TRACKED:
        # Keep the newest by expiry. An unbounded intent file is a slow leak in a mechanism nobody watches.
        newest = sorted(intents.items(), key=lambda kv: float(kv[1].get("expires_at", 0)), reverse=True)
        intents = dict(newest[:MAX_TRACKED])
    _save(intents)
    return True, warning


def release(path: Path | str, member: str, *, root: Path | str | None = None) -> bool:
    """Drop your intent once you are done, so a peer is not warned about a finished file."""
    key = artifact_key(path, root=root)
    intents = _load()
    held = intents.get(key)
    if not held or safe_id(str(held.get("member", ""))) != safe_id(member):
        return False
    intents.pop(key, None)
    _save(intents)
    return True


def holders(path: Path | str, *, root: Path | str | None = None) -> list[str]:
    """Who else currently says they are working on this artifact."""
    key = artifact_key(path, root=root)
    now = time.time()
    entry = _load().get(key)
    if not entry or float(entry.get("expires_at", 0)) <= now:
        return []
    return [safe_id(str(entry.get("member", "")))]


def check_write(path: Path | str, member: str, expected_digest: str,
                *, root: Path | str | None = None) -> tuple[bool, str]:
    """The check to run BEFORE writing a file you read earlier. Returns (safe_to_write, problem).

    This is the one that catches lost work: read the digest, do the work, then ask whether the file still
    has that digest. If it does not, somebody wrote in between and writing now would erase them.
    """
    if not expected_digest:
        return True, ""
    current = digest(path)
    if not current:
        # The file is gone or unreadable. Not a conflict -- a different situation the caller should know
        # about rather than have reported as one.
        return True, f"{artifact_key(path, root=root)} could not be read; writing will create or replace it"
    if current != expected_digest:
        others = holders(path, root=root)
        who = f" ({', '.join(others)})" if others else ""
        return False, (
            f"{artifact_key(path, root=root)} changed since you read it{who}. Writing now would erase that "
            "change. Re-read it and redo your edit, or hand the work back with swarm_release."
        )
    return True, ""


def active(*, root: Path | str | None = None) -> dict[str, str]:
    """Every artifact somebody currently claims, as {key: member}. Expired entries are dropped."""
    now = time.time()
    out: dict[str, str] = {}
    for key, entry in _load().items():
        if not isinstance(entry, dict):
            continue
        if float(entry.get("expires_at", 0)) <= now:
            continue
        out[key] = safe_id(str(entry.get("member", "")))
    return out


def format_intents(*, root: Path | str | None = None) -> str:
    """A short report for a status panel."""
    current = active(root=root)
    if not current:
        return "No artifact conflicts: nobody has declared work on a shared file."
    lines = [f"{len(current)} artifact(s) currently declared:"]
    for key, member in sorted(current.items()):
        lines.append(f"  {key}  <- {member}")
    return "\n".join(lines)
