"""Structured git access for the dashboard's file explorer and diff review.

The agent's `git_status`/`git_diff` tools return text for a model to read. A UI needs the opposite:
a list it can render and click. This module wraps the same commands and returns parsed data.

Nothing here writes to the repository. Every git call is read-only -- `status`, `diff`, `ls-files` --
so opening the diff panel can never change the working tree, which matters because this runs while an
agent may be mid-turn.

Git being absent, or the directory not being a repository, is reported as a normal state rather than an
error: plenty of workspaces are not repositories, and the explorer should still browse them.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

# A diff larger than this is truncated for display. The whole diff of a large change can be megabytes,
# and sending that to a browser to render a few visible lines is wasteful.
MAX_DIFF_BYTES = 300_000
GIT_TIMEOUT = 30

# Porcelain v1 status codes, mapped to what the UI shows. The first column is the staged state, the
# second the unstaged one; a UI that shows only "modified" hides the difference between "changed but
# not staged" and "staged and ready to commit".
_STATUS_LABELS = {
    "M": "modified",
    "A": "added",
    "D": "deleted",
    "R": "renamed",
    "C": "copied",
    "U": "conflicted",
    "?": "untracked",
    "!": "ignored",
    "T": "type changed",
}


def _run_git(args: list[str], cwd: Path) -> tuple[str, str, int]:
    """Run a read-only git command. Returns (stdout, stderr, returncode)."""
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=GIT_TIMEOUT,
        )
    except FileNotFoundError:
        return "", "git is not installed or not on PATH", 127
    except subprocess.TimeoutExpired:
        return "", f"git {args[0]} timed out after {GIT_TIMEOUT}s", 124
    except OSError as exc:
        return "", f"{type(exc).__name__}: {exc}", 1
    return completed.stdout or "", completed.stderr or "", completed.returncode


def work_tree_root(root: Path) -> Path | None:
    """The root of the work tree that `root` itself belongs to, or None.

    Returns None when `root` is merely *inside* a repository without being one -- a scratch directory
    created under a repo is not itself a repository, and treating it as one reported the parent repo's
    hundreds of changes as that directory's.
    """
    out, _, code = _run_git(["rev-parse", "--show-toplevel"], root)
    if code != 0 or not out.strip():
        return None
    try:
        toplevel = Path(out.strip()).resolve()
        here = Path(root).resolve()
    except OSError:
        return None
    # samefile, not a string comparison. Measured: on Windows `git rev-parse --show-toplevel` prints
    # forward slashes ("C:/Users/.../r") while Path.resolve() yields backslashes, so character
    # comparison failed for a genuine repository. samefile asks the filesystem whether the two paths
    # name the same directory, which is the real question and is immune to separators, casing, and
    # short-vs-long path forms.
    try:
        if os.path.samefile(here, toplevel):
            return here
    except OSError:
        if os.path.normcase(str(here)) == os.path.normcase(str(toplevel)):
            return here

    # A repository nested inside another one has its own .git, and git reports the outer repository as
    # the toplevel. Checking for .git is what makes a nested repository browsable in its own right.
    if (here / ".git").exists():
        return here
    return None


def is_repository(root: Path) -> bool:
    return work_tree_root(root) is not None


def find_repository(root: Path) -> Path | None:
    """The nearest repository at or above `root`, mirroring how git itself searches upward.

    Needed because the dashboard's workspace is often a subdirectory of the repository -- verified: the
    check runner's workspace was `<repo>/bench-out/checks`, and asking git from there reported the
    repository's changes with paths relative to the repository root, so a Source-control view that only
    looked at the workspace found nothing to show. Git climbs to the repository for you; this does the
    same.
    """
    try:
        current = Path(root).resolve()
    except OSError:
        return None
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def display_root(root: Path) -> Path:
    """Where the explorer should look for changes: the repository containing `root`, else `root`."""
    own = work_tree_root(root)
    if own is not None:
        return own
    return find_repository(root) or Path(root)


def repo_root(root: Path) -> Path:
    """The repository root containing `root`, or `root` itself when there is none."""
    return work_tree_root(root) or Path(root)


def current_branch(root: Path) -> str:
    out, _, code = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], root)
    if code == 0 and out.strip():
        return out.strip()
    return ""


def _relative_prefix(root: Path, toplevel: Path) -> str:
    """The prefix git prepends to paths, relative to the directory being explored.

    `git status` run from a subdirectory of a repository reports paths relative to the *repository
    root*, not to that subdirectory. Exploring the repository root but receiving paths already
    prefixed with the directory name meant every path was wrong, so a clickable entry pointed at a
    file that did not exist.
    """
    try:
        return str(Path(root).resolve().relative_to(toplevel)).replace("\\", "/")
    except (OSError, ValueError):
        return ""


def status(root: Path, repo: Path | None = None) -> dict:
    """Changed files as structured entries.

    Parsed from `--porcelain=v1 -z`, which is NUL-separated and does not quote or escape paths, so a
    filename containing a space or a newline survives intact. The line-based format does quote such
    names, and a naive split on whitespace then reports the wrong path.

    `repo` lets a caller query at the repository root while reporting paths relative to `root`; the
    dashboard passes its workspace so paths line up with what the explorer lists.
    """
    toplevel = work_tree_root(repo) if repo is not None else work_tree_root(root)
    if toplevel is None:
        return {"is_repo": False, "branch": "", "files": [], "clean": True, "error": ""}
    query_at = repo if repo is not None else root

    # -z with --branch puts the branch header first as its own NUL-terminated field.
    out, err, code = _run_git(["status", "--porcelain=v1", "-z", "--branch", "--untracked-files=all"], query_at)
    if code != 0:
        return {"is_repo": True, "branch": "", "files": [], "clean": False,
                "error": err.strip() or f"git status failed ({code})"}

    prefix = _relative_prefix(root, toplevel)
    fields = out.split("\0")
    branch = ""
    entries: list[dict] = []
    index = 0
    while index < len(fields):
        field = fields[index]
        index += 1
        if not field:
            continue
        if field.startswith("## "):
            header = field[3:]
            branch = header.split("...")[0].strip() or header.strip()
            continue
        if len(field) < 4:
            continue
        staged_code, unstaged_code = field[0], field[1]
        path = field[3:]
        original = ""
        # A rename/copy carries the source path as the NEXT NUL field.
        if staged_code in ("R", "C") or unstaged_code in ("R", "C"):
            if index < len(fields):
                original = fields[index]
                index += 1

        staged = staged_code not in (" ", "?")
        unstaged = unstaged_code not in (" ", "?")
        code = staged_code if staged else unstaged_code
        entries.append(
            {
                "path": _strip_prefix(path, prefix),
                "original": _strip_prefix(original, prefix) if original else "",
                "code": code,
                "staged_code": staged_code,
                "unstaged_code": unstaged_code,
                "label": _STATUS_LABELS.get(code, "changed"),
                "staged": staged,
                "unstaged": unstaged,
                "untracked": staged_code == "?" or unstaged_code == "?",
            }
        )

    return {
        "is_repo": True,
        "branch": branch,
        "root": str(toplevel),
        "files": entries,
        "clean": not entries,
        "error": "",
    }


def _strip_prefix(path: str, prefix: str) -> str:
    """Remove the repository-relative prefix, leaving a path relative to the explored directory."""
    if not prefix:
        return path
    normalised = path.replace("\\", "/")
    if normalised == prefix:
        return "."
    if normalised.startswith(prefix + "/"):
        return normalised[len(prefix) + 1:]
    return path


def diff(root: Path, path: str = "", staged: bool = False, context: int = 3, repo: Path | None = None) -> dict:
    """A unified diff for one path, or the whole working tree when `path` is empty."""
    query_at = repo if repo is not None else root
    toplevel = work_tree_root(query_at)
    if toplevel is None:
        return {"diff": "", "is_repo": False, "error": "not a git repository", "path": path}

    # A path typed into the UI is relative to the directory being explored, but git resolves a pathspec
    # against the repository root, so the repository-relative prefix has to be restored or the diff
    # silently comes back empty.
    prefix = _relative_prefix(root, toplevel)
    pathspec = f"{prefix}/{path}" if (path and prefix) else path

    args = ["diff", "--no-color", f"--unified={context}"]
    if staged:
        args.append("--cached")
    if pathspec:
        args += ["--", pathspec]

    out, err, code = _run_git(args, query_at)
    if code != 0:
        return {"diff": "", "is_repo": True, "error": err.strip() or f"git diff failed ({code})", "path": path}

    # An *untracked* file has no diff at all: git does not track it yet, so `git diff` prints nothing.
    # Showing "no changes" for a file the explorer just listed as untracked is confusing, so its content
    # is shown as an all-added diff instead. The untracked check is essential: a *tracked* file with a
    # legitimately empty diff (staged, with no further edits) took this path too and was displayed as if
    # its entire contents were newly added -- verified failure.
    if not out.strip() and path and not staged and _is_untracked(query_at, pathspec):
        target = Path(root) / path
        if target.is_file():
            try:
                text = target.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                return {"diff": "", "is_repo": True, "error": f"{type(exc).__name__}: {exc}", "path": path}
            lines = text.splitlines()
            body = "\n".join(f"+{line}" for line in lines[:4000])
            header = f"--- /dev/null\n+++ b/{path}\n@@ untracked file, {len(lines)} line(s) @@"
            return {
                "diff": f"{header}\n{body}",
                "is_repo": True,
                "error": "",
                "path": path,
                "untracked": True,
                "truncated": len(lines) > 4000,
            }

    truncated = False
    if len(out.encode("utf-8", errors="replace")) > MAX_DIFF_BYTES:
        out = out.encode("utf-8", errors="replace")[:MAX_DIFF_BYTES].decode("utf-8", errors="ignore")
        truncated = True

    return {
        "diff": out,
        "is_repo": True,
        "error": "",
        "path": path,
        "staged": staged,
        "truncated": truncated,
        "empty": not out.strip(),
    }


def _is_untracked(query_at: Path, pathspec: str) -> bool:
    """Whether `pathspec` names a file git does not track.

    `git ls-files --error-unmatch` exits non-zero for a path git knows nothing about, which is exactly
    the untracked case. Checking this before substituting a file's contents for an empty diff keeps a
    tracked file's genuinely-empty diff empty.
    """
    out, _, code = _run_git(["ls-files", "--error-unmatch", "--", pathspec], query_at)
    return code != 0


def parse_unified(diff_text: str) -> list[dict]:
    """Turn a unified diff into hunks of typed lines, for the UI to colour.

    Returned as data rather than HTML so the client does its own escaping: a diff contains arbitrary
    file content, and building markup from it server-side is how a repository file becomes a
    cross-site scripting vector.
    """
    hunks: list[dict] = []
    current: dict | None = None
    old_line = new_line = 0

    for raw in diff_text.splitlines():
        if raw.startswith("@@ "):
            # @@ -a,b +c,d @@ optional heading
            header = raw
            try:
                body = raw.split("@@")[1].strip()
                old_part, new_part = body.split(" ")[:2]
                old_line = int(old_part.lstrip("-").split(",")[0])
                new_line = int(new_part.lstrip("+").split(",")[0])
            except (IndexError, ValueError):
                old_line = new_line = 0
            current = {"header": header, "lines": []}
            hunks.append(current)
            continue
        if raw.startswith("---") or raw.startswith("+++") or raw.startswith("diff ") or raw.startswith("index "):
            current = None
            continue
        if current is None:
            current = {"header": "", "lines": []}
            hunks.append(current)
        if raw.startswith("+"):
            current["lines"].append({"type": "add", "text": raw[1:], "new": new_line})
            new_line += 1
        elif raw.startswith("-"):
            current["lines"].append({"type": "del", "text": raw[1:], "old": old_line})
            old_line += 1
        elif raw.startswith("\\"):
            current["lines"].append({"type": "meta", "text": raw})
        else:
            current["lines"].append(
                {"type": "ctx", "text": raw[1:] if raw.startswith(" ") else raw, "old": old_line, "new": new_line}
            )
            old_line += 1
            new_line += 1
    return hunks


def summary(root: Path) -> dict:
    """Counts for the panel header."""
    state = status(root)
    if not state.get("is_repo"):
        return state
    staged = sum(1 for f in state["files"] if f["staged"])
    unstaged = sum(1 for f in state["files"] if f["unstaged"])
    untracked = sum(1 for f in state["files"] if f["untracked"])
    return {
        "is_repo": True,
        "branch": state["branch"],
        "total": len(state["files"]),
        "staged": staged,
        "unstaged": unstaged,
        "untracked": untracked,
        "clean": state["clean"],
        "error": state.get("error", ""),
    }
