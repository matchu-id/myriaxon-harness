"""Git worktree isolation: give each concurrent agent its own working directory.

## The gap this closes, and why it is the most important one left

Myriaxon runs every agent in one working directory. A Locus waking while an interactive turn is in flight — or two of
the three permitted sub-agents — edit the same files, and the edits interleave. `checkpoints.py` gives *rollback*,
which is recovery: the damage happens first and you find out afterwards. Isolation is the only thing that prevents
it.

Opencode has this (`packages/opencode/src/worktree/index.ts`) and so does pi-harness
(`src/main/git/worktree-service.ts`). Myriaxon had nothing.

## The decision that shapes the whole module

**What counts as "dirty enough to refuse"?**

The obvious rule — refuse when the repository has uncommitted changes — is useless, and measurably so: this very
repository has **292 modified files** at the time of writing, which is the normal state of a working tree in active
use. A feature that refuses to run in its own author's repository is not a feature.

So the distinction is not clean-versus-dirty. It is **losing versus non-losing**:

  * `worktree add` **loses nothing.** It writes a new directory and a new branch ref. Uncommitted changes in the
    parent stay exactly where they are, because a worktree shares the object database and the index is per-worktree.
    So a dirty parent is **not** a reason to refuse.
  * `worktree remove` **loses everything uncommitted in that worktree.** So that is where the guard belongs, and it
    is the one operation that refuses on dirt.

Two further refusals, both because the failure would be silent rather than loud:

  * a path outside the project — an agent editing `/etc` or a sibling project is the worst outcome available, and
    it is one string comparison to prevent;
  * a duplicate name — `git worktree add` on an existing path fails with a message about the path, not about the
    name, so the caller learns nothing useful.

## What this deliberately does not do

It does not check out a clean copy of `HEAD` and pretend the uncommitted work exists there. A worktree is a
*checkout of a ref*: an agent starting in one sees committed state plus whatever it creates. That is the honest
semantics, and callers that need their working changes present must commit them first — which is why `create`
reports the parent's dirty count in its result rather than hiding it.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

# Where worktrees are created, relative to the project root. Inside the project so it travels with it, and prefixed
# with a locus so it is visually out of the way -- but NOT inside `.git`, which some tooling assumes is a repository.
WORKTREE_DIRNAME = ".myriaxon-worktrees"

# A name becomes a directory and a git ref, so it is sanitised rather than trusted. Same reasoning as a session id:
# the value can arrive from a model, and `../` in it would place the worktree outside the project.
NAME_SAFE = re.compile(r"[^A-Za-z0-9._-]+")
MAX_NAME = 60

# `git` is required. Reported as unavailable rather than assumed present, because the whole module is a no-op
# without it and a caller needs to be able to say so.
GIT_TIMEOUT_SECONDS = 120.0


@dataclass
class DirtyState:
    """How much uncommitted work a tree holds, and whether removing it would lose any."""

    modified: int = 0
    untracked: int = 0
    error: str = ""

    @property
    def has_losable_work(self) -> bool:
        """Whether removal would destroy something.

        Untracked files count. A worktree's whole purpose is to be a scratch space, so the common case is that an
        agent wrote a new file and never committed it -- which is exactly what `git worktree remove` deletes.
        """
        return self.modified > 0 or self.untracked > 0

    def render(self) -> str:
        if self.error:
            return f"could not be examined ({self.error})"
        return f"{self.modified} modified, {self.untracked} untracked"


@dataclass
class Worktree:
    """One isolated working directory."""

    name: str
    path: Path
    branch: str = ""
    commit: str = ""
    created_at: float = 0.0

    def render(self) -> str:
        return f"{self.name}: {self.path}" + (f" (branch {self.branch})" if self.branch else "")


@dataclass
class Result:
    """The outcome of an operation. `ok` False always carries a reason."""

    ok: bool
    message: str = ""
    worktree: Worktree | None = None
    details: list[str] = field(default_factory=list)

    def render(self) -> str:
        lines = [self.message]
        lines.extend(f"  {d}" for d in self.details)
        return "\n".join(lines)


def git_available() -> bool:
    return shutil.which("git") is not None


def _git(args: list[str], *, cwd: Path | None = None) -> tuple[int, str, str]:
    """Run git. Returns `(code, stdout, stderr)`. Never raises."""
    try:
        done = subprocess.run(
            ["git", *args],
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return 124, "", f"git {' '.join(args)} did not finish within {GIT_TIMEOUT_SECONDS:.0f}s"
    except OSError as exc:
        return 127, "", str(exc)
    return done.returncode, done.stdout or "", done.stderr or ""


def is_git_repo(root: Path) -> bool:
    """Whether `root` is inside a git working tree.

    `rev-parse --is-inside-work-tree` rather than a `.git` existence check: a worktree's own `.git` is a FILE, not a
    directory, and testing for a directory would report a worktree as not being a repository.
    """
    code, out, _err = _git(["rev-parse", "--is-inside-work-tree"], cwd=root)
    return code == 0 and out.strip().lower() == "true"


def repo_root(root: Path) -> Path:
    """The top level of the working tree containing `root`, or `root` itself if there is none."""
    code, out, _err = _git(["rev-parse", "--show-toplevel"], cwd=root)
    if code == 0 and out.strip():
        return Path(out.strip())
    return root


def sanitise_name(name: str) -> str:
    """Turn a caller-supplied name into something safe to use as a directory and a ref.

    Returns "" when nothing usable survives, so the caller can refuse rather than invent a name -- an invented name
    is one the caller cannot find again.
    """
    cleaned = NAME_SAFE.sub("-", (name or "").strip()).strip("-.")
    cleaned = cleaned[:MAX_NAME]
    # A name of only loci or dashes is not a name.
    if not cleaned or set(cleaned) <= {".", "-", "_"}:
        return ""
    return cleaned


def worktrees_dir(root: Path) -> Path:
    return repo_root(root) / WORKTREE_DIRNAME


def path_for(name: str, root: Path) -> Path:
    return worktrees_dir(root) / sanitise_name(name)


def check_path_containment(path: Path, root: Path) -> tuple[bool, str]:
    """Whether `path` is inside `root`. The one refusal that prevents editing someone else's files.

    Resolved before comparing, so `..` and a symlink cannot smuggle a path out. Both sides are resolved, because a
    project reached through a symlinked home directory would otherwise never match.
    """
    try:
        resolved_path = path.resolve()
        resolved_root = root.resolve()
    except OSError as exc:
        return False, f"the path could not be resolved ({exc})"
    if resolved_path == resolved_root:
        return True, ""
    if resolved_root not in resolved_path.parents:
        return False, (
            f"{resolved_path} is outside the project directory {resolved_root}. An agent working outside its "
            "project is the worst outcome this feature could produce, so the path is refused."
        )
    return True, ""


def dirty_state(path: Path) -> DirtyState:
    """Count modified and untracked files under `path`."""
    code, out, err = _git(["status", "--porcelain"], cwd=path)
    if code != 0:
        return DirtyState(error=(err.strip() or f"git status exited {code}")[:160])
    modified = untracked = 0
    for line in out.splitlines():
        if not line.strip():
            continue
        if line.startswith("??"):
            untracked += 1
        else:
            modified += 1
    return DirtyState(modified=modified, untracked=untracked)


def list_worktrees(root: Path) -> list[Worktree]:
    """Every worktree git knows about, including the main one.

    Parsed from `git worktree list --porcelain`, which is a stable machine format, rather than from the directory
    listing: a directory that git does not consider a worktree is not one, and a worktree whose directory was
    deleted by hand still appears here (which is information the caller needs).
    """
    code, out, _err = _git(["worktree", "list", "--porcelain"], cwd=root)
    if code != 0:
        return []
    found: list[Worktree] = []
    current: dict[str, str] = {}
    for line in out.splitlines() + [""]:
        if not line.strip():
            if current.get("worktree"):
                path = Path(current["worktree"])
                found.append(Worktree(
                    name=path.name,
                    path=path,
                    branch=current.get("branch", "").replace("refs/heads/", ""),
                    commit=current.get("HEAD", "")[:12],
                ))
            current = {}
            continue
        if " " in line:
            key, _, value = line.partition(" ")
            current[key] = value
        else:
            current[line] = ""
    return found


def find_worktree(name: str, root: Path) -> Worktree | None:
    clean = sanitise_name(name)
    if not clean:
        return None
    for tree in list_worktrees(root):
        if tree.name == clean or tree.branch == clean:
            return tree
    return None


def create(name: str, root: Path | None = None, *, base: str = "HEAD") -> Result:
    """Create an isolated worktree on a new branch. Returns a refusal rather than raising.

    A **dirty parent is not a refusal.** `worktree add` writes a new directory and a new ref; uncommitted changes in
    the parent remain untouched because a worktree has its own index over a shared object database. Refusing on dirt
    would make this unusable in any repository being actively worked in -- measured at 292 modified files in this
    one. The parent's dirty count is reported instead, because a caller who expected their working changes to be
    present needs to know they are not.
    """
    root = Path.cwd() if root is None else root
    if not git_available():
        return Result(ok=False, message="git is not available on PATH, so worktrees cannot be used")
    if not is_git_repo(root):
        return Result(ok=False, message=(
            f"{root} is not inside a git working tree. Isolation needs version control: a worktree is a checkout "
            "of a ref, and without one there is nothing to check out."
        ))

    clean = sanitise_name(name)
    if not clean:
        return Result(ok=False, message=(
            f"{name!r} has no characters usable in a directory or branch name. Pass a name with letters, digits, "
            "loci, dashes or underscores."
        ))

    top = repo_root(root)
    target = path_for(clean, top)
    ok, why = check_path_containment(target, top)
    if not ok:
        return Result(ok=False, message=why)

    existing = find_worktree(clean, top)
    if existing is not None:
        return Result(ok=False, message=(
            f"a worktree named {clean!r} already exists at {existing.path}. Reuse it, or remove it first -- a "
            "second create would fail on the path, which says nothing about the name."
        ), worktree=existing)

    if target.exists():
        return Result(ok=False, message=(
            f"{target} already exists but is not a worktree git knows about. Refusing rather than writing into a "
            "directory that may hold someone's files."
        ))

    parent_dirty = dirty_state(top)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return Result(ok=False, message=f"could not create {target.parent}: {exc}")

    code, out, err = _git(["worktree", "add", "-b", clean, str(target), base], cwd=top)
    if code != 0:
        return Result(ok=False, message=(
            f"git worktree add failed: {(err.strip() or out.strip())[:300]}"
        ))

    commit_code, commit_out, _ = _git(["rev-parse", "HEAD"], cwd=target)
    tree = Worktree(
        name=clean, path=target, branch=clean,
        commit=commit_out.strip()[:12] if commit_code == 0 else "",
    )
    details = [f"parent tree: {parent_dirty.render()}"]
    if parent_dirty.has_losable_work:
        # Stated explicitly because it is the most likely misunderstanding: the parent's uncommitted work is NOT in
        # the new worktree. An agent that assumes otherwise will look for a file that is not there.
        details.append(
            "the parent's uncommitted changes are NOT present in this worktree -- a worktree checks out a ref. "
            "Commit them first if the agent needs them."
        )
    return Result(ok=True, message=f"created worktree {clean!r} at {target}", worktree=tree, details=details)


def remove(name: str, root: Path | None = None, *, force: bool = False) -> Result:
    """Remove a worktree. **Refuses when it holds uncommitted work** unless `force` is set.

    This is where the guard belongs. `worktree add` cannot lose anything; `worktree remove` deletes the directory,
    so every untracked file an agent created in it goes. A worktree's normal state is exactly that -- scratch work
    never committed -- so refusing by default is the difference between a feature and a data-loss bug.
    """
    root = Path.cwd() if root is None else root
    if not git_available():
        return Result(ok=False, message="git is not available on PATH")
    if not is_git_repo(root):
        return Result(ok=False, message=f"{root} is not inside a git working tree")

    top = repo_root(root)
    tree = find_worktree(name, top)
    if tree is None:
        return Result(ok=False, message=f"no worktree named {name!r}")

    ok, why = check_path_containment(tree.path, top)
    if not ok:
        return Result(ok=False, message=(
            f"refusing to remove {tree.path}: {why}"
        ))

    state = dirty_state(tree.path)
    if state.error:
        return Result(ok=False, message=(
            f"could not determine whether {tree.path} holds uncommitted work ({state.error}), so it was not "
            "removed. Removing on an unknown state is how work is lost."
        ))
    if state.has_losable_work and not force:
        return Result(ok=False, message=(
            f"worktree {tree.name!r} holds uncommitted work ({state.render()}). Removing it would delete that work "
            "permanently. Commit or copy it first, or pass force=True if it is genuinely disposable."
        ), worktree=tree)

    args = ["worktree", "remove", str(tree.path)]
    if force:
        args.append("--force")
    code, out, err = _git(args, cwd=top)
    if code != 0:
        return Result(ok=False, message=f"git worktree remove failed: {(err.strip() or out.strip())[:300]}")

    # The branch is deleted only when it is fully merged, so a branch holding unique commits is reported rather than
    # silently discarded. `-d` (not `-D`) is what enforces that.
    branch_code, _, branch_err = _git(["branch", "-d", tree.branch], cwd=top) if tree.branch else (0, "", "")
    details = []
    if tree.branch and branch_code != 0:
        kept_because = branch_err.strip()[:160] or "it holds commits not merged elsewhere"
        details.append(f"the branch {tree.branch!r} was kept: {kept_because}")
    return Result(ok=True, message=f"removed worktree {tree.name!r}", details=details)


def prune(root: Path | None = None) -> Result:
    """Drop bookkeeping for worktrees whose directories were deleted by hand."""
    root = Path.cwd() if root is None else root
    if not git_available():
        return Result(ok=False, message="git is not available on PATH")
    code, out, err = _git(["worktree", "prune", "-v"], cwd=root)
    if code != 0:
        return Result(ok=False, message=f"git worktree prune failed: {(err.strip() or out.strip())[:200]}")
    lines = [line for line in (out + err).splitlines() if line.strip()]
    return Result(ok=True, message="pruned stale worktree records", details=lines[:10])


def render_list(root: Path | None = None) -> str:
    """A human-readable listing, marking which trees hold uncommitted work."""
    root = Path.cwd() if root is None else root
    if not git_available():
        return "git is not available on PATH, so worktrees cannot be used."
    if not is_git_repo(root):
        return f"{root} is not inside a git working tree."
    trees = list_worktrees(root)
    if not trees:
        return "git reports no worktrees for this repository."
    main = repo_root(root)
    lines = []
    for tree in trees:
        marker = " (main)" if tree.path.resolve() == main.resolve() else ""
        state = dirty_state(tree.path)
        detail = "" if not state.has_losable_work else f"  [{state.render()}]"
        lines.append(f"{tree.name}{marker}: {tree.path}{detail}")
    return "\n".join(lines)


def create_for_agent(name: str, root: Path | None = None) -> Path | None:
    """Convenience for a caller that wants a path or nothing.

    Returns `None` on refusal rather than raising, because the callers are agent paths where a refusal has to become
    a message to the model, not a traceback.
    """
    result = create(name, root)
    return result.worktree.path if result.ok and result.worktree else None
