#!/usr/bin/env python3
"""Verify session lineage: branching a session, and navigating the tree of branches.

## The gap this closes

Myriaxon stored exactly one transcript per project directory, so exploring a second direction from a point in a
conversation meant destroying the first. pi-harness has `sessionFork` and `sessionTree`; Myriaxon had neither.

## What these checks are mostly about

The feature's whole value is that a fork is **safe** — taking one must not damage the conversation it came from,
and asking for a fork point that does not exist must fail loudly rather than quietly give you a different
conversation. Two of the three bugs found while building this were exactly that class, so most of what follows
asserts a refusal or an unchanged parent rather than a happy path.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from myriaxon import session_lineage as SL  # noqa: E402
from myriaxon import session_store as SS  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAIL).append(label)
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}" + (f"\n         {detail}" if detail else ""))


def seed(root: Path, count: int = 6, session_id: str = "root-1") -> list[dict]:
    messages = [{"role": "user", "content": f"m{i}"} for i in range(count)]
    SS.save_session("anthropic", "claude-sonnet-4-5", "auto", None, False, messages, root,
                    session_id=session_id)
    return messages


def main() -> int:
    tmp = Path(tempfile.mkdtemp())

    print("=== 1. the identity of a session is persisted ===")
    # The first version never wrote a session id, so a fork had nothing stable to name as its parent -- the only
    # candidate was the file path, and every branch overwrote the same file.
    seed(tmp, 6, "root-1")
    active = SS.load_session(tmp)
    check("the active session carries an id", active.get("session_id") == "root-1",
          str(active.get("session_id")))
    check("and its transcript", len(active.get("messages", [])) == 6)

    print()
    print("=== 2. an ordinary turn does NOT erase the id ===")
    # A turn passes no id. If that blanked the identity, every fork after the next turn would lose its parent.
    SS.save_session("anthropic", "claude-sonnet-4-5", "auto", None, False,
                    active["messages"] + [{"role": "assistant", "content": "a"}], tmp)
    check("the id survives a save that omits it",
          SS.load_session(tmp).get("session_id") == "root-1",
          str(SS.load_session(tmp).get("session_id")))
    check("and the transcript grew", len(SS.load_session(tmp)["messages"]) == 7)

    print()
    print("=== 3. THE PARENT SURVIVES A FORK ===")
    # The central safety property, and the bug the first version shipped: every branch was written to the ONE main
    # session file, so creating a branch destroyed the conversation it branched from.
    before = SS.load_session(tmp)
    first = SL.fork_session(name="try-A", project_root=tmp)
    after = SS.load_session(tmp)
    check("the fork succeeded", first.ok, first.render())
    check("the parent's transcript is unchanged", before["messages"] == after["messages"],
          f"{len(before['messages'])} -> {len(after['messages'])}")
    check("the parent's id is unchanged", after.get("session_id") == "root-1")
    check("the branch got its own file",
          SS.load_session(tmp, first.session_id) is not None
          or (tmp / ".myriaxon" / SS.BRANCH_DIR).is_dir())
    check("the branch inherited everything", first.inherited == 7, str(first.inherited))

    print()
    print("=== 4. a partial fork inherits exactly that prefix ===")
    second = SL.fork_session(name="try-B", fork_index=2, project_root=tmp)
    check("the fork succeeded", second.ok, second.render())
    check("it inherited exactly 2", second.inherited == 2, str(second.inherited))
    branch = SS.load_session(tmp, second.session_id)
    check("the branch's messages ARE the parent's first two",
          [m["content"] for m in branch["messages"]] == ["m0", "m1"],
          str([m["content"] for m in branch["messages"]]))
    check("and it is a different session from the parent",
          branch.get("session_id") == second.session_id)
    check("the two branches have different ids", first.session_id != second.session_id)

    print()
    print("=== 5. AN OUT-OF-RANGE FORK POINT IS REFUSED, not clamped ===")
    # Clamping would silently give you a different conversation than the one you asked for, which is worse than an
    # error: you would keep working in a branch you did not choose.
    for bad, why in ((99, "past the end"), (-1, "negative")):
        result = SL.fork_session(fork_index=bad, project_root=tmp)
        check(f"fork_index={bad} is refused", not result.ok, result.reason[:90])
    check("  the reason says why for a past-the-end index",
          "past the end" in SL.fork_session(fork_index=99, project_root=tmp).reason)
    check("  and it names the real length",
          "7" in SL.fork_session(fork_index=99, project_root=tmp).reason,
          SL.fork_session(fork_index=99, project_root=tmp).reason[:110])
    check("fork_index=0 is allowed and empty",
          SL.fork_session(fork_index=0, project_root=tmp).inherited == 0)
    check("fork_index == len is allowed and full",
          SL.fork_session(fork_index=7, project_root=tmp).inherited == 7)

    print()
    print("=== 6. refusing does NOT create a branch ===")
    # A refusal that leaves a half-made branch behind is worse than no refusal.
    tree_before = len(SL.load_tree(tmp))
    for _ in range(3):
        SL.fork_session(fork_index=500, project_root=tmp)
    check("the tree did not grow", len(SL.load_tree(tmp)) == tree_before,
          f"{tree_before} -> {len(SL.load_tree(tmp))}")

    print()
    print("=== 7. forking with NO active session is refused ===")
    empty = Path(tempfile.mkdtemp())
    result = SL.fork_session(project_root=empty)
    check("refused", not result.ok, result.reason[:100])
    check("and it explains why a fork needs a persisted session",
          "never written" in result.reason or "active session" in result.reason, result.reason[:120])

    print()
    print("=== 8. the tree is navigable ===")
    tree = SL.load_tree(tmp)
    check("the root is registered", "root-1" in tree, str(sorted(tree)))
    kids = SL.children("root-1", tmp)
    check("the root has children", len(kids) >= 2, str([k.name for k in kids]))
    check("a child names its parent", all(k.parent == "root-1" for k in kids))
    check("the child records its fork point", all(k.fork_index >= 0 for k in kids))
    check("ancestors of a child include the root",
          "root-1" in [a.session_id for a in SL.ancestors(kids[0].session_id, tmp)])
    check("path_from_root runs root first",
          SL.path_from_root(kids[0].session_id, tmp)[0].session_id == "root-1")
    check("descendants of the root include the children",
          {k.session_id for k in kids} <= {d.session_id for d in SL.descendants("root-1", tmp)})
    check("a leaf has no descendants", SL.descendants(kids[0].session_id, tmp) == [])
    check("roots are only the root", [r.session_id for r in SL.roots(tmp)] == ["root-1"],
          str([r.session_id for r in SL.roots(tmp)]))

    print()
    print("=== 9. NESTED forks work (a branch of a branch) ===")
    # Resuming a branch makes it active, which is the only way to continue working in it -- and then it can itself
    # be forked, which is what makes this a tree rather than a fan.
    resumed, why = SL.resume_branch(second.session_id, tmp)
    check("a branch can be resumed", resumed, why)
    check("it becomes the active session", SS.load_session(tmp).get("session_id") == second.session_id)
    check("and its own file is still present",
          SS.load_session(tmp, second.session_id) is not None)
    grandchild = SL.fork_session(name="try-B1", project_root=tmp)
    check("the resumed branch can be forked", grandchild.ok, grandchild.render())
    check("the grandchild's parent is the branch", grandchild.parent == second.session_id,
          grandchild.parent)
    # `ancestors` is NEAREST FIRST, which is what the docstring says; `path_from_root` is the root-first order.
    # The first version of this check expected root-first from `ancestors` and failed against correct code.
    check("ancestors of the grandchild are branch then root (nearest first)",
          [a.session_id for a in SL.ancestors(grandchild.session_id, tmp)] == ["root-1", second.session_id][::-1],
          str([a.session_id for a in SL.ancestors(grandchild.session_id, tmp)]))
    check("path_from_root of the grandchild is root, branch, grandchild",
          [b.session_id for b in SL.path_from_root(grandchild.session_id, tmp)]
          == ["root-1", second.session_id, grandchild.session_id],
          str([b.session_id for b in SL.path_from_root(grandchild.session_id, tmp)]))

    print()
    print("=== 10. resuming is REPEATABLE, not destructive ===")
    ok_again, _ = SL.resume_branch(second.session_id, tmp)
    check("resuming twice works", ok_again)
    check("and the branch file still exists", SS.load_session(tmp, second.session_id) is not None,
          "a resume that consumed the branch would make the fork point unreachable")
    check("resuming an unknown id is refused", not SL.resume_branch("nope", tmp)[0])

    print()
    print("=== 11. CYCLES ARE REFUSED ===")
    # Nothing in the normal flow can build one, which is exactly why the guard is here: it is for the programmatic
    # caller and for an index someone edited by hand.
    ok, why = SL.set_parent("root-1", "root-1", project_root=tmp)
    check("a session cannot be its own parent", not ok, why)
    ok, why = SL.set_parent("root-1", grandchild.session_id, project_root=tmp)
    check("a session cannot be parented to its own descendant", not ok, why)
    check("  and the reason says why", "descendant" in why or "own ancestor" in why, why[:100])
    ok, why = SL.set_parent("", "root-1", project_root=tmp)
    check("an empty id is refused", not ok, why)

    print()
    print("=== 12. a CORRUPT index degrades, it does not crash ===")
    # An unreadable tree costs you the tree view. Raising would cost you the session, because every caller of this
    # is also on a path that has to work.
    index = tmp / ".myriaxon" / SL.BRANCH_INDEX_FILENAME
    saved = index.read_text(encoding="utf-8")
    try:
        index.write_text("{ not json", encoding="utf-8")
        check("a corrupt index loads as an empty tree", SL.load_tree(tmp) == {})
        check("and the tree still renders", isinstance(SL.render_tree(tmp), str))
        index.write_text(json.dumps({"branches": "not a list"}), encoding="utf-8")
        check("a wrong-shaped index also loads empty", SL.load_tree(tmp) == {})
        index.write_text(json.dumps({"branches": [{"session_id": ""}, "junk", {"session_id": "ok"}]}),
                         encoding="utf-8")
        loaded = SL.load_tree(tmp)
        check("junk entries are skipped, real ones kept", set(loaded) == {"ok"}, str(sorted(loaded)))
    finally:
        index.write_text(saved, encoding="utf-8")

    print()
    print("=== 13. a hand-edited index cannot HANG a listing ===")
    # A cycle in the file would make `ancestors` loop forever without the visited set. Verified by building one.
    index = tmp / ".myriaxon" / SL.BRANCH_INDEX_FILENAME
    saved = index.read_text(encoding="utf-8")
    try:
        index.write_text(json.dumps({"branches": [
            {"session_id": "x", "parent": "y", "created_at": 1},
            {"session_id": "y", "parent": "x", "created_at": 2},
        ]}), encoding="utf-8")
        chain = SL.ancestors("x", tmp)
        check("ancestors terminates on a cycle", isinstance(chain, list), f"{len(chain)} entries")
        check("descendants terminates too", isinstance(SL.descendants("x", tmp), list))
        rendered = SL.render_tree(tmp)
        check("the tree renders a cycle without hanging", isinstance(rendered, str))
        check("and it says the cycle exists", "cycle" in rendered.lower() or "unreachable" in rendered.lower(),
              rendered[:160])
    finally:
        index.write_text(saved, encoding="utf-8")

    print()
    print("=== 14. a branch id cannot escape the project directory ===")
    # Ids arrive from a file the user can edit, so they are sanitised rather than trusted.
    escaped = SS._session_path(tmp, "../../../../etc/passwd")
    check("a traversal id stays inside .myriaxon",
          ".myriaxon" in str(escaped) and ".." not in str(escaped), str(escaped))
    check("an id of only separators falls back to the main file",
          SS._session_path(tmp, "///") == tmp / ".myriaxon" / "SESS" .replace("SESS", SS.SESSION_FILENAME),
          str(SS._session_path(tmp, "///")))

    print()
    print("=== 15. the rendered tree is labelled and complete ===")
    rendered = SL.render_tree(tmp)
    check("every registered branch appears",
          all(b.name in rendered or b.session_id in rendered for b in SL.load_tree(tmp).values()),
          rendered[:200])
    check("a fork point is shown for children", "message(s)" in rendered, rendered[:160])
    check("an empty tree explains the feature",
          "branch_session" in SL.render_tree(Path(tempfile.mkdtemp())))

    print()
    print("=== 16. the OLD single-session behaviour is untouched ===")
    # Every existing caller passes no session id. If saving with none stopped working, this would break every turn.
    legacy = Path(tempfile.mkdtemp())
    check("a save with no id writes the main file",
          SS.save_session("openai", "gpt", "auto", None, False, [{"role": "user", "content": "x"}], legacy).written)
    check("and it loads back", SS.load_session(legacy)["messages"][0]["content"] == "x")
    check("no branch directory is created", not (legacy / ".myriaxon" / SS.BRANCH_DIR).exists())
    check("list_branches is empty", SS.list_branches(legacy) == [])

    print()
    print(f"=== {len(PASS)} passed, {len(FAIL)} failed ===")
    for label in FAIL:
        print(f"  FAILED: {label}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
