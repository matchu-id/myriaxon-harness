"""Prove the shadow-repo repair: a damaged repo is re-created instead of silently failing."""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

failures = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global failures
    if not ok:
        failures += 1
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}{('  -- ' + detail) if detail and not ok else ''}")


def main() -> int:
    from myriaxon import checkpoints

    print("shadow_git_available:", checkpoints.shadow_git_available())
    if not checkpoints.shadow_git_available():
        print("git unavailable; nothing to verify")
        return 0

    with tempfile.TemporaryDirectory(prefix="mx-shadow-") as tmp:
        project = Path(tmp) / "project"
        project.mkdir()
        (project / "hello.txt").write_text("hello\n", encoding="utf-8")

        print("\nfresh repository")
        git_dir = checkpoints._ensure_shadow_repo(project)
        check("a repo is created", git_dir is not None)
        check("it is usable", git_dir is not None and checkpoints._shadow_repo_is_usable(git_dir))

        print("\na commit lands")
        sha = checkpoints._shadow_commit(project, "first")
        check("a commit sha came back", bool(sha), f"got {sha!r}")

        print("\nsimulate the sync client: remove config and HEAD, keep objects/")
        for name in ("config", "HEAD"):
            target = git_dir / name
            if target.exists():
                target.unlink()
        check("it is now unusable", not checkpoints._shadow_repo_is_usable(git_dir))

        print("\nthe old behaviour would have returned the broken path and failed forever")
        print("the new behaviour re-initialises")
        repaired = checkpoints._ensure_shadow_repo(project)
        check("a usable repo is returned", repaired is not None and checkpoints._shadow_repo_is_usable(repaired))
        check("it is the same location", repaired == git_dir)

        print("\nand commits work again")
        sha2 = checkpoints._shadow_commit(project, "second")
        check("a commit sha came back after repair", bool(sha2), f"got {sha2!r}")

        result = subprocess.run(["git", f"--git-dir={repaired}", "log", "--oneline"],
                                capture_output=True, text=True, timeout=25)
        check("git can read the log", result.returncode == 0, result.stderr.strip()[:120])

        print("\na repo missing only HEAD is also repaired")
        (repaired / "HEAD").unlink()
        check("unusable with HEAD gone", not checkpoints._shadow_repo_is_usable(repaired))
        again = checkpoints._ensure_shadow_repo(project)
        check("repaired again", again is not None and checkpoints._shadow_repo_is_usable(again))

        print("\nan unrelated directory that is not a repo is not treated as one")
        bogus = Path(tmp) / "bogus" / ".git"
        bogus.mkdir(parents=True)
        check("a bare .git directory is not usable",
              not checkpoints._shadow_repo_is_usable(bogus))

    print()
    print("shadow repair verified" if not failures else f"{failures} check(s) FAILED")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
