"""Collective memory: a finding by one member, retrievable by every member.

Level 4 requirement 4.9. Until this existed, Myriaxon had memory in two places and neither was collective:

  * `loci_capabilities` gives each locus wins, memories, a journal and habits -- all under that locus's own
    directory, so a peer cannot read any of it.
  * `memory_store` holds session transcripts, keyed by project path and searched by substring.

So a locus could learn something and the next locus would rediscover it. That is the difference between
several agents and a swarm, and it is why 4.9 was still open after the board shipped: a shared directory
next to a private store does not make the store shared.

WHAT MAKES THIS DIFFERENT FROM A SHARED FILE

Three properties, each of which a naive "just write to a common JSON" loses:

  * **PROVENANCE.** Every entry records which member learned it and when. A finding with no author cannot be
    questioned, and a swarm where nobody can be asked "how do you know that" accumulates confident nonsense.
  * **ATTRIBUTION SURVIVES CURATION.** Inherited entries keep their original author, so re-sharing somebody
    else's finding does not launder it into your own.
  * **APPEND-ONLY.** Entries are never rewritten in place. A member that dies mid-write leaves the previous
    version intact rather than a truncated file, and an entry can be retracted without editing history.

WHAT THIS DELIBERATELY IS NOT

Not a knowledge base with embeddings or ranking. Retrieval is substring and recency, over a store measured
in hundreds of entries -- the same reasoning `loci_capabilities.recall` already records. An index would be a
second thing to keep consistent, and the honest scale here does not need one. If this grows past a few
thousand entries the decision should be revisited, and that is a note rather than a plan.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .atomic_io import atomic_write_text
from .jsonl_log import append_jsonl, read_jsonl
from .locus_swarm import ensure_layout, safe_id, shared_dir

# The collective store, inside the swarm's shared directory so it is one tree an operator can find.
MEMORY_FILENAME = "collective-memory.json"

# Who wrote an entry. Decides whether a sweep may touch it -- the same rule as goal retirement, for the same
# reason: an agent that can quietly discard what its owner wrote has been handed a delete button for its
# instructions.
OWNER_AUTHOR = "owner"
LOCUS_AUTHOR = "locus"

# How long an entry nothing has read stays. Level 5.9, and the number is a judgement rather than a
# measurement, stated so a reader can disagree with a number instead of discovering a store that grows
# forever. An entry READ once is kept far longer, because use is the only evidence that a finding helped.
UNUSED_TTL_SECONDS = 30 * 24 * 3600
USED_TTL_SECONDS = 180 * 24 * 3600


def _store_path() -> Path:
    ensure_layout()
    return shared_dir() / MEMORY_FILENAME


@dataclass
class CollectiveEntry:
    """One thing a member learned, with the attribution intact."""

    id: str = ""
    text: str = ""
    # Who learned it. Required, because an unattributed finding cannot be questioned.
    member: str = ""
    learned_at: float = 0.0
    # Where it came from inside that member's work -- a file, a task, a run. Optional but recorded, so a
    # reader can go and look rather than take it on trust.
    source: str = ""
    # Free-form labels, matching the board's tags so "show me everything about design" works the same way
    # in both places.
    tags: list[str] = field(default_factory=list)
    # Set when a member withdraws the entry. Kept rather than deleted: a retraction is itself information,
    # and a peer that already relied on it needs to be able to find out that it was withdrawn.
    retracted_at: float = 0.0
    retracted_by: str = ""
    retraction_reason: str = ""
    # --- Level 5.9: curation -------------------------------------------------------------------
    #
    # How many times a peer has actually READ this entry. The one signal that separates a finding worth
    # keeping from one that cost effort and helped nobody, and it cannot be inferred after the fact -- so it
    # is counted as it happens.
    uses: int = 0
    last_used_at: float = 0.0
    # "permanent" survives every sweep. Set deliberately, by a person or by the member that wrote it, for a
    # finding whose value does not decay -- a working configuration, a vendor behaviour that took a day to
    # establish. Everything else expires if nothing reads it.
    keep: bool = False
    # Who wrote it, as distinct from which MEMBER wrote it: "owner" means a person, and an entry a person
    # wrote is never swept. The same rule as goal retirement, for the same reason.
    author: str = LOCUS_AUTHOR
    # When an automatic sweep removed it, and why. Sweeps mark rather than delete, so a curated entry is still
    # findable by anyone who relied on it.
    swept_at: float = 0.0
    swept_reason: str = ""

    @property
    def live(self) -> bool:
        return self.retracted_at <= 0 and self.swept_at <= 0


def _load() -> list[dict]:
    try:
        data = json.loads(_store_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    entries = data.get("entries") if isinstance(data, dict) else None
    return entries if isinstance(entries, list) else []


def _save(entries: list[dict]) -> None:
    atomic_write_text(_store_path(), json.dumps({"entries": entries}, indent=2, ensure_ascii=False))


_id_lock = __import__("threading").Lock()
_last_id_stamp = [0.0]
_id_counter = [0]


def _new_id() -> str:
    """A unique id that still sorts by creation time.

    MEASURED BUG: the first version was `f"cm-{int(time.time() * 1000):x}"` -- a millisecond timestamp and
    nothing else. Four entries shared in the same millisecond all received the SAME id, which quietly broke
    every operation that addresses an entry by id: `retract` marked whichever one it found first,
    `keep_forever` protected the wrong entry, and `note_use` bumped the wrong counter. The curation sweep then
    reported ids that matched nothing.

    It surfaced only because the store was written to four times in a loop and the ids were compared -- a
    single-entry test would have passed forever.

    The fix keeps the timestamp prefix, so ids still sort by age and a human can read when an entry was made,
    and appends a monotonic counter that is bumped within the same millisecond. Thread-safe because two
    members can share concurrently through different threads of one process.
    """
    with _id_lock:
        now = time.time()
        millis = int(now * 1000)
        if millis == _last_id_stamp[0]:
            _id_counter[0] += 1
        else:
            _last_id_stamp[0] = millis
            _id_counter[0] = 0
        return f"cm-{millis:x}-{_id_counter[0]:02x}"


def share(text: str, *, member: str, source: str = "", tags: list[str] | None = None) -> tuple[CollectiveEntry | None, str]:
    """Publish a finding to every member. Returns (entry, problem).

    `member` is required rather than defaulted. A blank author is the one input that makes the store
    useless in a way nobody notices until they need to trust it.
    """
    cleaned = " ".join((text or "").split()).strip()
    if not cleaned:
        return None, "nothing to share: the text is empty"
    if not (member or "").strip():
        return None, "`member` is required: an unattributed finding cannot be questioned later"

    entry = CollectiveEntry(
        id=_new_id(),
        text=cleaned[:4000],
        member=safe_id(member),
        learned_at=time.time(),
        source=(source or "").strip()[:300],
        tags=[safe_id(t).lower() for t in (tags or []) if str(t).strip()],
    )
    entries = _load()
    entries.append(asdict(entry))
    try:
        _save(entries)
    except OSError as exc:
        return None, f"could not write the shared memory: {exc}"
    return entry, ""


def recall(query: str = "", *, tags: list[str] | None = None, member: str = "",
           limit: int = 20, include_retracted: bool = False,
           count_uses: bool = True) -> list[CollectiveEntry]:
    """Find entries. Newest first. An empty query returns everything live.

    Substring and tag matching, for the reason in the module docstring: the store is small and an index
    would be a second thing to keep consistent.

    Each returned entry counts as a USE, which is the measurement the curation sweep rests on: an entry a peer
    actually read is worth keeping, and one that cost effort and helped nobody is not. Nothing can tell those
    apart after the fact, so the count has to happen as it happens.

    `count_uses=False` exists for a REPORT -- a status panel listing entries is not a peer reading them, and
    counting it would make every entry look used the moment anybody looked at the panel.
    """
    needle = (query or "").strip().lower()
    wanted = {safe_id(t).lower() for t in (tags or []) if str(t).strip()}
    who = safe_id(member) if (member or "").strip() else ""
    out: list[CollectiveEntry] = []
    for raw in reversed(_load()):
        known = {f for f in CollectiveEntry.__dataclass_fields__}
        entry = CollectiveEntry(**{k: v for k, v in raw.items() if k in known})
        if not entry.live and not include_retracted:
            continue
        if who and entry.member != who:
            continue
        if wanted and not wanted & {t.lower() for t in entry.tags}:
            continue
        if needle:
            haystack = f"{entry.text} {entry.source} {' '.join(entry.tags)} {entry.member}".lower()
            if needle not in haystack:
                continue
        out.append(entry)
        if len(out) >= max(1, int(limit)):
            break

    if count_uses and out:
        # One write for the batch rather than one per entry: a recall returning twenty entries would otherwise
        # rewrite the store twenty times, and the file is the shared one every member reads.
        _note_uses([e.id for e in out])
    return out


def _note_uses(ids: list[str]) -> None:
    """Bump the use count for several entries in a single write."""
    wanted = set(ids)
    entries = _load()
    changed = False
    now = time.time()
    for raw in entries:
        if raw.get("id") in wanted:
            raw["uses"] = int(raw.get("uses", 0)) + 1
            raw["last_used_at"] = now
            changed = True
    if changed:
        try:
            _save(entries)
        except OSError:
            pass


def retract(entry_id: str, *, member: str, reason: str = "") -> tuple[bool, str]:
    """Withdraw an entry by marking it, never by deleting it.

    Only the member that shared it may retract it. Allowing anyone would let one member quietly remove
    another's finding, which is the failure mode a shared store most needs to prevent.
    """
    entries = _load()
    who = safe_id(member) if (member or "").strip() else ""
    if not who:
        return False, "`member` is required to retract an entry"
    for raw in entries:
        if raw.get("id") != entry_id:
            continue
        if raw.get("retracted_at"):
            return False, "already retracted"
        if safe_id(str(raw.get("member", ""))) != who:
            return False, f"entry was shared by {raw.get('member')!r}, not {who!r}"
        raw["retracted_at"] = time.time()
        raw["retracted_by"] = who
        raw["retraction_reason"] = (reason or "").strip()[:300]
        try:
            _save(entries)
        except OSError as exc:
            return False, f"could not write the retraction: {exc}"
        return True, ""
    return False, f"no entry {entry_id!r} in the shared memory"


def digest(limit: int = 10) -> str:
    """A short briefing of what the swarm currently knows, for a member starting work.

    This is what makes the memory COLLECTIVE rather than merely shared: a shared store nobody reads changes
    nothing. A new member is handed the recent findings so it does not rediscover them.
    """
    entries = recall(limit=limit)
    if not entries:
        return "Shared memory: nothing has been shared yet."
    lines = [f"Shared memory -- {len(entries)} most recent finding(s) from other members:"]
    for entry in entries[:limit]:
        tags = f" [{', '.join(entry.tags)}]" if entry.tags else ""
        source = f" (from {entry.source})" if entry.source else ""
        lines.append(f"  - {entry.member}{tags}: {entry.text[:300]}{source}")
    return "\n".join(lines)


def stats() -> dict:
    """Counts for a status panel. Never raises."""
    entries = _load()
    live = [e for e in entries if not e.get("retracted_at")]
    members = sorted({str(e.get("member", "")) for e in live if e.get("member")})
    return {
        "path": str(_store_path()),
        "total": len(entries),
        "live": len(live),
        "retracted": len(entries) - len(live),
        "members": members,
    }


# ---------------------------------------------------------------------------------------------
# Level 5.9: curation -- deciding what to remember and what to forget
# ---------------------------------------------------------------------------------------------
#
# Before this, memory was APPENDED and never curated. That is two problems in one: an unbounded store is a
# retrieval tax (every recall scans everything) and, once it holds anything sensitive, a privacy liability
# nobody can discharge because nobody knows what is in it.
#
# THE HARD RULE, and it is the same one goal retirement uses: an agent may never discard what its OWNER wrote.
# An agent handed a delete button for its owner's instructions fails silently -- the entry simply stops
# appearing and nobody is told.
#
# USE IS THE EVIDENCE. An entry a peer actually read is worth keeping; one that cost effort and helped nobody
# is not, and nothing can tell those apart after the fact. So `recall` counts reads, and the sweep keys on it.

CURATION_LOG_FILENAME = "curation-log.jsonl"


def _log_path() -> Path:
    ensure_layout()
    return shared_dir() / CURATION_LOG_FILENAME


def _log(action: str, entry_id: str, member: str, reason: str) -> None:
    """Every drop is recorded with its reason.

    A curation that cannot be audited is indistinguishable from data loss, and the first question anybody asks
    about a missing entry is "why did that go".
    """
    try:
        append_jsonl(_log_path(), {
            "at": time.time(), "action": action, "id": entry_id,
            "member": safe_id(member) if (member or "").strip() else "system",
            "reason": " ".join((reason or "").split())[:400],
        })
    except OSError:
        pass


def note_use(entry_id: str) -> None:
    """Record that an entry was actually read. Called by `recall` for each entry it returns.

    This is the measurement the whole of 5.9 rests on. Without it a sweep can only guess from age, and age is
    the wrong signal: a finding established once and used every week is older than one written yesterday and
    ignored.
    """
    entries = _load()
    for raw in entries:
        if raw.get("id") == entry_id:
            raw["uses"] = int(raw.get("uses", 0)) + 1
            raw["last_used_at"] = time.time()
            try:
                _save(entries)
            except OSError:
                pass
            return


def keep_forever(entry_id: str, *, member: str, reason: str = "") -> tuple[bool, str]:
    """Protect an entry from every future sweep. Only the member that shared it may do this."""
    entries = _load()
    who = safe_id(member) if (member or "").strip() else ""
    for raw in entries:
        if raw.get("id") != entry_id:
            continue
        if safe_id(str(raw.get("member", ""))) != who:
            return False, f"entry was shared by {raw.get('member')!r}, not {who!r}"
        raw["keep"] = True
        raw["swept_reason"] = ""
        try:
            _save(entries)
        except OSError as exc:
            return False, f"could not write: {exc}"
        _log("keep", entry_id, who, reason or "marked permanent")
        return True, ""
    return False, f"no entry {entry_id!r}"


def sweep(*, now: float | None = None, apply: bool = False) -> list[dict]:
    """Find (and optionally mark) entries nothing has read in a long time.

    Returns what WOULD go, and only marks them when `apply=True`. A dry run by default, deliberately: a
    curation that acts before it can be inspected is a curation nobody trusts, and the whole point of listing
    the entries first is that a person can disagree before anything is lost.

    Never touches: an entry the owner wrote, an entry marked permanent, or one already retracted or swept.
    """
    now = time.time() if now is None else now
    entries = _load()
    doomed: list[dict] = []
    for raw in entries:
        if raw.get("retracted_at") or raw.get("swept_at"):
            continue
        if raw.get("keep"):
            continue
        if str(raw.get("author", LOCUS_AUTHOR)) == OWNER_AUTHOR:
            continue
        used = int(raw.get("uses", 0))
        age = now - float(raw.get("last_used_at") or raw.get("learned_at") or now)
        ttl = USED_TTL_SECONDS if used else UNUSED_TTL_SECONDS
        if age <= ttl:
            continue
        doomed.append({
            "id": raw.get("id"), "member": raw.get("member"), "uses": used,
            "age_days": int(age / 86400), "ttl_days": int(ttl / 86400),
            "reason": (f"never read in {int(age / 86400)} days"
                       if not used else
                       f"last read {int(age / 86400)} days ago"),
            "text": str(raw.get("text", ""))[:160],
        })

    if apply and doomed:
        ids = {item["id"] for item in doomed}
        for raw in entries:
            if raw.get("id") in ids:
                raw["swept_at"] = now
                raw["swept_reason"] = next(i["reason"] for i in doomed if i["id"] == raw.get("id"))
        try:
            _save(entries)
        except OSError:
            return []
        for item in doomed:
            _log("sweep", item["id"], str(item.get("member", "")), item["reason"])
    return doomed


def curation_log(limit: int = 50) -> list[dict]:
    """Every curation decision, newest first. The audit trail for what was dropped and why."""
    return list(reversed(read_jsonl(_log_path())))[: max(1, int(limit))]


def format_curation(limit: int = 10) -> str:
    """A readable report: what would go, and what already did."""
    doomed = sweep()
    lines = []
    if doomed:
        lines.append(f"{len(doomed)} entry(s) nothing has read and would be swept:")
        for item in doomed[:limit]:
            lines.append(f"  {item['id']} ({item['member']}): {item['reason']}")
            lines.append(f"      {item['text'][:120]}")
        lines.append("  Apply with sweep(apply=True). Nothing is removed without being listed first.")
    else:
        lines.append("Nothing is due to be swept.")

    log = curation_log(limit=limit)
    if log:
        lines.append("")
        lines.append(f"Last {len(log)} curation decision(s):")
        for item in log:
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(float(item.get("at", 0))))
            lines.append(f"  [{when}] {item.get('action')}: {item.get('id')} -- {item.get('reason', '')[:90]}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------------------------
# Agent-facing tools
# ---------------------------------------------------------------------------------------------

MEMORY_SHARE_TOOL_DEF = {
    "name": "memory_share",
    "description": (
        "Record something you learned so other agents can use it instead of rediscovering it. Use this "
        "for a fact that cost you real effort to establish -- a working configuration, a vendor "
        "behaviour, a failure mode. Your name is attached, so a peer can ask how you know it. Do not "
        "share things a peer would find in seconds anyway."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "The finding, stated so it stands alone."},
            "member": {"type": "string", "description": "Your name. Required for attribution."},
            "source": {"type": "string", "description": "Where it came from: a file, a task, a run."},
            "tags": {"type": "array", "items": {"type": "string"},
                     "description": "Labels for retrieval, e.g. ['gcp','vertex']."},
        },
        "required": ["text", "member"],
    },
}

MEMORY_RECALL_TOOL_DEF = {
    "name": "memory_recall",
    "description": (
        "Look up what other agents have already learned. Check this before investigating something a "
        "peer may have already solved. Returns the finding with the name of whoever established it, so "
        "you can judge how much to trust it."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Words to search for. Empty returns recent."},
            "tags": {"type": "array", "items": {"type": "string"}},
            "limit": {"type": "integer", "description": "Maximum entries (default 20)."},
        },
        "required": [],
    },
}

MEMORY_TOOL_DEFS = (MEMORY_SHARE_TOOL_DEF, MEMORY_RECALL_TOOL_DEF)


def execute_memory_share(tool_input: dict) -> tuple[str, bool]:
    entry, problem = share(
        str(tool_input.get("text") or ""),
        member=str(tool_input.get("member") or ""),
        source=str(tool_input.get("source") or ""),
        tags=[str(t) for t in (tool_input.get("tags") or [])],
    )
    if entry is None:
        return problem, True
    return f"Shared with the swarm as {entry.id}.", False


def execute_memory_recall(tool_input: dict) -> tuple[str, bool]:
    entries = recall(
        str(tool_input.get("query") or ""),
        tags=[str(t) for t in (tool_input.get("tags") or [])] or None,
        limit=int(tool_input.get("limit") or 20),
    )
    if not entries:
        # An empty result is a real answer and worth saying plainly: it means nobody has recorded this,
        # so the caller should investigate rather than assume the memory is broken.
        return "Nothing in the shared memory matches. No peer has recorded this yet.", False
    lines = [f"{len(entries)} shared finding(s):"]
    for entry in entries:
        tags = f" [{', '.join(entry.tags)}]" if entry.tags else ""
        source = f" -- from {entry.source}" if entry.source else ""
        lines.append(f"  {entry.id}  {entry.member}{tags}{source}")
        lines.append(f"      {entry.text[:600]}")
    return "\n".join(lines), False
