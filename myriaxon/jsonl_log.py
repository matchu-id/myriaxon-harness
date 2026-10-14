"""Append-only JSONL that survives a crash mid-write.

Extracted after the SAME bug was found twice, in two modules, by the same test shape. `audit.py`
appended its entries without checking whether the file ended in a newline, and `usage.py` was written
with the identical flaw on the same day. A crash mid-write leaves a partial line with no trailing
newline, and the next append then concatenates onto the fragment -- producing two JSON objects on one
line, which every reader skips. The entry is lost silently, and the loss is worst exactly when the
process is dying, which is when the record matters most.

One implementation, used by both, because "remember to repair the tail" is not a rule anyone
remembers twice.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

# How much of the tail to scan when looking for the last complete line. An append-only log grows
# without bound, so reading it whole on every write would make appending O(n^2) over a long-lived
# installation.
TAIL_SCAN_BYTES = 65536


def tail_state(path: Path) -> tuple[dict | None, int]:
    """The last complete record and the byte offset where the next write should start.

    `offset` is the position just past the final newline. Writing there -- rather than at the end of
    the file -- discards a torn fragment instead of appending to it.
    """
    try:
        if not path.exists():
            return None, 0
        size = path.stat().st_size
        if size == 0:
            return None, 0
        with path.open("rb") as handle:
            block = min(size, TAIL_SCAN_BYTES)
            handle.seek(size - block)
            tail = handle.read(block).decode("utf-8", "replace")
    except OSError:
        return None, 0

    last_newline = tail.rfind("\n")
    if last_newline == -1:
        # Either one enormous line or a torn first write. Rewrite from the start rather than append
        # to something that cannot be parsed.
        return None, 0
    offset = size - block + last_newline + 1
    for line in reversed(tail[:last_newline].splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue          # a torn or foreign line; keep looking back
        if isinstance(record, dict):
            return record, offset
    return None, offset


def append_jsonl(path: Path, record: dict) -> bool:
    """Append one record, repairing a torn tail first. Never raises; returns whether it was written.

    Best-effort by design: this is called from inside tool dispatch and turn completion, so a failure
    here must not break the work it is recording. An audit log or ledger that aborts the operation it
    describes is worse than no log.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _previous, offset = tail_state(path)
        mode = "r+" if path.exists() else "w+"
        with path.open(mode, encoding="utf-8") as handle:
            handle.seek(offset)
            handle.truncate()
            handle.write(json.dumps(record, default=str) + "\n")
        return True
    except Exception:  # noqa: BLE001
        return False


def read_jsonl(path: Path, limit: int = 0) -> list[dict]:
    """Every complete record, oldest first. Malformed lines are skipped, never fatal."""
    try:
        if not path.exists():
            return []
        rows: list[dict] = []
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                rows.append(record)
        return rows[-limit:] if limit else rows
    except OSError:
        return []
