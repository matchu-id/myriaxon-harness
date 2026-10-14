"""Verify the git view module: status parsing, diffs, and hunk parsing."""
import os
import shutil
import subprocess
import sys
import warnings
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
warnings.filterwarnings("ignore")

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from myriaxon import git_view

scratch = REPO / "bench-out" / "gitview"
shutil.rmtree(scratch, ignore_errors=True)
# A unique directory per run. A fixed name was stale on Windows: rmtree(ignore_errors=True) can leave a
# directory behind when a git child still holds a handle, and git then silently treats the leftover as
# part of the parent repository instead of initialising a new one -- which looked like a bug in the
# code under test.
scratch = scratch / f"run{os.getpid()}"
scratch.mkdir(parents=True, exist_ok=True)

# Created before the helper so that `cwd or repo` has something to fall back to.
repo = scratch / "repo"
repo.mkdir()


def git(*args, cwd=None):
    # cwd defaults to `repo`. Running these in `scratch` initialised the *parent* directory instead, so
    # no repository existed at `repo` and every later assertion failed for a fixture reason.
    result = subprocess.run(["git", *args], cwd=str(cwd or repo), capture_output=True, text=True)
    if result.returncode != 0:
        print(f"      [git {' '.join(args)}] rc={result.returncode} err={result.stderr.strip()[:140]!r}")
    return result


print("=== a directory that is not a repository ===")
plain = scratch / "plain"
plain.mkdir()
state = git_view.status(plain)
print("  status:", {k: v for k, v in state.items() if k != "files"})
print("  is_repository:", git_view.is_repository(plain))
print("  diff:", git_view.diff(plain)["error"])

print("\n=== a real repository ===")
for _args in (("init", "-q"), ("config", "user.email", "t@example.com"), ("config", "user.name", "Test")):
    _r = git(*_args)
    if _r.returncode != 0:
        print(f"    FAILED git {' '.join(_args)}: rc={_r.returncode} err={_r.stderr.strip()[:200]}")
print(f"    after init: .git exists={(repo / '.git').exists()} rc of rev-parse="
      f"{git('rev-parse', '--show-toplevel').stdout.strip()!r}")
(repo / "tracked.txt").write_text("line one\nline two\nline three\n", encoding="utf-8")
_r = git("add", "tracked.txt")
print(f"    git add rc={_r.returncode} err={_r.stderr.strip()[:150]!r}")
_r = git("commit", "-q", "-m", "initial")
print(f"    git commit rc={_r.returncode} err={_r.stderr.strip()[:150]!r}")

print("  clean:", git_view.summary(repo))
from myriaxon.git_view import _run_git, work_tree_root

_out, _err, _code = _run_git(["rev-parse", "--show-toplevel"], repo)
print(f"    diag: git stdout={_out.strip()!r} code={_code}")
print(f"    diag: work_tree_root={work_tree_root(repo)}")
print(f"    diag: repo.resolve()={repo.resolve()}")
print(f"    diag: .git exists={(repo.resolve() / '.git').exists()}")
import os as _os

try:
    print(f"    diag: samefile={_os.path.samefile(repo.resolve(), Path(_out.strip()).resolve())}")
except OSError as exc:
    print(f"    diag: samefile failed {exc}")

# Modify a tracked file, add a staged file, and create an untracked file.
(repo / "tracked.txt").write_text("line one\nCHANGED\nline three\n", encoding="utf-8")
(repo / "staged.txt").write_text("staged content\n", encoding="utf-8")
git("add", "staged.txt")
(repo / "untracked file with spaces.txt").write_text("untracked\n", encoding="utf-8")

state = git_view.status(repo)
print(f"  changed files: {len(state['files'])}")
for entry in state["files"]:
    print(f"    {entry['code']} {entry['path']!r} staged={entry['staged']} untracked={entry['untracked']}")

by_path = {entry["path"]: entry for entry in state["files"]}
assert "tracked.txt" in by_path, by_path
assert by_path["tracked.txt"]["label"] == "modified", by_path["tracked.txt"]
assert "staged.txt" in by_path and by_path["staged.txt"]["staged"] is True, by_path.get("staged.txt")
# The NUL-separated format exists precisely so a name with spaces survives intact.
assert "untracked file with spaces.txt" in by_path, list(by_path)
print("  status parsing: OK (spaces in filenames preserved)")

print("\n=== diff of a modified file ===")
result = git_view.diff(repo, "tracked.txt")
print(f"  truncated={result['truncated']} empty={result.get('empty')}")
print("  " + "\n  ".join(result["diff"].splitlines()[:8]))
hunks = git_view.parse_unified(result["diff"])
assert hunks, "no hunks parsed"
types = [line["type"] for hunk in hunks for line in hunk["lines"]]
print(f"  hunk lines: {types}")
assert "add" in types and "del" in types and "ctx" in types, types
added = [line for hunk in hunks for line in hunk["lines"] if line["type"] == "add"]
print(f"  added line text: {added[0]['text']!r}")
assert added[0]["text"] == "CHANGED", added
print("  diff + hunk parsing: OK")

print("\n=== an untracked file has no diff, so its content is shown ===")
untracked = git_view.diff(repo, "untracked file with spaces.txt")
print(f"  untracked flag: {untracked.get('untracked')}")
print("  " + "\n  ".join(untracked["diff"].splitlines()[:5]))
assert untracked.get("untracked") is True, untracked
assert "untracked" in untracked["diff"], untracked["diff"][:100]
assert "+untracked" in untracked["diff"], untracked["diff"]
print("  untracked content shown: OK")

print("\n=== an empty diff is reported as empty, not as an error ===")
empty = git_view.diff(repo)
print(f"  whole-tree diff: empty={empty.get('empty')} error={empty['error']!r} len={len(empty['diff'])}")
whole = git_view.diff(repo, "staged.txt", staged=True)
print(f"  staged diff for staged.txt: {len(whole['diff'])} bytes, empty={whole.get('empty')}")
print("  OK")

print("\n=== a path outside the repository is not readable through git ===")
outside = git_view.diff(repo, "../plain/nope.txt")
print(f"  error={outside['error']!r} diff len={len(outside['diff'])}")

print("\nALL GIT VIEW CHECKS PASSED")
shutil.rmtree(scratch, ignore_errors=True)
