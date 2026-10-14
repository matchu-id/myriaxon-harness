"""Session lineage: branching a session, and navigating the tree of branches.

## The gap this closes

Myriaxon stored exactly one transcript per project directory (`session_store`), so there was no way to explore a
second direction from a point in a conversation without destroying the first. Asked to "try this another way",
the only honest options were to argue with the existing context or to throw it away.

pi-harness has this (`sessionFork`, `sessionTree` in `src/main/harness/harness-capabilities.ts`, gated on
`isPersisted() && typeof manager.createBranchedSession === 'function'`), and their capability detector gates the
whole feature on the session being persisted -- which is the right instinct: a fork of something that was never
written down cannot be a separate thing.

## The design

A **branch** is a session whose transcript starts as a copy of another session's prefix. The lineage lives in a
separate index (`BRANCH_INDEX_FILENAME`) rather than inside the transcript, because the transcript is a wire
format all three providers read and write; adding bookkeeping fields to it would mean every provider path has to
tolerate them.

Each entry records `parent`, `fork_index` (how many messages were inherited) and `name`. The tree is then derived:
`ancestors()` walks up, `descendants()` walks down, `path_from_root()` gives the ancestry chain.

## The four decisions that matter

**Forking from index N copies N messages, not N-1.** The index is an exclusive bound on the *parent's* message
list, so `fork_index=0` is an empty branch and `fork_index=len(messages)` is a copy of the whole conversation. An
index outside `0..len` is **refused** rather than clamped: clamping would silently give you a different
conversation than the one you asked for, which is worse than an error.

**A branch does not modify its parent.** Forking writes a new session file; the parent's transcript, claim and
`last_active` are untouched. That is what makes forking safe to do speculatively.

**Lineage survives the transcript being replaced.** The index is keyed by session id and rewrite-on-change, so a
branch stays reachable even after the parent's file is overwritten by later turns -- the parent's *current* state
is not what the fork point referred to.

**Cycles are refused.** `set_parent` walks the prospective ancestry and refuses a link that would make a session
its own ancestor. Nothing in the UI can produce one, which is exactly why it must be checked here: the guard is for
the programmatic caller and the hand-edited index.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .atomic_io import atomic_write_text
from .constants import PROJECT_SESSION_DIR

# Beside the session file, in the same project directory: a branch index describes THIS project's sessions, and
# putting it anywhere else would break the moment two projects are open.
BRANCH_INDEX_FILENAME = "branches.json"

# A session id is opaque, but it must be filename-safe and stable. Timestamp prefix so a directory listing sorts
# into creation order, which is what a person expects when looking for "the one from this morning".
def new_session_id() -> str:
    return f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"


@dataclass
class Branch:
    """One session's place in the tree."""

    session_id: str
    name: str = ""
    # The session this was forked from, if any. Empty means a root.
    parent: str = ""
    # How many of the parent's messages were inherited. An EXCLUSIVE bound on the parent's list at fork time.
    fork_index: int = 0
    created_at: float = field(default_factory=time.time)
    last_active: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "name": self.name,
            "parent": self.parent,
            "fork_index": self.fork_index,
            "created_at": self.created_at,
            "last_active": self.last_active,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Branch":
        return cls(
            session_id=str(data.get("session_id") or ""),
            name=str(data.get("name") or ""),
            parent=str(data.get("parent") or ""),
            fork_index=int(data.get("fork_index") or 0),
            created_at=float(data.get("created_at") or 0.0),
            last_active=float(data.get("last_active") or 0.0),
        )

    def label(self) -> str:
        """How this branch is named in a listing. Never empty, so a tree is readable."""
        return self.name.strip() or self.session_id


def _index_path(project_root: Path | None = None) -> Path:
    root = project_root if project_root is not None else Path.cwd()
    return root / PROJECT_SESSION_DIR / BRANCH_INDEX_FILENAME


def load_tree(project_root: Path | None = None) -> dict[str, Branch]:
    """Read the branch index. A corrupt or missing index yields an empty tree, never an exception.

    An unreadable index costs you the tree view; raising would cost you the session, because every caller of this
    is on a path that also needs to work.
    """
    path = _index_path(project_root)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    entries = data.get("branches") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        return {}
    out: dict[str, Branch] = {}
    for item in entries:
        if not isinstance(item, dict):
            continue
        branch = Branch.from_dict(item)
        if branch.session_id:
            out[branch.session_id] = branch
    return out


def save_tree(tree: dict[str, Branch], project_root: Path | None = None) -> bool:
    path = _index_path(project_root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"branches": [b.to_dict() for b in sorted(tree.values(), key=lambda b: b.created_at)]}
        atomic_write_text(path, json.dumps(payload, indent=2))
        return True
    except Exception:  # noqa: BLE001
        return False


def register(
    session_id: str,
    *,
    name: str = "",
    parent: str = "",
    fork_index: int = 0,
    project_root: Path | None = None,
) -> Branch:
    """Add or update a session in the tree. Idempotent by id.

    Registers the session itself as well as its lineage: a root session created by an ordinary `save_session` gets
    an entry the first time anything asks about the tree, so a project with branched sessions never shows a
    dangling parent reference to a session that exists but was never registered.
    """
    tree = load_tree(project_root)
    existing = tree.get(session_id)
    if existing is None:
        existing = Branch(session_id=session_id, name=name, parent=parent, fork_index=fork_index)
    else:
        if name:
            existing.name = name
        if parent:
            existing.parent = parent
            existing.fork_index = fork_index
    existing.last_active = time.time()
    tree[session_id] = existing
    save_tree(tree, project_root)
    return existing


def ancestors(session_id: str, project_root: Path | None = None) -> list[Branch]:
    """The chain from this session's parent up to its root, nearest first.

    Stops on a cycle as well as on a missing entry: a hand-edited index must not be able to hang a listing, and
    `visited` is cheaper than trusting the data.
    """
    tree = load_tree(project_root)
    chain: list[Branch] = []
    seen = {session_id}
    current = tree.get(session_id)
    while current is not None and current.parent:
        if current.parent in seen:
            break
        seen.add(current.parent)
        parent = tree.get(current.parent)
        if parent is None:
            # A parent that is not in the index: reported as the end of the chain rather than invented.
            break
        chain.append(parent)
        current = parent
    return chain


def path_from_root(session_id: str, project_root: Path | None = None) -> list[Branch]:
    """Root first, this session last. The order a person reads a lineage in."""
    tree = load_tree(project_root)
    own = tree.get(session_id)
    chain = list(reversed(ancestors(session_id, project_root)))
    if own is not None:
        chain.append(own)
    return chain


def children(session_id: str, project_root: Path | None = None) -> list[Branch]:
    tree = load_tree(project_root)
    return sorted(
        (b for b in tree.values() if b.parent == session_id),
        key=lambda b: b.created_at,
    )


def descendants(session_id: str, project_root: Path | None = None) -> list[Branch]:
    """Every branch below this one, breadth first. Cycle-safe."""
    out: list[Branch] = []
    seen = {session_id}
    queue = [session_id]
    tree = load_tree(project_root)
    while queue:
        current = queue.pop(0)
        for child in tree.values():
            if child.parent != current or child.session_id in seen:
                continue
            seen.add(child.session_id)
            out.append(child)
            queue.append(child.session_id)
    return out


def roots(project_root: Path | None = None) -> list[Branch]:
    tree = load_tree(project_root)
    known = set(tree)
    return sorted(
        (b for b in tree.values() if not b.parent or b.parent not in known),
        key=lambda b: b.created_at,
    )


def set_parent(
    session_id: str, parent: str, *, fork_index: int = 0, project_root: Path | None = None
) -> tuple[bool, str]:
    """Re-parent a session. Returns `(ok, reason)`.

    Refuses a self-link and any link that would make the session its own ancestor. Nothing in the normal flow can
    produce either, which is exactly why the check belongs here -- it is for the programmatic caller and for an
    index someone edited by hand.
    """
    if not session_id or not parent:
        return False, "both a session id and a parent are required"
    if session_id == parent:
        return False, "a session cannot be its own parent"
    tree = load_tree(project_root)
    # Walk up from the prospective parent: if we reach this session, the link would close a cycle.
    seen = {parent}
    current = tree.get(parent)
    while current is not None and current.parent:
        if current.parent == session_id:
            return False, (
                f"{parent!r} is a descendant of {session_id!r}, so this link would make the session its own "
                "ancestor"
            )
        if current.parent in seen:
            break
        seen.add(current.parent)
        current = tree.get(current.parent)

    branch = tree.get(session_id) or Branch(session_id=session_id)
    branch.parent = parent
    branch.fork_index = max(0, int(fork_index))
    branch.last_active = time.time()
    tree[session_id] = branch
    save_tree(tree, project_root)
    return True, "re-parented"


def resume_branch(session_id: str, project_root: Path | None = None) -> tuple[bool, str]:
    """Make a branch the ACTIVE session, keeping it in the tree. Returns `(ok, reason)`.

    A branch is created as an inactive file so that taking one cannot disturb the conversation you are in. Resuming
    is the deliberate opposite: it copies the branch's transcript into the main session file, which is what every
    provider path reads. The branch file is left in place, so the fork point is still reachable afterwards and
    resuming is repeatable rather than destructive.
    """
    from . import session_store

    branch = session_store.load_session(project_root, session_id)
    if branch is None:
        return False, f"no branch or session with id {session_id!r}"
    messages = branch.get("messages")
    if not isinstance(messages, list):
        return False, f"session {session_id!r} has no usable transcript"

    # `session_id=session_id` keeps the identity stable, so a resume does not look like a brand-new session to the
    # tree, and a second resume of the same branch is a no-op rather than a duplicate.
    written = session_store.save_session(
        provider=str(branch.get("provider") or ""),
        model=str(branch.get("model") or ""),
        mode=str(branch.get("mode") or ""),
        effort=branch.get("effort"),
        thinking=bool(branch.get("thinking")),
        messages=messages,
        project_root=project_root,
        session_id=session_id,
    )
    if not written:
        return False, written.reason or "the session could not be made active"
    register(session_id, project_root=project_root)
    return True, f"resumed [{session_id}] with {len(messages)} message(s)"


BRANCH_TOOL_DEF = {
    "name": "branch_session",
    "description": (
        "Branch the conversation so you can try a different approach WITHOUT losing the current one.\n"
        "Use it when the user says 'try another way', when you want to attempt something risky, or when you "
        "would otherwise have to argue with a context that has already committed to a wrong direction.\n"
        "The active session is left byte-for-byte untouched -- a fork writes a new file. Resume the branch "
        "with the CLI (`myriaxon branch --resume <id>`) when you want to continue in it."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "A label for the branch, e.g. 'try-dataclass'."},
            "at": {
                "type": "integer",
                "description": (
                    "Inherit only the first N messages of the current conversation. Omit for all of them. "
                    "An N past the end is refused rather than clamped."
                ),
            },
        },
        "required": [],
    },
}


def execute_branch_session(tool_input: dict, ctx=None) -> tuple[str, bool]:
    """The `branch_session` tool: fork the active session."""
    result = fork_session(
        name=str(tool_input.get("name") or ""),
        fork_index=tool_input.get("at"),
        project_root=Path.cwd(),
    )
    if not result.ok:
        return result.render(), True
    lines = [
        result.render(),
        "",
        "The active session is unchanged, so nothing you have done is at risk.",
        f"To continue in the branch: myriaxon branch --resume {result.session_id}",
    ]
    return "\n".join(lines), False


__all__ = [
    "BRANCH_INDEX_FILENAME", "Branch", "ForkResult", "BRANCH_TOOL_DEF",
    "new_session_id", "load_tree", "save_tree", "register", "ancestors", "path_from_root",
    "children", "descendants", "roots", "set_parent", "fork_session", "resume_branch",
    "fork_point_is_valid", "render_tree", "execute_branch_session",
]


def fork_point_is_valid(fork_index: int, parent_message_count: int) -> tuple[bool, str]:
    """Whether `fork_index` names a real point in a parent of that length.

    Separated from `fork_session` so the check can be exercised without touching the filesystem, and because it is
    the one rule that decides whether a fork means what the caller thinks it means.
    """
    if fork_index < 0:
        return False, f"a fork index cannot be negative ({fork_index})"
    if fork_index > parent_message_count:
        return False, (
            f"the fork index {fork_index} is past the end of the parent's {parent_message_count} messages. "
            "Clamping it would silently give you a different conversation than the one you asked for, so the "
            "fork is refused instead."
        )
    return True, ""


@dataclass
class ForkResult:
    """What a fork produced."""

    ok: bool
    session_id: str = ""
    inherited: int = 0
    parent: str = ""
    reason: str = ""

    def render(self) -> str:
        if not self.ok:
            return f"fork refused: {self.reason}"
        where = f" from [{self.parent}]" if self.parent else ""
        return f"branched [{self.session_id}]{where} with {self.inherited} inherited message(s)"


def fork_session(
    *,
    name: str = "",
    fork_index: int | None = None,
    project_root: Path | None = None,
    session_id: str = "",
) -> ForkResult:
    """Branch the current session, inheriting its first `fork_index` messages.

    The parent is the session stored in `project_root`; the branch is written to a NEW session file and the parent
    is left byte-for-byte alone. That is what makes a fork safe to take speculatively -- nothing about trying
    another direction can damage the conversation you already had.

    `fork_index=None` means "everything so far". An out-of-range index is **refused**, not clamped.

    The new transcript keeps the parent's provider and model: a branch is a continuation of the same work, and
    silently switching models because the branch was created from a different surface would make the two
    transcripts incomparable.
    """
    from . import session_store

    # The parent is the ACTIVE session, which `load_session` with no id resolves to the main file. A branch being
    # forked from is itself active by definition -- you have to resume it before you can continue working in it.
    parent_data = session_store.load_session(project_root)
    if parent_data is None:
        return ForkResult(ok=False, reason=(
            "there is no active session in this directory to branch. A fork of something that was never written "
            "down cannot be a separate thing -- run a turn first, or resume a branch."
        ))

    messages = parent_data.get("messages") or []
    if not isinstance(messages, list):
        return ForkResult(ok=False, reason="the saved session's messages are not a list")

    index = len(messages) if fork_index is None else int(fork_index)
    valid, why = fork_point_is_valid(index, len(messages))
    if not valid:
        return ForkResult(ok=False, reason=why)

    parent_id = session_id or str(parent_data.get("session_id") or "")
    new_id = new_session_id()
    inherited = list(messages[:index])

    # Written through the same atomic path as any other session, so a crash mid-fork cannot leave a half-written
    # branch that later loads as a corrupt transcript.
    #
    # `session_id=new_id` is load-bearing: without it every branch would be written to the ONE main session file,
    # so creating a branch would destroy the conversation it branched from. That was the first version, and it is
    # the exact opposite of the feature's purpose.
    written = session_store.save_session(
        provider=str(parent_data.get("provider") or ""),
        model=str(parent_data.get("model") or ""),
        mode=str(parent_data.get("mode") or ""),
        effort=parent_data.get("effort"),
        thinking=bool(parent_data.get("thinking")),
        messages=inherited,
        project_root=project_root,
        session_id=new_id,
        as_branch=True,
    )
    if not written:
        return ForkResult(ok=False, reason=written.reason or "the branch could not be written")

    register(new_id, name=name, parent=parent_id, fork_index=index, project_root=project_root)
    if parent_id:
        # Register the parent too, so a tree listing never shows a parent reference to a session that exists on
        # disk but was never entered in the index.
        register(parent_id, project_root=project_root)
    return ForkResult(ok=True, session_id=new_id, inherited=index, parent=parent_id)


def render_tree(project_root: Path | None = None) -> str:
    """The lineage as indented text, deepest last.

    Rendered here rather than in a CLI so the terminal and the dashboard cannot disagree about what the tree is.
    """
    tree = load_tree(project_root)
    if not tree:
        return (
            "No branches recorded for this directory. `branch_session` splits the conversation here so you can "
            "try another direction without losing this one."
        )
    lines: list[str] = []
    seen: set[str] = set()

    def walk(branch: Branch, depth: int) -> None:
        if branch.session_id in seen:
            lines.append(f"{'  ' * depth}- [{branch.session_id}] (cycle -- not expanded)")
            return
        seen.add(branch.session_id)
        where = f"  (from {branch.fork_index} message(s))" if branch.parent else ""
        lines.append(f"{'  ' * depth}- [{branch.label()}]{where}")
        for child in children(branch.session_id, project_root):
            walk(child, depth + 1)

    for root in roots(project_root):
        walk(root, 0)
    # Anything unreachable from a root (a cycle, or a dangling parent chain) is still shown, because a branch a
    # listing hides is a branch nobody can delete.
    for branch in sorted(tree.values(), key=lambda b: b.created_at):
        if branch.session_id not in seen:
            lines.append(f"- [{branch.label()}] (unreachable -- check for a cycle in the index)")
    return "\n".join(lines)
