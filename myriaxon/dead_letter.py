"""A dead-letter store: where a failed run goes so somebody can find out why.

Level 4 requirement 4.17. Before this, a run that died left almost nothing behind. A Locus recorded a line
in its own activity log, the run-state file was deleted, and the only way to answer "what has been failing
and why" was to read logs by hand -- which nobody does, so a repeated failure stayed invisible until it had
happened many times.

WHY A STORE AND NOT JUST LOGGING

A log answers "what happened at 14:02". This answers "what is broken right now", which is a different
question and needs a different shape: one record per failure, queryable by Locus and by kind, with a COUNT,
because the useful signal is a failure repeating rather than a failure happening. The count is what turns
noise into a finding.

WHAT IS DELIBERATELY NOT STORED

Not the full transcript, not the prompt, not tool output. A dead-letter entry is a POINTER plus a reason: who,
what objective, what kind of failure, and where to look. Copying the run's content here would make this a
second, staler copy of the transcript -- and a failure store that grows to megabytes is one that gets deleted
to save space, taking the operational signal with it.

Kinds are a small fixed vocabulary rather than free text, because the whole value is grouping. "repeating",
"failed", "budget", "timeout" can be counted and compared across weeks; a paragraph of prose cannot.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .atomic_io import atomic_write_text
from .jsonl_log import append_jsonl, read_jsonl
from .locus_swarm import ensure_layout, safe_id, swarm_root

# A JSONL append rather than a rewritten JSON array: failures are the one thing most likely to happen while
# a process is dying, and an append cannot truncate what is already there. The torn-tail repair in
# `jsonl_log` is what makes reading it safe.
STORE_FILENAME = "dead-letter.jsonl"

# Kinds are a closed set, because grouping is the entire value. An open vocabulary produces a hundred
# spellings of "it broke" and no way to count any of them.
KIND_FAILED = "failed"          # an exception or a failed step
KIND_REPEATING = "repeating"    # stopped because the run was restating itself
KIND_BUDGET = "budget"          # ran out of steps, time or tokens
KIND_BLOCKED = "blocked"        # could not proceed and handed back
KIND_TIMEOUT = "timeout"        # a tool or command exceeded its bound

KINDS = (KIND_FAILED, KIND_REPEATING, KIND_BUDGET, KIND_BLOCKED, KIND_TIMEOUT)

# How many entries to keep. Bounded because this is a diagnostic store, not an audit trail -- the tamper-
# evident record is `audit.jsonl`, and duplicating it here would be a second thing to trust.
MAX_ENTRIES = 500


def _store_path() -> Path:
    ensure_layout()
    return swarm_root() / STORE_FILENAME


@dataclass
class DeadLetter:
    """One failure, as a pointer plus a reason."""

    id: str = ""
    at: float = 0.0
    locus: str = ""
    goal: str = ""
    kind: str = KIND_FAILED
    # The reason in one line. Truncated, because a full traceback belongs in the log and a store that
    # swallows megabytes is a store that gets deleted.
    reason: str = ""
    # Enough to go and look: a step number, a run id, a file. Never the content itself.
    pointer: str = ""

    @property
    def when(self) -> str:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(self.at))


def record(locus: str, goal: str, kind: str, reason: str, *, pointer: str = "") -> DeadLetter:
    """File a failure. Never raises -- a store that can fail a dying run is worse than no store."""
    kind = kind if kind in KINDS else KIND_FAILED
    entry = DeadLetter(
        id=f"dl-{int(time.time() * 1000):x}",
        at=time.time(),
        locus=safe_id(locus) if (locus or "").strip() else "unknown",
        goal=" ".join((goal or "").split())[:300],
        kind=kind,
        reason=" ".join((reason or "").split())[:600],
        pointer=(pointer or "").strip()[:300],
    )
    try:
        append_jsonl(_store_path(), asdict(entry))
        _trim()
    except OSError:
        # Losing a diagnostic record must never turn into failing the run that was already failing.
        pass
    return entry


def _trim() -> None:
    """Keep the newest `MAX_ENTRIES`, so the store cannot grow without bound."""
    entries = read_jsonl(_store_path())
    if len(entries) <= MAX_ENTRIES:
        return
    keep = entries[-MAX_ENTRIES:]
    atomic_write_text(_store_path(), "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in keep))


def entries(*, locus: str = "", kind: str = "", limit: int = 50,
            since: float = 0.0) -> list[DeadLetter]:
    """Read the store, newest first."""
    out: list[DeadLetter] = []
    wanted_locus = safe_id(locus) if (locus or "").strip() else ""
    known = {f for f in DeadLetter.__dataclass_fields__}
    for raw in reversed(read_jsonl(_store_path())):
        if not isinstance(raw, dict):
            continue
        item = DeadLetter(**{k: v for k, v in raw.items() if k in known})
        if wanted_locus and item.locus != wanted_locus:
            continue
        if kind and item.kind != kind:
            continue
        if since and item.at < since:
            continue
        out.append(item)
        if len(out) >= max(1, int(limit)):
            break
    return out


def summary() -> dict:
    """Counts by kind and by locus, which is what makes a repeated failure visible."""
    by_kind: dict[str, int] = {}
    by_locus: dict[str, int] = {}
    total = 0
    for raw in read_jsonl(_store_path()):
        if not isinstance(raw, dict):
            continue
        total += 1
        by_kind[str(raw.get("kind", KIND_FAILED))] = by_kind.get(str(raw.get("kind", KIND_FAILED)), 0) + 1
        name = str(raw.get("locus", "unknown"))
        by_locus[name] = by_locus.get(name, 0) + 1
    return {"total": total, "by_kind": by_kind, "by_locus": by_locus}


def repeat_offenders(*, threshold: int = 3, window_seconds: float = 24 * 3600,
                     now: float | None = None) -> list[dict]:
    """Locus/kind pairs that have failed `threshold` or more times inside the window.

    The reason this function exists rather than leaving the counting to a reader: a failure that happens once
    is noise and a failure that happens three times in a day is a defect, and that distinction requires
    arithmetic across records. Doing it here means the answer is consistent and testable.
    """
    now = time.time() if now is None else now
    cutoff = now - float(window_seconds)
    counts: dict[tuple[str, str], list[DeadLetter]] = {}
    for item in entries(limit=MAX_ENTRIES, since=cutoff):
        counts.setdefault((item.locus, item.kind), []).append(item)

    offenders = []
    for (locus, kind), items in counts.items():
        if len(items) < threshold:
            continue
        offenders.append({
            "locus": locus,
            "kind": kind,
            "count": len(items),
            "last": items[0].when,
            "reason": items[0].reason,
            "sample_id": items[0].id,
        })
    offenders.sort(key=lambda o: o["count"], reverse=True)
    return offenders


def format_report(*, limit: int = 20) -> str:
    """A readable report for a status panel."""
    data = summary()
    if not data["total"]:
        return "Dead letters: none recorded."

    lines = [f"Dead letters: {data['total']} recorded"]
    kinds = ", ".join(f"{k}={v}" for k, v in sorted(data["by_kind"].items()))
    lines.append(f"  by kind  : {kinds}")
    if data["by_locus"]:
        loci = ", ".join(f"{k}={v}" for k, v in sorted(data["by_locus"].items()))
        lines.append(f"  by locus : {loci}")

    offenders = repeat_offenders()
    if offenders:
        lines.append("")
        lines.append("Repeating failures (3+ in 24h) -- these are defects, not noise:")
        for item in offenders[:5]:
            lines.append(f"  {item['locus']} / {item['kind']}: {item['count']}x, last {item['last']}")
            lines.append(f"      {item['reason'][:150]}")

    recent = entries(limit=limit)
    if recent:
        lines.append("")
        lines.append(f"Most recent {len(recent)}:")
        for item in recent:
            lines.append(f"  [{item.when}] {item.locus} ({item.kind}): {item.reason[:120]}")
    return "\n".join(lines)


def clear(*, confirm: bool = False) -> int:
    """Delete the store. Requires `confirm=True`, because it is the only way to lose the signal."""
    if not confirm:
        return 0
    count = len(read_jsonl(_store_path()))
    try:
        atomic_write_text(_store_path(), "")
    except OSError:
        return 0
    return count
