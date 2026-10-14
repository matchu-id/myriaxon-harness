"""Staged skill proposals: a draft an agent writes is reviewed before it becomes live.

## The gap this closes

`skills.execute_save_skill` wrote straight to `.myriaxon/skills/<name>/SKILL.md`. The trust gate in `skill_trust`
validates structure and safety, so a malformed or unsafe skill was refused — but a *well-formed* generated skill
became live immediately, with no step at which anyone could look at it first, and no way back if it turned out to
be a bad idea. The only record of what changed was the file's new contents; the previous version was gone.

OpenClaw's `src/skills/workshop/` solves this with a proposal lifecycle, verified in
`store-record.ts:130-166` and `skill-workshop-tool-schema.ts:24-30`:

    status: pending -> applied | rejected | quarantined | stale

* an agent draft is stored as a **pending proposal**, not written to the live root;
* a separate explicit **apply** performs the write;
* the previous content and its hash are captured as a **rollback record** before that write;
* `stale` is reached by explicit transition when the target changed under the proposal;
* every mutation is fenced by an `expected_revision_hash` compare-and-swap.

## What is copied exactly, and what is deliberately thinner

Copied: the status set, the rollback record, the revision-hash CAS, `stale` as an explicit transition, and the
bounding constants (`MAX_PROPOSAL_BYTES` 1 MiB, `MAX_PROPOSAL_SUPPORT_FILES` 64, `MAX_PROPOSAL_DRAFT_BYTES`
1 MiB, description 160 bytes, name 64) — those numbers are sane and were chosen deliberately rather than invented
here.

Thinner: their state lives in a four-table event-sourced SQLite schema with a worker-thread access split and a
collection-review/backup/restore subsystem. This is one JSON file plus one directory per proposal, because
Myriaxon is a single process on one machine and their own capability ladder says to take the first *fitting* step.
What is kept is the part that makes the lifecycle meaningful — append-only events, a monotonic sequence, and a
compare-and-swap on every transition — and what is dropped is the machinery that exists to serve a fleet.

## Why `stale` matters more than it looks

It is a time-of-check/time-of-use guard. Without it, a proposal drafted against version A would be applied on top
of version B, silently discarding whatever changed in between. The revision hash makes that a refused transition
with a name rather than a lost write.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .constants import CONFIG_DIR, PROJECT_SKILLS_SUBDIR

# --- bounds, borrowed from OpenClaw's workshop config rather than invented. See the module docstring.
MAX_PROPOSAL_BYTES = 1 * 1024 * 1024
MAX_PROPOSAL_DRAFT_BYTES = 1 * 1024 * 1024
MAX_PROPOSAL_SUPPORT_FILES = 64
MAX_SUPPORT_FILE_BYTES = 256 * 1024
MAX_SUPPORT_FILES_TOTAL_BYTES = 2 * 1024 * 1024
MAX_DESCRIPTION_BYTES = 160
MAX_NAME_LENGTH = 64
MAX_PENDING = 200
MIN_PENDING = 1

PROPOSAL_ID_RE = re.compile(r"^p-[0-9a-f]{12}$")
SUPPORT_PATH_RE = re.compile(r"^[A-Za-z0-9._/-]{1,200}$")

# The lifecycle. `stale` is terminal-by-transition, never a recomputed flag -- a proposal that was invalidated
# stays invalidated, so the record and the state cannot disagree.
PENDING = "pending"
APPLIED = "applied"
REJECTED = "rejected"
QUARANTINED = "quarantined"
STALE = "stale"
STATUSES = (PENDING, APPLIED, REJECTED, QUARANTINED, STALE)

# Statuses that still occupy the pending budget. Applied and rejected are done with.
LIVE_STATUSES = (PENDING, QUARANTINED)


def workshop_dir(project_root: Path | None = None) -> Path:
    # `Path(...)` around the result, not just the default: callers pass either a `Path` or a plain string, and
    # `/` on a `str` raises `TypeError: unsupported operand type(s) for /: 'str' and 'str'`. Found by a test
    # that passed a string.
    root = Path(project_root) if project_root is not None else Path.cwd()
    return root / ".myriaxon" / "skill-workshop"


def _proposals_file(project_root: Path | None = None) -> Path:
    return workshop_dir(project_root) / "proposals.json"


def _events_file(project_root: Path | None = None) -> Path:
    return workshop_dir(project_root) / "events.jsonl"


def _rollbacks_file(project_root: Path | None = None) -> Path:
    return workshop_dir(project_root) / "rollbacks.json"


def _drafts_dir(project_root: Path | None = None) -> Path:
    return workshop_dir(project_root) / "drafts"


def _skills_root(project_root: Path | None = None) -> Path:
    root = Path(project_root) if project_root is not None else Path.cwd()
    return root / PROJECT_SKILLS_SUBDIR


# --------------------------------------------------------------------------- hashing


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def content_hash(content: str) -> str:
    """`sha256(content)`, the same shape as their `draftHash`."""
    return sha256_hex(content)


def revision_hash(content: str, support_files: list[dict] | None = None, version: str = "") -> str:
    """A composite hash over the content, the support files, and the version.

    Their `revision-hash.ts` hashes canonical JSON of `{proposedVersion, contentSha256, supportFiles}` with the
    support files sorted by path. Sorting is the important detail: the same set of files listed in a different
    order must produce the same hash, or the compare-and-swap would fail for reasons the caller cannot see.
    """
    canonical = {
        "proposedVersion": version,
        "contentSha256": content_hash(content),
        "supportFiles": sorted(
            (
                {"path": f.get("path", ""), "sha256": f.get("hash", ""), "sizeBytes": f.get("size_bytes", 0)}
                for f in (support_files or [])
            ),
            key=lambda entry: entry["path"],
        ),
    }
    return sha256_hex(json.dumps(canonical, sort_keys=True, separators=(",", ":")))


# --------------------------------------------------------------------------- the record


@dataclass
class Proposal:
    """One staged skill draft."""

    id: str
    kind: str                      # "create" | "update"
    status: str
    title: str
    description: str
    target_name: str               # the skill name this would become
    target_dir: str                # where it would be written
    draft_hash: str
    revision_hash: str
    created_at: float
    updated_at: float
    origin: str = ""               # who proposed it
    status_reason: str = ""
    support_files: list[dict] = field(default_factory=list)
    # For an update: the hash of the live content this draft was written against. The compare-and-swap that
    # makes `stale` meaningful -- applying on top of different content would silently discard the difference.
    base_hash: str = ""
    # Set when the target changed underneath the proposal.
    stale_at: float = 0.0
    applied_at: float = 0.0
    rejected_at: float = 0.0
    quarantined_at: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Proposal":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})

    @property
    def is_live(self) -> bool:
        return self.status in LIVE_STATUSES

    def render(self) -> str:
        lines = [
            f"  {self.id}  [{self.status}] {self.kind} {self.target_name!r}",
            f"      {self.title or '(untitled)'}",
        ]
        if self.description:
            lines.append(f"      {self.description}")
        lines.append(f"      revision {self.revision_hash[:12]}  origin {self.origin or 'unknown'}")
        if self.support_files:
            lines.append(f"      {len(self.support_files)} support file(s)")
        if self.status_reason:
            lines.append(f"      why: {self.status_reason}")
        return "\n".join(lines)


# --------------------------------------------------------------------------- storage


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _write_json(path: Path, data: Any) -> None:
    from .atomic_io import atomic_write_text

    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(data, indent=2, ensure_ascii=False))


def load_proposals(project_root: Path | None = None) -> dict[str, Proposal]:
    raw = _read_json(_proposals_file(project_root), {})
    if not isinstance(raw, dict):
        return {}
    out: dict[str, Proposal] = {}
    for key, value in raw.items():
        if isinstance(value, dict):
            try:
                out[key] = Proposal.from_dict(value)
            except TypeError:
                continue
    return out


def save_proposals(proposals: dict[str, Proposal], project_root: Path | None = None) -> None:
    _write_json(_proposals_file(project_root), {k: p.to_dict() for k, p in proposals.items()})


def _append_event(project_root: Path | None, proposal_id: str, event: str, detail: str = "") -> None:
    """Append one event with a monotonic sequence, the way their `skill_workshop_proposal_events` does.

    Append-only JSONL rather than a row in a table: the sequence is derived from the last line, which is enough
    for a single process and keeps the history readable without a query layer.
    """
    path = _events_file(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    sequence = 0
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                sequence += 1
    except OSError:
        pass
    record = {
        "proposal_id": proposal_id,
        "sequence": sequence + 1,
        "event_type": event,
        "detail": detail,
        "at": time.time(),
    }
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def read_events(project_root: Path | None = None, proposal_id: str = "") -> list[dict]:
    events: list[dict] = []
    try:
        text = _events_file(project_root).read_text(encoding="utf-8")
    except OSError:
        return events
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if proposal_id and record.get("proposal_id") != proposal_id:
            continue
        events.append(record)
    return events


def draft_path(proposal_id: str, project_root: Path | None = None) -> Path:
    return _drafts_dir(project_root) / f"{proposal_id}.md"


def draft_text(proposal: Proposal, project_root: Path | None = None) -> str:
    """The proposal's body, or "" when the draft file has gone missing.

    A missing draft is *reported*, not raised: their `listSkillProposals` annotates the row
    `degradedState: "draft-missing"` rather than failing the whole listing, because one broken proposal must not
    hide the others.
    """
    try:
        return draft_path(proposal.id, project_root).read_text(encoding="utf-8")
    except OSError:
        return ""


def draft_missing(proposal: Proposal, project_root: Path | None = None) -> bool:
    return proposal.is_live and not draft_path(proposal.id, project_root).is_file()


# --------------------------------------------------------------------------- the API


@dataclass
class ProposalResult:
    ok: bool
    proposal: Proposal | None = None
    errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def render(self) -> str:
        lines = []
        if self.proposal is not None:
            lines.append(self.proposal.render())
        for note in self.notes:
            lines.append(f"      {note}")
        for error in self.errors:
            lines.append(f"      error: {error}")
        return "\n".join(lines)


def _pending_count(proposals: dict[str, Proposal]) -> int:
    return sum(1 for p in proposals.values() if p.is_live)


def propose(
    name: str,
    description: str,
    body: str,
    *,
    origin: str = "agent:save_skill",
    support_files: dict[str, str] | None = None,
    project_root: Path | None = None,
) -> ProposalResult:
    """Stage a skill draft as a pending proposal. Does not touch the live skills root.

    Nothing is written to `.myriaxon/skills` here — that only happens on `apply`. This is the whole point: an
    agent can propose freely, and a human decides what becomes real.
    """
    result = ProposalResult(ok=False)

    if not name or not re.match(r"^[a-zA-Z0-9_-]+$", name):
        result.errors.append("name may only contain letters, digits, hyphens and underscores")
        return result
    if len(name) > MAX_NAME_LENGTH:
        result.errors.append(f"name is longer than {MAX_NAME_LENGTH} characters")
        return result
    if len(description.encode("utf-8")) > MAX_DESCRIPTION_BYTES:
        result.errors.append(
            f"description is longer than {MAX_DESCRIPTION_BYTES} bytes; it is one line in every future prompt"
        )
        return result
    if not body.strip():
        result.errors.append("body is empty")
        return result
    if len(body.encode("utf-8")) > MAX_PROPOSAL_DRAFT_BYTES:
        result.errors.append(f"body exceeds the {MAX_PROPOSAL_DRAFT_BYTES} byte draft limit")
        return result

    checked_support: list[dict] = []
    total_support = 0
    for path, content in (support_files or {}).items():
        if not SUPPORT_PATH_RE.match(path) or ".." in path.split("/"):
            result.errors.append(f"support file path {path!r} is not a safe relative path")
            return result
        size = len(content.encode("utf-8"))
        if size > MAX_SUPPORT_FILE_BYTES:
            result.errors.append(f"support file {path!r} exceeds {MAX_SUPPORT_FILE_BYTES} bytes")
            return result
        total_support += size
        checked_support.append({"path": path, "hash": content_hash(content), "size_bytes": size})
    if len(checked_support) > MAX_PROPOSAL_SUPPORT_FILES:
        result.errors.append(f"more than {MAX_PROPOSAL_SUPPORT_FILES} support files")
        return result
    if total_support > MAX_SUPPORT_FILES_TOTAL_BYTES:
        result.errors.append(f"support files exceed {MAX_SUPPORT_FILES_TOTAL_BYTES} bytes in total")
        return result

    proposals = load_proposals(project_root)
    if _pending_count(proposals) >= MAX_PENDING:
        result.errors.append(
            f"{MAX_PENDING} proposals are already pending or quarantined; apply, reject or quarantine some "
            "before adding more"
        )
        return result

    target_dir = _skills_root(project_root) / name
    skill_md = target_dir / "SKILL.md"
    kind = "update" if skill_md.is_file() else "create"
    # The content this proposal is drafted against, for an update. Recorded here because it is the only moment it
    # can be observed: by the time `apply` runs, the live file may already have changed, and without a recorded
    # base there is nothing to compare against. The first version of this module never stored it, so
    # `_expected_previous` always returned "" and the staleness guard silently could not fire -- a guard that
    # looks present in the code and never runs is worse than no guard.
    base_hash = ""
    if kind == "update":
        try:
            base_hash = content_hash(skill_md.read_text(encoding="utf-8"))
        except OSError:
            base_hash = ""

    now = time.time()
    proposal_id = f"p-{uuid.uuid4().hex[:12]}"
    draft = f"---\nname: {name}\ndescription: {description}\n---\n{body.strip()}\n"

    proposal = Proposal(
        id=proposal_id,
        kind=kind,
        status=PENDING,
        title=f"{kind} {name}",
        description=description,
        target_name=name,
        target_dir=str(target_dir),
        draft_hash=content_hash(draft),
        revision_hash=revision_hash(draft, checked_support),
        created_at=now,
        updated_at=now,
        origin=origin,
        support_files=checked_support,
        base_hash=base_hash,
    )

    path = draft_path(proposal_id, project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    from .atomic_io import atomic_write_text

    atomic_write_text(path, draft)
    for entry in checked_support:
        side = _drafts_dir(project_root) / proposal_id / entry["path"]
        side.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(side, (support_files or {})[entry["path"]])

    proposals[proposal_id] = proposal
    save_proposals(proposals, project_root)
    _append_event(project_root, proposal_id, "created", f"{kind} {name}")

    result.ok = True
    result.proposal = proposal
    result.notes.append("pending: nothing was written to the live skills root")
    return result


def _find(proposals: dict[str, Proposal], ref: str) -> Proposal | None:
    if ref in proposals:
        return proposals[ref]
    matches = [p for p in proposals.values() if p.target_name == ref and p.is_live]
    if len(matches) == 1:
        return matches[0]
    return None


def _cas(proposal: Proposal, expected_revision_hash: str) -> str | None:
    """Compare-and-swap. Returns an error string, or None when the fence passed.

    Their tool schema: `expected_revision_hash` "fails if the stored proposal record changed". Without this,
    two writers -- or one writer working from a stale listing -- would both apply, and the second would silently
    discard the first.
    """
    if not expected_revision_hash:
        return None
    if expected_revision_hash != proposal.revision_hash:
        return (
            f"revision mismatch: the stored proposal is {proposal.revision_hash[:12]}, "
            f"expected {expected_revision_hash[:12]} -- re-read it and retry"
        )
    return None


def mark_stale(ref: str, reason: str, *, project_root: Path | None = None,
               expected_revision_hash: str = "") -> ProposalResult:
    """Move a pending proposal to `stale`. Terminal, and only reachable by explicit transition.

    Used when the live target changed under the proposal, so applying it would silently discard that change.
    """
    result = ProposalResult(ok=False)
    proposals = load_proposals(project_root)
    proposal = _find(proposals, ref)
    if proposal is None:
        result.errors.append(f"no proposal matching {ref!r}")
        return result
    if proposal.status != PENDING:
        result.errors.append(f"{proposal.id} is {proposal.status}, not pending")
        return result
    mismatch = _cas(proposal, expected_revision_hash)
    if mismatch:
        result.errors.append(mismatch)
        return result

    proposal.status = STALE
    proposal.stale_at = time.time()
    proposal.updated_at = proposal.stale_at
    proposal.status_reason = reason
    proposals[proposal.id] = proposal
    save_proposals(proposals, project_root)
    _append_event(project_root, proposal.id, "stale", reason)
    result.ok = True
    result.proposal = proposal
    return result


def reject(ref: str, reason: str = "", *, project_root: Path | None = None,
           expected_revision_hash: str = "") -> ProposalResult:
    return _terminal(ref, REJECTED, reason, project_root, expected_revision_hash)


def quarantine(ref: str, reason: str = "", *, project_root: Path | None = None,
               expected_revision_hash: str = "") -> ProposalResult:
    return _terminal(ref, QUARANTINED, reason, project_root, expected_revision_hash)


def _terminal(ref: str, status: str, reason: str, project_root: Path | None,
              expected_revision_hash: str) -> ProposalResult:
    result = ProposalResult(ok=False)
    proposals = load_proposals(project_root)
    proposal = _find(proposals, ref)
    if proposal is None:
        result.errors.append(f"no proposal matching {ref!r}")
        return result
    # Interrupted-apply recovery: a record left `pending` whose target already matches the draft has in fact
    # been applied, and marking it rejected would be wrong. Their `reconcile-transition.ts` does the same.
    if proposal.status == PENDING and _already_applied(proposal, project_root):
        proposal.status = APPLIED
        proposal.applied_at = time.time()
        proposal.updated_at = proposal.applied_at
        proposal.status_reason = "reconciled: the target already matches this draft"
        proposals[proposal.id] = proposal
        save_proposals(proposals, project_root)
        _append_event(project_root, proposal.id, "applied", "reconciled during terminal transition")
        result.ok = True
        result.proposal = proposal
        result.notes.append("reconciled as applied: the live file already matches the draft")
        return result

    if proposal.status not in (PENDING, QUARANTINED):
        result.errors.append(f"{proposal.id} is {proposal.status}; only a pending or quarantined proposal "
                             "can be moved")
        return result
    mismatch = _cas(proposal, expected_revision_hash)
    if mismatch:
        result.errors.append(mismatch)
        return result

    now = time.time()
    proposal.status = status
    proposal.updated_at = now
    proposal.status_reason = reason or f"marked {status}"
    if status == REJECTED:
        proposal.rejected_at = now
    else:
        proposal.quarantined_at = now
    proposals[proposal.id] = proposal
    save_proposals(proposals, project_root)
    _append_event(project_root, proposal.id, status, proposal.status_reason)
    result.ok = True
    result.proposal = proposal
    return result


def _already_applied(proposal: Proposal, project_root: Path | None) -> bool:
    try:
        live = (Path(proposal.target_dir) / "SKILL.md").read_text(encoding="utf-8")
    except OSError:
        return False
    return content_hash(live) == proposal.draft_hash


def apply(ref: str, *, project_root: Path | None = None, expected_revision_hash: str = "") -> ProposalResult:
    """Write a pending proposal into the live skills root, capturing a rollback record first.

    Refuses when the live target changed since the proposal was created: that is the `stale` condition, and
    applying anyway would discard whatever changed. The caller can `mark_stale` explicitly or re-propose against
    the new content.
    """
    result = ProposalResult(ok=False)
    proposals = load_proposals(project_root)
    proposal = _find(proposals, ref)
    if proposal is None:
        result.errors.append(f"no proposal matching {ref!r}")
        return result
    if proposal.status == APPLIED:
        result.ok = True
        result.proposal = proposal
        result.notes.append("already applied; nothing to do")
        return result
    if proposal.status != PENDING:
        result.errors.append(f"{proposal.id} is {proposal.status}; only a pending proposal can be applied")
        return result
    mismatch = _cas(proposal, expected_revision_hash)
    if mismatch:
        result.errors.append(mismatch)
        return result

    draft = draft_text(proposal, project_root)
    if not draft:
        result.errors.append(
            f"the draft for {proposal.id} is missing on disk; the proposal cannot be applied"
        )
        return result

    target_dir = Path(proposal.target_dir)
    skill_md = target_dir / "SKILL.md"
    previous = ""
    try:
        previous = skill_md.read_text(encoding="utf-8")
    except OSError:
        previous = ""

    # Staleness guard. Only meaningful for an update -- a create has no previous content to be stale against.
    # An empty `base_hash` means the proposal predates this field, and the check is skipped rather than guessed:
    # refusing every pre-existing proposal would be worse than applying one that might be stale, and the
    # rollback record below still makes it recoverable.
    if previous and proposal.base_hash and content_hash(previous) != proposal.base_hash:
        result.errors.append(
            "the live skill changed since this proposal was created; applying it would discard that change. "
            "Re-read the skill and re-propose, or mark this proposal stale."
        )
        return result

    rollback = {
        "proposal_id": proposal.id,
        "written_at": time.time(),
        "target_skill_file": str(skill_md),
        "action": proposal.kind,
        "previous_content_hash": content_hash(previous) if previous else "",
        "previous_content": previous,
        "support_files": proposal.support_files,
    }
    rollbacks = _read_json(_rollbacks_file(project_root), {})
    if not isinstance(rollbacks, dict):
        rollbacks = {}
    rollbacks[proposal.id] = rollback
    _write_json(_rollbacks_file(project_root), rollbacks)

    from .atomic_io import atomic_write_text

    target_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_text(skill_md, draft)
    for entry in proposal.support_files:
        side = _drafts_dir(project_root) / proposal.id / entry["path"]
        if side.is_file():
            dest = target_dir / entry["path"]
            dest.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(dest, side.read_text(encoding="utf-8"))

    now = time.time()
    proposal.status = APPLIED
    proposal.applied_at = now
    proposal.updated_at = now
    proposal.status_reason = f"applied to {skill_md}"
    proposals[proposal.id] = proposal
    save_proposals(proposals, project_root)
    _append_event(project_root, proposal.id, "applied", str(skill_md))

    result.ok = True
    result.proposal = proposal
    result.notes.append(f"written to {skill_md}")
    result.notes.append("a rollback record was captured before the write")
    return result


def rollback(proposal_id: str, *, project_root: Path | None = None) -> ProposalResult:
    """Restore the content captured before an apply. Consumes the record, so it cannot be replayed."""
    result = ProposalResult(ok=False)
    rollbacks = _read_json(_rollbacks_file(project_root), {})
    record = rollbacks.get(proposal_id) if isinstance(rollbacks, dict) else None
    if not isinstance(record, dict):
        result.errors.append(f"no rollback record for {proposal_id!r}")
        return result

    skill_md = Path(record.get("target_skill_file", ""))
    previous = record.get("previous_content", "")
    try:
        if previous:
            from .atomic_io import atomic_write_text

            atomic_write_text(skill_md, previous)
            result.notes.append(f"restored the previous content of {skill_md}")
        else:
            # The proposal created the file, so rolling back means removing it.
            if skill_md.is_file():
                skill_md.unlink()
            try:
                skill_md.parent.rmdir()
            except OSError:
                pass
            result.notes.append(f"removed {skill_md} (the proposal created it)")
    except OSError as exc:
        result.errors.append(f"could not roll back: {exc}")
        return result

    rollbacks.pop(proposal_id, None)
    _write_json(_rollbacks_file(project_root), rollbacks)
    _append_event(project_root, proposal_id, "rolled-back", str(skill_md))
    result.ok = True
    return result


def listing(project_root: Path | None = None, status: str = "") -> list[Proposal]:
    """Every proposal, newest first, with a missing draft annotated rather than hidden."""
    proposals = load_proposals(project_root)
    chosen = [p for p in proposals.values() if not status or p.status == status]
    chosen.sort(key=lambda p: p.created_at, reverse=True)
    for proposal in chosen:
        if draft_missing(proposal, project_root):
            proposal.status_reason = (
                proposal.status_reason + "; " if proposal.status_reason else ""
            ) + "degraded: draft-missing"
    return chosen


def summary(project_root: Path | None = None) -> dict[str, int]:
    counts = {status: 0 for status in STATUSES}
    for proposal in load_proposals(project_root).values():
        counts[proposal.status] = counts.get(proposal.status, 0) + 1
    counts["total"] = sum(counts.values())
    return counts
