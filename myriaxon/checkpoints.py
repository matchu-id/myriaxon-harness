import hashlib
import json
import re
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path

from rich.panel import Panel
from rich.markup import escape

from .constants import DEFAULT_IGNORE_PATTERNS, PROJECT_CHECKPOINTS_SUBDIR, SHADOW_HISTORY_DIR
from .logging_setup import logger
from .tool_context import ToolContext

CHECKPOINT_LIST_TOOL_DEF = {
    "name": "checkpoint_list",
    "description": (
        "List recent file-edit checkpoints (automatic snapshots taken before write_file/edit_file/"
        "edit_notebook overwrite a file). Read-only, no confirmation."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "limit": {"type": "integer", "description": "Max entries to return. Optional."},
        },
    },
}

CHECKPOINT_RESTORE_TOOL_DEF = {
    "name": "checkpoint_restore",
    "description": (
        "Roll a file back to a snapshot: restore or revert it to the content captured in a given checkpoint id "
        "(see checkpoint_list). Undo an edit by naming the checkpoint taken before it. "
        "This overwrites the file's current content -- requires user confirmation, and itself "
        "creates a new checkpoint first, so a restore can be undone."
    ),
    "input_schema": {
        "type": "object",
        "properties": {"id": {"type": "string"}},
        "required": ["id"],
    },
}


def _checkpoints_dir(project_root: Path | None = None) -> Path:
    root = project_root if project_root is not None else Path.cwd()
    return root / PROJECT_CHECKPOINTS_SUBDIR


def _index_path(project_root: Path | None = None) -> Path:
    return _checkpoints_dir(project_root) / "index.json"


# One lock per index file, created on demand and kept for the life of the process.
#
# Keyed by the resolved path rather than global, so two projects do not serialise against each other
# while one project's writers still cannot interleave. The dict itself is guarded because two threads
# can reach a new project's first checkpoint simultaneously.
_index_locks: dict[str, threading.Lock] = {}
_index_locks_guard = threading.Lock()


def _index_lock(index_path: Path) -> threading.Lock:
    key = str(index_path.resolve())
    with _index_locks_guard:
        lock = _index_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _index_locks[key] = lock
        return lock


def shadow_git_available() -> bool:
    return shutil.which("git") is not None


def _shadow_repo_dir(project_root: Path) -> Path:
    # Lives outside the project entirely (under ~/.myriaxon/history), keyed by a hash of the
    # resolved project root -- never collides with a project's own .git (if any), and works even
    # in a project that isn't a git repo at all (confirmed this environment: myriaxon itself
    # wasn't a git repo before this feature was built).
    digest = hashlib.sha1(str(project_root.resolve()).encode("utf-8")).hexdigest()[:16]
    return SHADOW_HISTORY_DIR / digest


def _run_shadow_git(args: list[str], project_root: Path, git_dir: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", f"--git-dir={git_dir}", f"--work-tree={project_root}", *args],
        cwd=project_root,
        capture_output=True,
        text=True,
    )


def _shadow_repo_is_usable(git_dir: Path) -> bool:
    """Whether git will actually accept this directory as a repository.

    Two files are the difference between a repository and a directory full of objects:

    * ``config`` -- without it, `git --git-dir=<dir>` refuses outright with "not a git repository".
    * ``HEAD`` -- without it, git cannot resolve a branch to commit onto.

    Checked because a `.git` directory is not proof of a repository. Measured on this machine: a
    sync client (OneDrive, with the project living under a synced folder) removed every hidden file
    inside `.myriaxon/history/*/.git/`, leaving `objects/` intact and `config`/`HEAD` gone. All
    fifteen shadow repos were in that state, and the old `if git_dir.is_dir(): return git_dir`
    treated each one as healthy -- so every checkpoint after the damage ran `git add`/`git commit`
    against a directory git rejects, returned None, and the checkpoint silently lost its shadow
    commit while looking like it had been recorded.
    """
    return (git_dir / "config").is_file() and (git_dir / "HEAD").is_file()


def _ensure_shadow_repo(project_root: Path) -> Path | None:
    """Best-effort, like everything else in this module: returns the shadow repo's git-dir if
    usable, None if git isn't installed or init failed -- /rewind is simply unavailable in that
    case, same non-fatal degradation as the per-file .bak checkpoints already have. Initializes
    a repo-local (not global) commit identity and a small built-in excludes list (independent of
    whatever .gitignore the real project may or may not have) so a project with no .gitignore of
    its own doesn't get its entire .venv/node_modules tree committed into shadow history.

    A damaged repo is re-initialised rather than trusted. The previous check was `git_dir.is_dir()`,
    which answered "is there a directory" to a question that means "can git use this" -- and a
    damaged repo is worse than a missing one, because a missing one is created while a damaged one
    is used and silently fails on every commit. Re-initialising discards that history, which is the
    honest outcome: the commits in it were already unreachable.
    """
    if not shadow_git_available():
        return None
    repo_dir = _shadow_repo_dir(project_root)
    git_dir = repo_dir / ".git"
    if _shadow_repo_is_usable(git_dir):
        return git_dir

    repo_dir.mkdir(parents=True, exist_ok=True)
    if _run_shadow_git(["init"], project_root, git_dir).returncode != 0:
        return None
    if not _shadow_repo_is_usable(git_dir):
        # `init` succeeded and the result still is not usable -- a filesystem that cannot hold the
        # files, or an aggressive sync client. Report it as unavailable rather than handing back a
        # path that will fail on every later call.
        logger.warning("shadow history repo at %s is not usable after init", git_dir)
        return None
    _run_shadow_git(["config", "user.email", "myriaxon@local"], project_root, git_dir)
    _run_shadow_git(["config", "user.name", "Myriaxon shadow history"], project_root, git_dir)

    excludes_file = repo_dir / "excludes"
    excludes_file.write_text("\n".join(DEFAULT_IGNORE_PATTERNS) + "\n", encoding="utf-8")
    _run_shadow_git(["config", "core.excludesFile", str(excludes_file)], project_root, git_dir)
    return git_dir


def _shadow_commit(project_root: Path, message: str) -> str | None:
    """Commits the CURRENT on-disk state of the whole project tree -- called from
    record_checkpoint before the triggering tool's write actually lands, so this captures the
    pre-edit state. Returns the new commit's short sha, or None if shadow-git isn't available or
    the commit failed outright.

    --allow-empty is required, not optional -- confirmed live this matters: the very first
    checkpoint ever taken (a brand-new shadow repo, snapshotting a to-be-created file that
    doesn't exist on disk yet) has *nothing* to commit, and plain `git commit` refuses with
    "nothing to commit" in that case. Without --allow-empty, that checkpoint silently got no
    commit sha at all and vanished from /rewind's history. --allow-empty guarantees every
    checkpoint_id maps to a real, retrievable commit, with no fallback-to-previous-HEAD
    ambiguity needed."""
    git_dir = _ensure_shadow_repo(project_root)
    if git_dir is None:
        return None
    if _run_shadow_git(["add", "-A"], project_root, git_dir).returncode != 0:
        return None

    commit_result = _run_shadow_git(["commit", "--allow-empty", "-m", message], project_root, git_dir)
    if commit_result.returncode != 0:
        return None

    # git commit's own summary line already contains the sha ("[branch sha] message" or
    # "[branch (root-commit) sha] message") -- parsing it directly avoids a third subprocess
    # spawn (a separate `git rev-parse HEAD`) on every single checkpoint. Falls back to the old
    # rev-parse call if the summary format ever doesn't match, rather than ever silently breaking
    # /rewind over a speed optimization.
    match = re.search(r"\[[^\]]*?(?:\(root-commit\)\s+)?([0-9a-f]{4,40})\]", commit_result.stdout)
    if match:
        return match.group(1)
    rev = _run_shadow_git(["rev-parse", "HEAD"], project_root, git_dir)
    return rev.stdout.strip()[:12] if rev.returncode == 0 else None


def record_checkpoint(
    path: Path, old_content: str, tool_name: str, project_root: Path | None = None, ctx: ToolContext | None = None
) -> str | None:
    """Best-effort: snapshot old_content before a destructive overwrite. Never raises -- a
    checkpoint failure must never block the user's actual edit. When ctx is provided, also takes
    a whole-project shadow-git snapshot (see _shadow_commit) and tags it with the conversation's
    turn-start message count, so /rewind can revert both files and conversation together -- ctx
    is optional (defaults to None) so checkpoint_restore's own internal call, which shouldn't
    itself count as a new turn boundary, can skip that part."""
    root = project_root if project_root is not None else Path.cwd()
    try:
        checkpoint_id = uuid.uuid4().hex[:12]
        checkpoints_dir = _checkpoints_dir(project_root)
        checkpoints_dir.mkdir(parents=True, exist_ok=True)

        backup_file = checkpoints_dir / f"{checkpoint_id}.bak"
        backup_file.write_text(old_content, encoding="utf-8")

        entry = {
            "id": checkpoint_id,
            "timestamp": time.time(),
            "tool_name": tool_name,
            "original_path": str(path),
            "backup_file": str(backup_file),
        }
        if ctx is not None:
            shadow_commit = _shadow_commit(root, f"checkpoint {checkpoint_id}: {tool_name} on {path.name}")
            if shadow_commit is not None:
                entry["shadow_commit"] = shadow_commit
                entry["message_count"] = ctx.current_turn_message_count

        index_path = _index_path(project_root)
        # The read-modify-write is serialised and the write is atomic, because three sub-agents editing
        # three files at once all reach this line together. Measured without it: 32 concurrent
        # checkpoints left 1, 1 and 3 surviving index entries across three trials -- and a lost entry
        # is not cosmetic, because the entry is the only record of where the `.bak` is. The glob
        # fallback in `list_checkpoints` fires only when the index is EMPTY, so a partially-written
        # index makes the missing files unreachable and the overwritten content unrestorable.
        #
        # The lock is per-index-path, so two projects do not contend and one project cannot interleave.
        with _index_lock(index_path):
            entries = []
            if index_path.exists():
                try:
                    entries = json.loads(index_path.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    entries = []
            if not isinstance(entries, list):
                entries = []
            entries.append(entry)
            # Written via a temporary file and moved into place: a crash mid-write must not leave a
            # truncated index, which would read as "no checkpoints" and strand every backup on disk.
            #
            # The temp name is UNIQUE per write. A single `.json.tmp` shared by every writer collided
            # on Windows even under the lock: one thread's `write_text` truncates the file while
            # another is mid-`replace`, and the loser gets `WinError 5` on a path it owns -- measured,
            # 1 of 24 concurrent checkpoints lost with "Access is denied" on the rename.
            temp = index_path.with_name(f"{index_path.name}.{uuid.uuid4().hex[:8]}.tmp")
            try:
                temp.write_text(json.dumps(entries, indent=2), encoding="utf-8", newline="\n")
                # Retried, because on Windows `os.replace` is not reliably atomic against a
                # concurrent reader: it can fail with WinError 5 "Access is denied" when another
                # handle is momentarily open on the destination -- a sync client, a virus scanner, or
                # another process reading the index. Measured at roughly one failure in a few hundred
                # concurrent writes, which is enough to lose a checkpoint in real use. Three attempts
                # with a short backoff turn that into a non-event; if all three fail the caller sees
                # the failure rather than a silently shorter index.
                last_error: OSError | None = None
                for attempt in range(3):
                    try:
                        temp.replace(index_path)
                        last_error = None
                        break
                    except OSError as exc:
                        last_error = exc
                        time.sleep(0.02 * (attempt + 1))
                if last_error is not None:
                    raise last_error
            finally:
                # A failed replace leaves the temp behind; a directory slowly filling with orphaned
                # index copies is a leak nobody would attribute to checkpoints.
                if temp.exists():
                    try:
                        temp.unlink()
                    except OSError:
                        pass
        return checkpoint_id
    except Exception:
        logger.exception("Checkpoint snapshot failed for %s", path)
        return None


def snapshot_and_write(path: Path, new_content: str, tool_name: str, ctx: ToolContext | None = None) -> None:
    record_checkpoint(
        path, path.read_text(encoding="utf-8", errors="replace") if path.exists() else "", tool_name, ctx=ctx
    )
    path.write_text(new_content, encoding="utf-8")


def list_checkpoints(limit: int | None = None, project_root: Path | None = None) -> list[dict]:
    index_path = _index_path(project_root)
    entries: list[dict] = []
    if index_path.exists():
        try:
            entries = json.loads(index_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            entries = []

    if not entries:
        # Index missing/corrupted -- fall back to globbing the backup files themselves so
        # checkpoints are still discoverable (tolerant-to-corruption, same precedent as
        # mcp_client.load_mcp_config).
        checkpoints_dir = _checkpoints_dir(project_root)
        if checkpoints_dir.is_dir():
            entries = [
                {
                    "id": bak.stem,
                    "timestamp": bak.stat().st_mtime,
                    "tool_name": "(unknown)",
                    "original_path": "(unknown)",
                    "backup_file": str(bak),
                }
                for bak in checkpoints_dir.glob("*.bak")
            ]

    entries.sort(key=lambda e: e.get("timestamp", 0), reverse=True)
    return entries[:limit] if limit else entries


def _find_checkpoint(checkpoint_id: str, project_root: Path | None = None) -> dict | None:
    for entry in list_checkpoints(project_root=project_root):
        if entry["id"] == checkpoint_id:
            return entry
    return None


def list_rewind_points(limit: int | None = None, project_root: Path | None = None) -> list[dict]:
    """Only checkpoints with a whole-project shadow-git snapshot -- distinct from
    list_checkpoints (every checkpoint, single-file .bak only), since not every checkpoint
    necessarily got a shadow commit (git missing, or it predates this feature)."""
    entries = [e for e in list_checkpoints(project_root=project_root) if e.get("shadow_commit")]
    return entries[:limit] if limit else entries


def rewind_to(checkpoint_id: str, project_root: Path | None = None) -> tuple[str, int | None]:
    """Reverts every tracked file in the project to the state captured at checkpoint_id, via the
    shadow git repo. Returns (message, message_count) -- message_count is the conversation length
    the caller (cli.py's /rewind) should truncate agent.messages to, or None if the rewind didn't
    happen (caller must leave the conversation untouched in that case). Deliberately does NOT
    delete files created after the checkpoint (only restores tracked file *content*) -- a
    conservative scope chosen specifically to avoid /rewind ever deleting untracked user work it
    has no record of."""
    root = project_root if project_root is not None else Path.cwd()
    entry = _find_checkpoint(checkpoint_id, project_root=root)
    if entry is None:
        return f"No checkpoint found with id {checkpoint_id!r}.", None

    shadow_commit = entry.get("shadow_commit")
    if shadow_commit is None:
        return (
            f"Checkpoint {checkpoint_id!r} has no whole-project snapshot (it predates /rewind, "
            "or git wasn't available when it was recorded) -- use /undo for a single-file "
            "restore instead.",
            None,
        )

    git_dir = _ensure_shadow_repo(root)
    if git_dir is None:
        return "Shadow git history isn't available right now (is git installed and on PATH?).", None

    result = _run_shadow_git(["checkout", shadow_commit, "--", "."], root, git_dir)
    if result.returncode != 0:
        return f"Rewind failed: {result.stderr.strip()}", None

    return (
        f"Restored project files to checkpoint {checkpoint_id} (shadow commit {shadow_commit}).",
        entry.get("message_count"),
    )


def execute_checkpoint_list(tool_input: dict, ctx: ToolContext) -> tuple[str, bool]:
    entries = list_checkpoints(limit=tool_input.get("limit"))
    if not entries:
        return "No checkpoints recorded yet.", False

    lines = [
        f"{e['id']}  {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(e['timestamp']))}  "
        f"{e['tool_name']}  {e['original_path']}"
        for e in entries
    ]
    return "\n".join(lines), False


def execute_checkpoint_restore(tool_input: dict, ctx: ToolContext) -> tuple[str, bool]:
    from .tools import confirm_action  # local import: tools.py imports checkpoints, avoid a cycle

    checkpoint_id = tool_input["id"]
    entry = _find_checkpoint(checkpoint_id)
    if entry is None:
        return f"No checkpoint found with id {checkpoint_id!r}.", True

    backup_file = Path(entry["backup_file"])
    if not backup_file.exists():
        return f"Checkpoint {checkpoint_id!r}'s backup file is missing on disk.", True

    original_path = Path(entry["original_path"])
    content = backup_file.read_text(encoding="utf-8", errors="replace")

    panel = Panel(
        f"Restore checkpoint {checkpoint_id} to [path]{escape(str(original_path))}[/]?",
        style="tool.confirm.question",
    )
    proceed, blocked = confirm_action(ctx.console, panel, ctx, "checkpoint_restore")
    if blocked:
        return blocked, False
    if not proceed:
        return "User declined to run this tool call.", False

    snapshot_and_write(original_path, content, "checkpoint_restore")
    return f"Restored {original_path} from checkpoint {checkpoint_id}.", False
