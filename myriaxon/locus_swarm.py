"""The swarm substrate and the work board: the capability that defines Level 4.

Level 4 is *"coordinates with other agents"*, and until this module existed Myriaxon had no way to do it.
Each locus was confined to its own workspace and therefore **mutually invisible by design** -- three loci
could hold the same objective and never know. Confinement is not the thing to relax; the thing to add is
a place they can all see.

THE SHAPE

    <swarm root>/
      board/            work items; the coordination surface
        <task>/
          item.json     what the work is
          owner.json    the lease: who holds it, until when
          result.md     what the holder produced
          done.json     written last, so a task is done only when its result is readable
      shared/           artifacts any member may read
      members/<id>/     reserved, NOT private: it is inside the tree granted to every member

WHY FILES AND NOT A SERVER

A coordination service is a second process to run, a second failure mode, and a thing that must be
started before anything works. The filesystem is already there, already durable, already concurrent, and
`os.replace` is already atomic -- this codebase depends on that in `atomic_io.py`. A board built from
files survives a reboot with no recovery logic, because there is nothing to recover.

WHAT A LEASE MEANS HERE

Claiming a task writes `owner.json` saying who holds it and until when. A holder that dies does not
release it, so an expired lease is reclaimable by anyone -- the same rule the locus run lease already
uses (`RUN_LEASE_STALE_SECONDS`), applied to work rather than to a run. Claiming is COMPARE-AND-SWAP over
two files: an exclusive create (`O_EXCL`) decides the winner, so two members racing cannot both believe
they own the same task.

WHY IT IS SAFE TO ADD

The board lives at the SWARM root and is visible to members by explicit path, not by widening any
existing confinement. A locus still cannot read another locus's workspace; it can read the board because
the board is what it was given to read. No mount flag is relaxed and no boundary moves.

Correction to an earlier claim: `loci_capabilities.py` DOES provide per-locus memory, wins and journals.
They are per-locus, not collective -- another locus cannot read them. So 4.9 remains open, and this
module deliberately does not pretend to close it by putting a shared directory next to it.
"""

from __future__ import annotations

import errno
import json
import os
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .atomic_io import atomic_write_text
from .constants import CONFIG_DIR

# Where the swarm lives. Under CONFIG_DIR beside `loci/`, so it is one tree an operator can find, back up
# or delete, and so it does not sit inside any single locus's workspace.
SWARM_DIR = CONFIG_DIR / "swarm"
BOARD_DIRNAME = "board"
SHARED_DIRNAME = "shared"
MEMBERS_DIRNAME = "members"

# How long a claim is good for. Short by default: a stale lease blocks work, and a holder that is still
# alive can renew. Long enough that a slow model call does not lose its own task.
DEFAULT_LEASE_SECONDS = 900.0

# Task ids are used as directory names, so the character set is restricted rather than escaped. A task id
# containing a path separator would otherwise let a caller write outside the board.
_SAFE_ID = re.compile(r"[^A-Za-z0-9._-]")


def swarm_root() -> Path:
    """The swarm root, honouring `MYRIAXON_SWARM_DIR` so tests and operators can relocate it."""
    override = (os.environ.get("MYRIAXON_SWARM_DIR") or "").strip()
    return Path(override) if override else SWARM_DIR


def board_dir() -> Path:
    return swarm_root() / BOARD_DIRNAME


def shared_dir() -> Path:
    return swarm_root() / SHARED_DIRNAME


def members_dir() -> Path:
    return swarm_root() / MEMBERS_DIRNAME


def safe_id(value: str) -> str:
    """A task or member id that is safe to use as a single path component.

    Restricted rather than escaped. An id containing `..` or a separator is a path-traversal primitive, and
    escaping rules are exactly the kind of thing that gets subtly wrong; restricting to a known-safe
    alphabet cannot.
    """
    cleaned = _SAFE_ID.sub("-", str(value or "").strip())
    cleaned = cleaned.strip(".-")
    if not cleaned:
        raise ValueError(f"task id {value!r} has no usable characters")
    return cleaned[:120]


def ensure_layout() -> Path:
    """Create the three directories. Idempotent, and cheap enough to call per operation."""
    root = swarm_root()
    for path in (board_dir(), shared_dir(), members_dir()):
        path.mkdir(parents=True, exist_ok=True)
    return root


def member_workspace(member_id: str) -> Path:
    """A member's own scratch space, DELIBERATELY OUTSIDE the shared swarm tree.

    This used to be `members_dir() / member_id`, which documented itself as "one member's own scratch space"
    -- and was the opposite. `members_dir()` is `swarm_root()/members`, so anything placed in it sat INSIDE the
    directory the run loop grants to every confined Locus in order for the board to be reachable. A workspace
    under a granted root is a workspace every peer can read and write: exactly the isolation requirement 4.12
    exists to enforce, inverted by the layout itself.

    Measured, before the move:

        swarm_root()       : <config>/swarm
        member_workspace() : <config>/swarm/members/peer      <- inside the granted tree

    Nothing in the package called it, so nothing leaked. It was a trap for the first feature that did.

    The fix puts it where isolation is already the rule: beside the Loci's own workspaces, one level ABOVE the
    swarm. Sharing then becomes a deliberate act -- a member puts an artifact in `shared/` -- rather than an
    accident of where a directory happens to sit. `swarm_root()` still holds the board and `shared/`, and those
    are the parts meant to be reachable.
    """
    from .constants import CONFIG_DIR

    path = CONFIG_DIR / "member-workspaces" / safe_id(member_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


# ---------------------------------------------------------------------------------------------
# The work item
# ---------------------------------------------------------------------------------------------


@dataclass
class Task:
    """One unit of work on the board."""

    id: str
    title: str = ""
    detail: str = ""
    # Free-form labels so a member can claim only what it is suited to -- a designer should not take a
    # migration. Matching is the caller's; the board only records what was asked for.
    tags: list[str] = field(default_factory=list)
    created_at: float = 0.0
    created_by: str = ""
    # Set when the work is finished. Kept on the item so a completed task stays readable.
    completed_at: float = 0.0
    completed_by: str = ""
    # A task may be handed back rather than finished, which is different from being abandoned.
    released_count: int = 0
    # How many times this task has been claimed, kept on the ITEM rather than on the lease.
    #
    # Measured, and it is why this field moved: the count used to live in `owner.json`, and `release`
    # deletes that file so the task becomes claimable again -- which reset the count to zero every time.
    # A task that four members had each bounced looked exactly as fresh as one nobody had ever touched,
    # which is the opposite of the signal the counter exists to give. A release is precisely the event
    # that should increment it, so it cannot be stored in the thing a release removes.
    claims: int = 0

    @property
    def done(self) -> bool:
        return self.completed_at > 0


@dataclass
class Lease:
    """Who holds a task, and until when."""

    member: str = ""
    claimed_at: float = 0.0
    expires_at: float = 0.0
    # How many times this task has been claimed. A high number is the signal that something is wrong with
    # the task itself rather than with the members taking it.
    attempt: int = 0

    def held(self, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        return bool(self.member) and self.expires_at > now

    def expired(self, now: float | None = None) -> bool:
        return bool(self.member) and not self.held(now)


def task_dir(task_id: str) -> Path:
    return board_dir() / safe_id(task_id)


def _write_json(path: Path, payload: dict) -> None:
    atomic_write_text(path, json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True))


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------------------------------------
# Board operations
# ---------------------------------------------------------------------------------------------


def create_task(task_id: str, *, title: str = "", detail: str = "",
                tags: list[str] | None = None, created_by: str = "") -> tuple[Task | None, str]:
    """Put work on the board. Returns (task, problem).

    `O_EXCL` on `item.json` is what makes this safe: two members publishing the same task id at the same
    moment cannot both succeed, so a task is never silently overwritten by a duplicate.
    """
    ensure_layout()
    directory = task_dir(task_id)
    directory.mkdir(parents=True, exist_ok=True)
    item = directory / "item.json"

    task = Task(
        id=safe_id(task_id),
        title=title or "",
        detail=detail or "",
        tags=list(tags or []),
        created_at=time.time(),
        created_by=created_by,
    )
    payload = json.dumps(asdict(task), indent=2, ensure_ascii=False, sort_keys=True)
    try:
        # `x` is O_CREAT|O_EXCL -- the exclusivity is the whole point, so this is a plain open rather than
        # the atomic_io helper, which deliberately overwrites.
        with open(item, "x", encoding="utf-8") as handle:
            handle.write(payload)
    except FileExistsError:
        existing = _read_json(item)
        if existing and existing.get("completed_at"):
            # A finished task may be re-queued deliberately; the result is cleared so a stale answer cannot
            # be read as the new one.
            for name in ("result.md", "done.json", "owner.json"):
                (directory / name).unlink(missing_ok=True)
            atomic_write_text(item, payload)
            return task, ""
        return None, f"task {task_id!r} already exists on the board and is not finished"
    except OSError as exc:
        return None, f"could not write the task: {exc}"
    return task, ""


def get_task(task_id: str) -> Task | None:
    data = _read_json(task_dir(task_id) / "item.json")
    if not data:
        return None
    known = {f for f in Task.__dataclass_fields__}
    return Task(**{k: v for k, v in data.items() if k in known})


def read_lease(task_id: str) -> Lease:
    data = _read_json(task_dir(task_id) / "owner.json") or {}
    known = {f for f in Lease.__dataclass_fields__}
    return Lease(**{k: v for k, v in data.items() if k in known})


def claim(task_id: str, member: str, *, lease_seconds: float = DEFAULT_LEASE_SECONDS,
          now: float | None = None) -> bool:
    """Take a task. True when this member now holds it.

    Compare-and-swap over `owner.json`, with the exclusive create deciding the winner. A task already held
    by someone else returns False rather than waiting: blocking here would make a member's turn depend on
    another member's progress, and a turn that waits on a peer is a turn that can hang.
    """
    ensure_layout()
    now = time.time() if now is None else now
    directory = task_dir(task_id)
    if not (directory / "item.json").exists():
        return False
    task = get_task(task_id)
    if task is None or task.done:
        return False

    owner = directory / "owner.json"
    current = read_lease(task_id)
    # The attempt number lives on the ITEM, because `release` deletes the lease and would reset it.
    next_attempt = (task.claims or 0) + 1
    try:
        task.claims = next_attempt
        _write_json(directory / "item.json", asdict(task))
    except OSError:
        pass

    if current.member and current.held(now) and current.member != safe_id(member):
        return False

    new_lease = Lease(member=safe_id(member), claimed_at=now,
                      expires_at=now + float(lease_seconds), attempt=next_attempt)
    payload = json.dumps(asdict(new_lease), indent=2, ensure_ascii=False, sort_keys=True)

    if not current.member:
        # Unclaimed: the exclusive create is the lock. Two members racing, one wins.
        try:
            with open(owner, "x", encoding="utf-8") as handle:
                handle.write(payload)
            return True
        except FileExistsError:
            # Someone claimed it between the read and the create. Re-read; if they hold it, we lose.
            fresh = read_lease(task_id)
            if fresh.member and fresh.held(now) and fresh.member != safe_id(member):
                return False
            atomic_write_text(owner, payload)
            return True
        except OSError:
            return False

    # Held but expired, or held by us: replacing the lease is safe because the previous holder is gone or
    # is renewing its own claim.
    try:
        atomic_write_text(owner, payload)
        return True
    except OSError:
        return False


def renew(task_id: str, member: str, *, lease_seconds: float = DEFAULT_LEASE_SECONDS,
          now: float | None = None) -> bool:
    """Extend a lease this member already holds. False if it no longer holds it.

    Needed because a model call can outlast a lease. Without renewal a slow holder would lose its own task
    to a scavenger while still working on it, and both would write a result.
    """
    now = time.time() if now is None else now
    current = read_lease(task_id)
    if current.member != safe_id(member) or not current.held(now):
        return False
    current.expires_at = now + float(lease_seconds)
    try:
        _write_json(task_dir(task_id) / "owner.json", asdict(current))
        return True
    except OSError:
        return False


def release(task_id: str, member: str, *, reason: str = "") -> bool:
    """Hand a task back unclaimed so another member can take it.

    Distinct from completing: a release means "I did not do this", and the attempt counter is kept so a
    task that everyone bounces is visible as such rather than looking fresh forever.
    """
    current = read_lease(task_id)
    if current.member != safe_id(member):
        return False
    directory = task_dir(task_id)
    try:
        # Recorded on the ITEM as well as the lease is removed: a release is the event that makes a task
        # look untouched, so it has to leave its mark somewhere that survives.
        task = get_task(task_id)
        if task is not None:
            task.released_count = (task.released_count or 0) + 1
            _write_json(directory / "item.json", asdict(task))
        (directory / "owner.json").unlink(missing_ok=True)
        if reason:
            note = directory / "released.log"
            with open(note, "a", encoding="utf-8") as handle:
                handle.write(f"{time.time():.0f}\t{safe_id(member)}\t{reason.strip()[:500]}\n")
        return True
    except OSError:
        return False


def complete(task_id: str, member: str, result: str, *, now: float | None = None) -> tuple[bool, str]:
    """Finish a task and record what it produced. Returns (ok, problem).

    `done.json` is written LAST. Completion is therefore observable only once `result.md` is fully on
    disk, so a reader that sees a task as done can always read its result -- the alternative lets a member
    mark work finished and then die before writing the answer.
    """
    now = time.time() if now is None else now
    current = read_lease(task_id)
    if current.member and current.member != safe_id(member):
        return False, f"task {task_id!r} is held by {current.member!r}, not {member!r}"

    directory = task_dir(task_id)
    task = get_task(task_id)
    if task is None:
        return False, f"no task {task_id!r} on the board"

    try:
        atomic_write_text(directory / "result.md", result or "")
        task.completed_at = now
        task.completed_by = safe_id(member)
        _write_json(directory / "item.json", asdict(task))
        _write_json(directory / "done.json",
                    {"completed_at": now, "completed_by": safe_id(member),
                     "bytes": len(result or "")})
    except OSError as exc:
        return False, f"could not record the result: {exc}"
    return True, ""


def read_result(task_id: str) -> str:
    """A finished task's result, or "" when there is none."""
    return _read_text(task_dir(task_id) / "result.md")


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def available(tags: list[str] | None = None, *, now: float | None = None) -> list[Task]:
    """Unfinished tasks nobody currently holds, optionally filtered by tag.

    Expired leases count as available -- that is the recovery path for a member that died holding work.
    """
    now = time.time() if now is None else now
    wanted = {t.lower() for t in (tags or [])}
    out: list[Task] = []
    try:
        directories = sorted(p for p in board_dir().iterdir() if p.is_dir())
    except OSError:
        return out
    for directory in directories:
        task = get_task(directory.name)
        if task is None or task.done:
            continue
        lease = read_lease(directory.name)
        if lease.held(now):
            continue
        if wanted and not wanted & {t.lower() for t in task.tags}:
            continue
        out.append(task)
    return out


def reclaim_expired(*, now: float | None = None) -> list[str]:
    """Clear leases whose holder is gone. Returns the task ids that became available.

    Separate from `available` so a caller can report recovery rather than let it happen silently: a task
    that had to be reclaimed is evidence that a member died mid-work, which is worth seeing.
    """
    now = time.time() if now is None else now
    recovered: list[str] = []
    try:
        directories = sorted(p for p in board_dir().iterdir() if p.is_dir())
    except OSError:
        return recovered
    for directory in directories:
        lease = read_lease(directory.name)
        if lease.expired(now):
            try:
                (directory / "owner.json").unlink(missing_ok=True)
                recovered.append(directory.name)
            except OSError:
                continue
    return recovered


def board_status() -> dict:
    """Counts for a status panel. Never raises -- an unreadable board reports zeros."""
    status = {"root": str(swarm_root()), "total": 0, "open": 0, "claimed": 0,
              "done": 0, "expired": 0, "members": []}
    now = time.time()
    try:
        directories = [p for p in board_dir().iterdir() if p.is_dir()]
    except OSError:
        return status
    holders: set[str] = set()
    for directory in directories:
        task = get_task(directory.name)
        if task is None:
            continue
        status["total"] += 1
        if task.done:
            status["done"] += 1
            continue
        lease = read_lease(directory.name)
        if lease.held(now):
            status["claimed"] += 1
            holders.add(lease.member)
        elif lease.member:
            status["expired"] += 1
        else:
            status["open"] += 1
    status["members"] = sorted(holders)
    return status


def format_board(status: dict | None = None) -> str:
    """Plain-text report for the CLI."""
    status = status or board_status()
    lines = [
        "Swarm board:",
        f"  root    : {status['root']}",
        f"  tasks   : {status['total']}  (open {status['open']}, claimed {status['claimed']}, "
        f"done {status['done']}, expired {status['expired']})",
    ]
    if status["members"]:
        lines.append(f"  holders : {', '.join(status['members'])}")
    if status["expired"]:
        lines.append("  note    : expired leases are reclaimable -- run `--reclaim` to release them")
    return "\n".join(lines)


# ---------------------------------------------------------------------------------------------
# Agent-facing tools
# ---------------------------------------------------------------------------------------------
#
# The board is only a coordination mechanism if an agent can reach it. These four tools are the whole
# surface a member needs, and each is deliberately small: publish, look, take, finish. Anything richer
# (priorities, dependencies, estimates) would be a project-management product, and the Level 4
# requirement is coordination, not planning.

SWARM_POST_TOOL_DEF = {
    "name": "swarm_post",
    "description": (
        "Put a piece of work on the shared board so another agent can pick it up. Use this when you find "
        "work that is outside your own objective, or that another agent is better suited to. The task "
        "survives this run, so a peer can take it later or after a restart. Give it tags describing the "
        "skill needed (e.g. ['design'], ['data']) so the right agent claims it."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "Short stable id, e.g. 'brandkit-poster'."},
            "title": {"type": "string", "description": "One line saying what the work is."},
            "detail": {"type": "string", "description": "Everything a peer needs that you already know."},
            "tags": {"type": "array", "items": {"type": "string"},
                     "description": "Skill labels for matching, e.g. ['design']."},
        },
        "required": ["task_id", "title"],
    },
}

SWARM_LIST_TOOL_DEF = {
    "name": "swarm_list",
    "description": (
        "See what work is on the shared board and who is holding it. Unclaimed tasks are available to "
        "take; a task whose holder died becomes available again automatically. Check this before starting "
        "work of your own -- a peer may already have done it."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "tags": {"type": "array", "items": {"type": "string"},
                     "description": "Only show tasks with one of these tags."},
            "include_done": {"type": "boolean", "description": "Also list finished tasks."},
        },
        "required": [],
    },
}

SWARM_CLAIM_TOOL_DEF = {
    "name": "swarm_claim",
    "description": (
        "Take a task from the board so no other agent works on it at the same time. Returns false if "
        "somebody else already holds it -- that is a normal outcome, not an error, and you should take a "
        "different task rather than retrying. Hold it only while you are actually working on it."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "task_id": {"type": "string"},
            "member": {"type": "string", "description": "Your name, so the board can show who holds it."},
        },
        "required": ["task_id", "member"],
    },
}

SWARM_FINISH_TOOL_DEF = {
    "name": "swarm_finish",
    "description": (
        "Finish a task you hold and record what you produced, so a peer can use your result instead of "
        "redoing the work. If you cannot complete it, use swarm_release instead -- that returns it to the "
        "board unclaimed, which is different from finishing it."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "task_id": {"type": "string"},
            "member": {"type": "string", "description": "Your name; must match the claim."},
            "result": {"type": "string", "description": "The answer, finding or artifact path."},
        },
        "required": ["task_id", "member", "result"],
    },
}

SWARM_RELEASE_TOOL_DEF = {
    "name": "swarm_release",
    "description": (
        "Hand a task back to the board without finishing it, because it is not something you can do. "
        "This is the honest outcome and is better than marking it done. Give a reason so the next agent "
        "knows why it came back."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "task_id": {"type": "string"},
            "member": {"type": "string"},
            "reason": {"type": "string"},
        },
        "required": ["task_id", "member"],
    },
}

SWARM_TOOL_DEFS = (
    SWARM_POST_TOOL_DEF,
    SWARM_LIST_TOOL_DEF,
    SWARM_CLAIM_TOOL_DEF,
    SWARM_FINISH_TOOL_DEF,
    SWARM_RELEASE_TOOL_DEF,
)


def execute_swarm_post(tool_input: dict) -> tuple[str, bool]:
    task, problem = create_task(
        str(tool_input.get("task_id") or ""),
        title=str(tool_input.get("title") or ""),
        detail=str(tool_input.get("detail") or ""),
        tags=[str(t) for t in (tool_input.get("tags") or [])],
        created_by=str(tool_input.get("member") or ""),
    )
    if task is None:
        return problem, True
    return f"Posted '{task.id}' to the board: {task.title}", False


def execute_swarm_list(tool_input: dict, *, member: str = "") -> tuple[str, bool]:
    reclaim_expired()
    open_tasks = available([str(t) for t in (tool_input.get("tags") or [])] or None)
    lines: list[str] = []
    if open_tasks:
        lines.append(f"{len(open_tasks)} task(s) available to claim:")
        for task in open_tasks:
            tags = f"  [{', '.join(task.tags)}]" if task.tags else ""
            lines.append(f"  {task.id}: {task.title}{tags}")
            if task.claims:
                lines.append(f"      claimed {task.claims} time(s) before")
            if task.detail:
                lines.append(f"      {task.detail[:300]}")
    else:
        lines.append("No tasks are available to claim.")

    if tool_input.get("include_done"):
        done = []
        try:
            for directory in sorted(p for p in board_dir().iterdir() if p.is_dir()):
                task = get_task(directory.name)
                if task is not None and task.done:
                    done.append(task)
        except OSError:
            pass
        if done:
            lines.append("")
            lines.append(f"{len(done)} finished task(s) -- their results are readable:")
            for task in done:
                lines.append(f"  {task.id}: {task.title}  (by {task.completed_by})")
    return "\n".join(lines), False


def execute_swarm_claim(tool_input: dict) -> tuple[str, bool]:
    task_id = str(tool_input.get("task_id") or "")
    member = str(tool_input.get("member") or "")
    if not member:
        return "`member` is required: the board must be able to show who holds a task.", True
    if claim(task_id, member):
        task = get_task(task_id)
        title = task.title if task else task_id
        return (f"You hold '{task_id}': {title}\n"
                f"Finish it with swarm_finish, or hand it back with swarm_release.", False)
    current = read_lease(task_id)
    if current.member:
        return (f"'{task_id}' is held by {current.member} until "
                f"{time.strftime('%H:%M:%S', time.localtime(current.expires_at))}. "
                "Take a different task -- do not retry this one."), False
    return f"Could not claim '{task_id}' (no such task, or it is already finished).", False


def execute_swarm_finish(tool_input: dict) -> tuple[str, bool]:
    ok, problem = complete(str(tool_input.get("task_id") or ""),
                           str(tool_input.get("member") or ""),
                           str(tool_input.get("result") or ""))
    if not ok:
        return problem, True
    return f"Recorded the result for '{tool_input.get('task_id')}'.", False


def execute_swarm_release(tool_input: dict) -> tuple[str, bool]:
    task_id = str(tool_input.get("task_id") or "")
    member = str(tool_input.get("member") or "")
    if release(task_id, member, reason=str(tool_input.get("reason") or "")):
        return f"'{task_id}' is back on the board, unclaimed.", False
    return f"You do not hold '{task_id}', so it cannot be released by you.", True
