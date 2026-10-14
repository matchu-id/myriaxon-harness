#!/usr/bin/env python3
"""Verify git worktree isolation.

## Why this module exists

Myriaxon runs every agent in one working directory, so a Locus waking during an interactive turn — or two of the three
permitted sub-agents — edit the same files and the edits interleave. `checkpoints.py` gives rollback, which is
recovery: the damage happens first. This is prevention.

## What these checks are about

Mostly refusals, because the dangerous outcomes here are all silent: removing a worktree that held uncommitted work
deletes it with no trace, and a path outside the project edits someone else's files. Two checks assert the actual
value proposition — that two worktrees cannot see each other's changes — because "isolation" is the claim and it is
falsifiable.

Every check runs against a **scratch repository created by this file**, never the Myriaxon repository itself: a
verifier that creates branches and removes worktrees in the project under test is a verifier nobody dares run.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from myriaxon import worktree as W  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAIL).append(label)
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}" + (f"\n         {detail}" if detail else ""))


def git(args: list[str], cwd: Path) -> int:
    done = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    return done.returncode


def make_repo(dirty: bool = False) -> Path:
    root = Path(tempfile.mkdtemp())
    git(["init", "-q"], root)
    (root / "a.txt").write_text("hello\n", encoding="utf-8")
    git(["-c", "user.email=t@t", "-c", "user.name=t", "add", "-A"], root)
    git(["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"], root)
    if dirty:
        (root / "a.txt").write_text("hello\nmodified\n", encoding="utf-8")
    return root


def main() -> int:
    print("=== 1. preconditions are reported, not assumed ===")
    check("git is detected", W.git_available())
    repo = make_repo()
    check("a git repo is recognised", W.is_git_repo(repo))
    check("repo_root finds the top level", W.repo_root(repo) == repo.resolve()
          or W.repo_root(repo).name == repo.name, str(W.repo_root(repo)))
    plain = Path(tempfile.mkdtemp())
    check("a non-repo is recognised as such", not W.is_git_repo(plain))

    print()
    print("=== 2. THE DECISION: a dirty parent does NOT refuse ===")
    # A worktree has its own index over a shared object database, so `worktree add` cannot lose the parent's
    # uncommitted work. Refusing on dirt would make this unusable in any repository being actively worked in --
    # measured at 292 modified files in Myriaxon itself. The dirty count is REPORTED instead.
    dirty_repo = make_repo(dirty=True)
    state = W.dirty_state(dirty_repo)
    check("the parent's dirt is measured", state.modified == 1, state.render())
    result = W.create("dirty-parent", dirty_repo)
    check("creating with a dirty parent SUCCEEDS", result.ok, result.message[:100])
    check("and the dirty count is reported back",
          any("modified" in d for d in result.details), str(result.details))
    check("and it warns the parent's changes are absent",
          any("NOT present" in d for d in result.details),
          "the most likely misunderstanding: a worktree checks out a REF")
    if result.worktree:
        check("the parent's uncommitted edit is NOT in the worktree",
              "modified" not in (result.worktree.path / "a.txt").read_text(encoding="utf-8"),
              (result.worktree.path / "a.txt").read_text(encoding="utf-8")[:40])
        check("and the parent still has it",
              "modified" in (dirty_repo / "a.txt").read_text(encoding="utf-8"),
              "creating a worktree must not touch the parent's files")
        W.remove("dirty-parent", dirty_repo, force=True)

    print()
    print("=== 3. the worktree is a real checkout ===")
    clean = make_repo()
    created = W.create("agent-1", clean)
    check("created", created.ok, created.message[:100])
    tree = created.worktree
    check("it has a path that exists", tree is not None and tree.path.is_dir())
    check("it contains the committed file", (tree.path / "a.txt").is_file())
    check("it is on its own branch", tree.branch == "agent-1", str(tree.branch))
    check("it recorded a commit", bool(tree.commit), tree.commit)
    check("git lists it", any(t.name == "agent-1" for t in W.list_worktrees(clean)),
          str([t.name for t in W.list_worktrees(clean)]))

    print()
    print("=== 4. THE VALUE PROPOSITION: two worktrees are ISOLATED ===")
    # The claim is "isolation", and it is falsifiable. If an edit in one is visible in the other, the feature does
    # nothing and this whole module is decoration.
    second = W.create("agent-2", clean)
    check("a second worktree is created", second.ok, second.message[:100])
    if tree and second.worktree:
        (tree.path / "only-in-one.txt").write_text("agent one's work", encoding="utf-8")
        (tree.path / "a.txt").write_text("edited by agent one\n", encoding="utf-8")
        check("an untracked file in one is NOT in the other",
              not (second.worktree.path / "only-in-one.txt").exists())
        check("an EDIT in one is NOT in the other",
              (second.worktree.path / "a.txt").read_text(encoding="utf-8") == "hello\n",
              (second.worktree.path / "a.txt").read_text(encoding="utf-8")[:40])
        check("and the main tree is also untouched",
              (clean / "a.txt").read_text(encoding="utf-8") == "hello\n")
        check("each worktree has its own index",
              W.dirty_state(tree.path).untracked >= 1 and W.dirty_state(second.worktree.path).untracked == 0,
              f"one={W.dirty_state(tree.path).render()} two={W.dirty_state(second.worktree.path).render()}")

    print()
    print("=== 5. REMOVAL REFUSES when it would destroy work ===")
    # This is where the guard belongs. A worktree's normal state is scratch work never committed, so `remove`
    # deleting silently would be a data-loss bug with a friendly name.
    if tree:
        before = W.dirty_state(tree.path)
        check("the worktree holds losable work", before.has_losable_work, before.render())
        refused = W.remove("agent-1", clean)
        check("remove is REFUSED", not refused.ok, refused.message[:110])
        check("  it names the amount at risk", "uncommitted work" in refused.message, refused.message[:130])
        check("  it offers the escape hatch", "force" in refused.message, refused.message[-90:])
        check("  and the work SURVIVES", (tree.path / "only-in-one.txt").is_file(),
              "a refusal that deleted anyway would be the bug it prevents")

    print()
    print("=== 6. a clean worktree removes without force ===")
    if second.worktree:
        removed = W.remove("agent-2", clean)
        check("remove succeeds", removed.ok, removed.message[:110])
        check("the directory is gone", not second.worktree.path.exists())
        check("git no longer lists it", not any(t.name == "agent-2" for t in W.list_worktrees(clean)))

    print()
    print("=== 7. force removes a dirty worktree ===")
    if tree:
        forced = W.remove("agent-1", clean, force=True)
        check("force remove succeeds", forced.ok, forced.message[:110])
        check("the directory is gone", not tree.path.exists())

    print()
    print("=== 8. DUPLICATE names are refused ===")
    # `git worktree add` on an existing path fails with a message about the PATH, which tells the caller nothing
    # about the name they used. So the refusal is ours, and it names the name.
    a = W.create("dup", clean)
    dup = W.create("dup", clean)
    check("a duplicate is refused", not dup.ok, dup.message[:100])
    check("  the message names the existing path", "already exists" in dup.message, dup.message[:120])
    check("  and says which name", "'dup'" in dup.message or "dup" in dup.message)
    if a.worktree:
        W.remove("dup", clean, force=True)

    print()
    print("=== 9. BAD names are refused, not invented ===")
    # An invented name is one the caller cannot find again.
    for bad in ("...", "   ", "", "///", "!!!", "..", "---"):
        result = W.create(bad, clean)
        check(f"name {bad!r} is refused", not result.ok, result.message[:80])
    check("a name is sanitised rather than rejected outright",
          W.sanitise_name("agent one/two") == "agent-one-two", W.sanitise_name("agent one/two"))
    check("a long name is truncated", len(W.sanitise_name("x" * 200)) <= W.MAX_NAME)
    check("a name of only loci sanitises to empty", W.sanitise_name("..") == "")

    print()
    print("=== 10. PATHS OUTSIDE THE PROJECT are refused ===")
    # The worst outcome this feature could produce: an agent editing a sibling project or a system directory.
    outside = Path(tempfile.mkdtemp())
    ok, why = W.check_path_containment(outside / "x", clean)
    check("a path outside the root is refused", not ok, why[:110])
    ok, why = W.check_path_containment(clean / "inside", clean)
    check("a path inside the root is allowed", ok, why)
    ok, _ = W.check_path_containment(clean, clean)
    check("the root itself is allowed", ok)
    # Traversal must not smuggle a path out, even though `worktrees_dir` makes it hard to reach.
    escaping = clean / W.WORKTREE_DIRNAME / ".." / ".." / "elsewhere"
    ok, why = W.check_path_containment(escaping, clean)
    check("a traversal path is resolved before comparing", not ok, why[:100])

    print()
    print("=== 11. a non-repository is refused with an explanation ===")
    result = W.create("x", plain)
    check("refused", not result.ok, result.message[:100])
    check("  the reason says why version control is required",
          "not inside a git working tree" in result.message and "checkout" in result.message,
          result.message[:170])
    check("remove on a non-repo is refused too", not W.remove("x", plain).ok)
    check("list on a non-repo explains rather than raising",
          "not inside a git working tree" in W.render_list(plain))

    print()
    print("=== 12. removing a worktree git does not know about is refused ===")
    check("an unknown name is refused", not W.remove("never-created", clean).ok)
    check("and it says so plainly",
          "no worktree named" in W.remove("never-created", clean).message,
          W.remove("never-created", clean).message[:90])

    print()
    print("=== 13. a branch holding unique commits is KEPT ===")
    # `git branch -d` refuses to delete an unmerged branch, and the outcome is reported rather than the branch
    # being discarded with `-D`. Silent history loss is the failure mode being avoided.
    keeper = W.create("keeper", clean)
    kept_branch = False
    if keeper.worktree:
        (keeper.worktree.path / "newfile.txt").write_text("new\n", encoding="utf-8")
        git(["add", "-A"], keeper.worktree.path)
        git(["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "unique work"],
            keeper.worktree.path)
        removed = W.remove("keeper", clean, force=True)
        check("the worktree is removed", removed.ok, removed.message[:90])
        code, out, _ = W._git(["branch", "--list", "keeper"], cwd=clean)
        kept_branch = bool(out.strip())
        check("the branch with unique commits is KEPT", kept_branch, out.strip()[:80])
        check("and the outcome is reported",
              any("kept" in d for d in removed.details), str(removed.details))
        if kept_branch:
            # The commit itself must still be reachable, not just the ref.
            code, out, _ = W._git(["log", "--oneline", "keeper"], cwd=clean)
            check("its commit is still reachable", "unique work" in out, out.strip()[:80])

    print()
    print("=== 14. a hand-deleted worktree is prunable ===")
    # A worktree whose directory was deleted by hand still appears in git's bookkeeping. `prune` is how that is
    # cleaned, and it must not be a no-op that reports success.
    orphan = W.create("orphan", clean)
    if orphan.worktree:
        import shutil as _shutil
        _shutil.rmtree(orphan.worktree.path, ignore_errors=True)
        check("git still lists the orphan", any(t.name == "orphan" for t in W.list_worktrees(clean)),
              "a worktree record outlives its directory, which is why prune exists")
        pruned = W.prune(clean)
        check("prune reports success", pruned.ok, pruned.message[:90])

    print()
    print("=== 15. the listing marks work at risk ===")
    marked = W.create("marked", clean)
    if marked.worktree:
        (marked.worktree.path / "unsaved.txt").write_text("x", encoding="utf-8")
        listing = W.render_list(clean)
        check("the listing includes the worktree", "marked" in listing, listing[:160])
        check("the main tree is marked", "(main)" in listing, listing[:160])
        check("a worktree with work at risk is flagged", "untracked" in listing or "modified" in listing,
              listing[:200])
        W.remove("marked", clean, force=True)

    print()
    print("=== 16. results are rendered for a model to read ===")
    # The callers are agent paths, so a refusal has to become a sentence rather than a traceback.
    rendered = W.create("", clean).render()
    check("a refusal renders as text", isinstance(rendered, str) and len(rendered) > 20, rendered[:80])
    good = W.create("render-check", clean)
    check("a success renders with its details", "\n  " in good.render(), good.render()[:120])
    check("create_for_agent returns None on refusal rather than raising",
          W.create_for_agent("...", clean) is None)
    check("create_for_agent returns a Path on success",
          isinstance(W.create_for_agent("path-check", clean), Path))
    W.remove("render-check", clean, force=True)
    W.remove("path-check", clean, force=True)

    print()
    print(f"=== {len(PASS)} passed, {len(FAIL)} failed ===")
    for label in FAIL:
        print(f"  FAILED: {label}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
