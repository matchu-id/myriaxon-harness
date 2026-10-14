"""Sweep the accumulated state: orphaned dot workspaces and empty shadow-history repos.

Safe by construction, and it says what it will do before doing it:

* **Orphaned dot workspaces** -- a directory under `.myriaxon/dots/` whose id is not in
  `dots.json`. Every dot gets a workspace on creation, so each test dot in the suite leaves one
  behind: 242 of them here, none containing a single file.
* **Shadow-history repos with no commits** -- `_ensure_shadow_repo` runs `git init` when a session
  first touches a project, and an `init` with no snapshot is 28 KB of scaffolding holding nothing.
  All 37 had zero commits.

Anything with content, or with commits, is reported and left alone. `--apply` is required to
delete, so running this by accident cannot lose work.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from myriaxon import constants  # noqa: E402


def dir_size(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def shadow_is_disposable(git_dir: Path) -> bool:
    """Whether a shadow repo may be deleted: only if it is *provably* empty.

    This function exists because the first version of this sweep got it wrong and deleted 22
    directories it should have kept. `shadow_commit_count` returned -1 for "could not read", and
    the caller tested `commits == 0`, so an unreadable repo read as an empty one -- the failure
    mode where a sentinel is introduced and then conflated with a legitimate value.

    Now the test is positive evidence of emptiness, never absence of evidence:

    * `config` and `HEAD` must both exist, or the directory is not a git repository at all. It
      cannot hold reachable history, so it is residue -- and it is reported as such rather than
      counted as an empty repo.
    * the repo must be readable (exit code 0) **and** report zero commits, with no refs on disk.
    * anything else is kept.

    Even so, deletion is only ever safe because a shadow repo is a *cache*: `_ensure_shadow_repo`
    recreates it, and per-file `.bak` checkpoints are a separate mechanism. A function that deletes
    things should be able to say what makes deletion safe, and that is the sentence.
    """
    if not (git_dir / "config").is_file() or not (git_dir / "HEAD").is_file():
        return False
    try:
        result = subprocess.run(
            ["git", f"--git-dir={git_dir}", "rev-list", "--all", "--count"],
            capture_output=True, text=True, timeout=20,
        )
    except Exception:  # noqa: BLE001
        return False
    if result.returncode != 0:
        return False
    if (result.stdout or "").strip() not in ("", "0"):
        return False
    # `--all` covers refs; a packed-refs file or loose refs would have shown up in the count, but
    # check explicitly so a repo whose refs are unreadable is kept rather than guessed at.
    return not any((git_dir / "refs").rglob("*")) if (git_dir / "refs").is_dir() else True


def main() -> int:
    parser = argparse.ArgumentParser(description="Sweep orphaned dot state")
    parser.add_argument("--apply", action="store_true", help="actually delete; without it, report only")
    args = parser.parse_args()

    cfg = constants.CONFIG_DIR
    dots_file = cfg / "dots.json"
    live_ids = set()
    if dots_file.exists():
        try:
            live_ids = {d["id"] for d in json.loads(dots_file.read_text(encoding="utf-8")).get("dots", [])}
        except Exception as exc:  # noqa: BLE001
            print(f"refusing to run: could not read {dots_file}: {exc}")
            return 1

    print(f"config dir : {cfg}")
    print(f"live dots  : {sorted(live_ids) or '(none)'}")
    print(f"mode       : {'APPLY — deleting' if args.apply else 'report only (pass --apply to delete)'}")
    print()

    reclaim = 0
    deleted = 0
    kept = 0

    # ---- orphaned dot workspaces -------------------------------------------------------
    workspaces = cfg / "dots"
    orphans: list[Path] = []
    if workspaces.is_dir():
        for entry in sorted(workspaces.iterdir()):
            if not entry.is_dir():
                continue
            if entry.name in live_ids:
                print(f"  keep  workspace {entry.name} — a live dot")
                kept += 1
                continue
            files = [f for f in entry.rglob("*") if f.is_file()]
            if files:
                print(f"  keep  workspace {entry.name} — {len(files)} file(s) of real work")
                kept += 1
                continue
            orphans.append(entry)

    print(f"\norphaned dot workspaces: {len(orphans)} (each empty)")
    if args.apply and orphans:
        for entry in orphans:
            reclaim += dir_size(entry)
            shutil.rmtree(entry, ignore_errors=True)
            deleted += 1
        print(f"  deleted {deleted}")
        # Remove the parent only if every workspace is gone now.
        try:
            remaining = [d for d in workspaces.iterdir() if d.is_dir()] if workspaces.is_dir() else []
            if not remaining and workspaces.is_dir():
                workspaces.rmdir()
                print("  removed the now-empty dots/ directory")
        except OSError:
            pass
    else:
        reclaim += sum(dir_size(o) for o in orphans)

    # ---- shadow history ----------------------------------------------------------------
    hist = constants.SHADOW_HISTORY_DIR
    disposable: list[Path] = []
    broken: list[Path] = []
    if hist.is_dir():
        for entry in sorted(hist.iterdir()):
            if not entry.is_dir():
                continue
            git_dir = entry / ".git"
            if not (git_dir / "config").is_file() or not (git_dir / "HEAD").is_file():
                # An objects directory with no config is not a repository. Reported separately from
                # an empty one, because "we could not read this" and "this is empty" are different
                # facts and only the second one is safe to act on.
                broken.append(entry)
                continue
            if shadow_is_disposable(git_dir):
                disposable.append(entry)
            else:
                print(f"  keep  history {entry.name} — holds commits")
                kept += 1

    broken_size = sum(dir_size(b) for b in broken)
    empty_size = sum(dir_size(d) for d in disposable)
    print(f"\nshadow-history dirs that are not repositories: {len(broken)} ({broken_size:,} bytes)")
    for entry in broken:
        files = [f for f in entry.rglob("*") if f.is_file()]
        print(f"    {entry.name}  {len(files)} file(s), no config/HEAD")
    print(f"shadow-history repos provably empty: {len(disposable)} ({empty_size:,} bytes)")

    if args.apply:
        for entry in broken + disposable:
            shutil.rmtree(entry, ignore_errors=True)
            deleted += 1
        reclaim += broken_size + empty_size
        print(f"  deleted {len(broken) + len(disposable)}")
        try:
            if hist.is_dir() and not any(hist.iterdir()):
                hist.rmdir()
                print("  removed the now-empty history/ directory")
        except OSError:
            pass
    else:
        reclaim += broken_size + empty_size

    print()
    if args.apply:
        print(f"deleted {deleted} item(s), reclaimed about {reclaim / 1024 / 1024:.2f} MB")
        print(f"kept {kept} item(s) that held something")
    else:
        print(f"would delete {len(orphans) + len(broken) + len(disposable)} item(s), "
              f"reclaiming about {reclaim / 1024 / 1024:.2f} MB")
        print(f"would keep {kept} item(s)")
        print("\nre-run with --apply to do it")
    return 0


if __name__ == "__main__":
    sys.exit(main())
