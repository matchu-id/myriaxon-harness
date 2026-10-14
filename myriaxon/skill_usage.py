"""Skill usage telemetry: which skills are actually invoked, and how often.

## The gap this closes

`myriaxon verify` proves a skill is *well-formed and loadable*. It cannot say whether the skill has ever been
used. That distinction matters because a skill is not free: every one of them is listed in the system prompt on
every turn, and a skill with `use_count = 0` that has been installed for months is consuming context forever
while returning nothing. Without this there is no way to tell a useful library from an accumulating pile.

OpenClaw keeps a `skill_usage` table (verified in `src/state/openclaw-state-schema.sql`):

    skill_file, skill_key, skill_name, skill_source,
    first_used_at_ms, last_used_at_ms, use_count, last_agent_id

The same eight fields are recorded here. Theirs is a SQLite row updated in a worker thread; this is a JSON file
written under a lock, because Myriaxon is one process and the write happens once per `use_skill` call rather than
once per model turn.

## Why `last_agent_id` is worth keeping

Their column records *which* agent used a skill, not just that one did. For Myriaxon that is the difference
between "this skill is used" and "this skill is only used by the reviewer Locus" -- which is what tells you whether
a skill belongs in the shared listing or should be scoped. It costs one string.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from .atomic_io import atomic_write_text
from .constants import CONFIG_DIR

USAGE_FILE = CONFIG_DIR / "skill-usage.json"
_LOCK = threading.Lock()

# A cap on distinct skills tracked, matching OpenClaw's `MAX_TRACKED_SKILL_USAGE_RUNS` intent: unbounded growth
# in a file read on every status call is a slow leak, so the least-recently-used entries are dropped rather than
# accumulated forever.
MAX_TRACKED_SKILLS = 1024


@dataclass
class UsageRecord:
    """One skill's usage. Field names mirror their `skill_usage` columns."""

    skill_key: str
    skill_name: str
    skill_source: str = ""
    first_used_at: float = 0.0
    last_used_at: float = 0.0
    use_count: int = 0
    last_agent_id: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "UsageRecord":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})

    @property
    def never_used(self) -> bool:
        return self.use_count <= 0

    def render(self) -> str:
        if self.never_used:
            return f"  {self.skill_name:<32} never used"
        age = time.time() - self.last_used_at
        return (
            f"  {self.skill_name:<32} {self.use_count:>4} use(s), "
            f"last {_humanise_age(age)} ago"
            + (f", by {self.last_agent_id}" if self.last_agent_id else "")
        )


def _humanise_age(seconds: float) -> str:
    if seconds < 90:
        return f"{int(seconds)}s"
    if seconds < 5400:
        return f"{int(seconds // 60)}m"
    if seconds < 172800:
        return f"{int(seconds // 3600)}h"
    return f"{int(seconds // 86400)}d"


def _load() -> dict[str, UsageRecord]:
    try:
        raw = json.loads(USAGE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, UsageRecord] = {}
    for key, value in raw.items():
        if isinstance(value, dict):
            try:
                out[key] = UsageRecord.from_dict(value)
            except TypeError:
                continue
    return out


def _save(records: dict[str, UsageRecord]) -> None:
    try:
        USAGE_FILE.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(
            USAGE_FILE,
            json.dumps({k: v.to_dict() for k, v in records.items()}, indent=2, ensure_ascii=False),
        )
    except OSError:
        # Telemetry must never break a turn. A missing usage count is a missing statistic, not a failed skill.
        pass


def record_use(
    name: str,
    *,
    source: str = "",
    agent_id: str = "",
    now: float | None = None,
) -> UsageRecord:
    """Record one invocation of a skill. Returns the updated record.

    Never raises: this is called from the `use_skill` path, and a telemetry failure must not stop a skill from
    loading. Callers that need to know whether the write succeeded should read back with `get`.
    """
    stamp = time.time() if now is None else now
    key = name.strip().lower()
    with _LOCK:
        records = _load()
        record = records.get(key)
        if record is None:
            record = UsageRecord(
                skill_key=key, skill_name=name, skill_source=source,
                first_used_at=stamp, last_used_at=stamp, use_count=0,
            )
        record.skill_name = name
        if source:
            record.skill_source = source
        if not record.first_used_at:
            record.first_used_at = stamp
        record.last_used_at = stamp
        record.use_count += 1
        if agent_id:
            record.last_agent_id = agent_id
        records[key] = record

        if len(records) > MAX_TRACKED_SKILLS:
            # Drop the least recently used, never the one just recorded.
            ordered = sorted(records.items(), key=lambda item: item[1].last_used_at)
            for stale_key, _ in ordered[: len(records) - MAX_TRACKED_SKILLS]:
                if stale_key != key:
                    records.pop(stale_key, None)

        _save(records)
        return record


def get(name: str) -> UsageRecord | None:
    return _load().get(name.strip().lower())


def all_records() -> dict[str, UsageRecord]:
    return _load()


def report(skills: dict | None = None) -> list[dict]:
    """Combine discovered skills with their usage, so *never used* is visible rather than absent.

    This is the useful view: a listing built only from the usage file cannot show the skills that have never
    run, and those are exactly the ones worth questioning.
    """
    records = _load()
    rows: list[dict] = []
    seen: set[str] = set()

    for name, skill in (skills or {}).items():
        key = name.strip().lower()
        seen.add(key)
        record = records.get(key)
        rows.append({
            "name": name,
            "source": str(getattr(skill, "source", "")),
            "use_count": record.use_count if record else 0,
            "last_used_at": record.last_used_at if record else 0.0,
            "first_used_at": record.first_used_at if record else 0.0,
            "last_agent_id": record.last_agent_id if record else "",
            "installed": True,
        })

    # Skills with usage but no longer discovered: removed, or the file moved.
    for key, record in records.items():
        if key not in seen:
            rows.append({
                "name": record.skill_name,
                "source": record.skill_source,
                "use_count": record.use_count,
                "last_used_at": record.last_used_at,
                "first_used_at": record.first_used_at,
                "last_agent_id": record.last_agent_id,
                "installed": False,
            })

    rows.sort(key=lambda row: (-row["use_count"], row["name"]))
    return rows


def summary(skills: dict | None = None) -> dict[str, int]:
    rows = report(skills)
    installed = [r for r in rows if r["installed"]]
    return {
        "discovered": len(installed),
        "used_at_least_once": sum(1 for r in installed if r["use_count"] > 0),
        "never_used": sum(1 for r in installed if r["use_count"] == 0),
        "orphaned": sum(1 for r in rows if not r["installed"]),
        "total_invocations": sum(r["use_count"] for r in rows),
    }


def never_used(skills: dict | None = None) -> list[str]:
    """Discovered skills that have never been invoked -- the ones whose prompt cost is not being earned."""
    return [r["name"] for r in report(skills) if r["installed"] and r["use_count"] == 0]


def reset(name: str = "") -> int:
    """Clear usage for one skill, or all of them. Returns how many records were removed."""
    with _LOCK:
        records = _load()
        if not name:
            count = len(records)
            records = {}
        else:
            key = name.strip().lower()
            count = 1 if records.pop(key, None) is not None else 0
        _save(records)
        return count
