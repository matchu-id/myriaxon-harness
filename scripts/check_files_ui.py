"""Verify the git view parsing and the file-explorer / diff endpoints.

    python scripts/check_files_ui.py

Three layers, because a UI feature can fail at any of them:

1. `git_view` parsing, against a real repository created in a scratch directory -- including the
   status codes, paths with spaces, renames, and untracked files.
2. The HTTP endpoints over the running dashboard, which is what the browser actually calls.
3. The panel's own markup, so a missing element id cannot silently break the wiring.

Everything is offline; no model is involved.
"""
import json
import os
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request
import warnings
from pathlib import Path

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8791
BASE = f"http://127.0.0.1:{PORT}"
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
warnings.filterwarnings("ignore")

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

passed, failed = [], []


def check(label, condition, detail=""):
    (passed if condition else failed).append(label)
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}{(' -- ' + str(detail)) if detail and not condition else ''}")


def get(path):
    return json.loads(urllib.request.urlopen(BASE + path, timeout=120).read().decode())


def main() -> int:
    from myriaxon import git_view

    print("=== git_view against a real repository ===")
    scratch = REPO / "bench-out" / f"filescheck{os.getpid()}"
    repo = scratch / "repo"
    repo.mkdir(parents=True, exist_ok=True)

    def git(*args):
        return subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True)

    git("init", "-q")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "Test")
    (repo / "tracked.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
    git("add", "tracked.txt")
    git("commit", "-q", "-m", "initial")

    check("a clean repository reports clean", git_view.summary(repo).get("clean") is True,
          git_view.summary(repo))

    # A directory inside a repository is NOT itself a repository.
    plain = scratch / "not-a-repo"
    plain.mkdir()
    check("a plain subdirectory is not reported as a repository", git_view.is_repository(plain) is False)
    check("its status says so", git_view.status(plain).get("is_repo") is False)
    check("its diff explains why", "not a git repository" in git_view.diff(plain)["error"],
          git_view.diff(plain)["error"])

    (repo / "tracked.txt").write_text("one\nCHANGED\nthree\n", encoding="utf-8")
    (repo / "staged.txt").write_text("staged\n", encoding="utf-8")
    git("add", "staged.txt")
    (repo / "name with spaces.txt").write_text("spaces\n", encoding="utf-8")

    state = git_view.status(repo)
    by_path = {entry["path"]: entry for entry in state["files"]}
    check("all three changes are listed", len(state["files"]) == 3, list(by_path))
    check("the modified file is labelled", by_path.get("tracked.txt", {}).get("label") == "modified",
          by_path.get("tracked.txt"))
    check("the staged file is marked staged", by_path.get("staged.txt", {}).get("staged") is True,
          by_path.get("staged.txt"))
    # The NUL-separated format is used precisely so a filename with spaces survives.
    check("a filename with spaces survives intact", "name with spaces.txt" in by_path, list(by_path))
    check("the branch is reported", bool(state.get("branch")), state.get("branch"))

    print("\n=== diffs ===")
    result = git_view.diff(repo, "tracked.txt")
    hunks = git_view.parse_unified(result["diff"])
    types = [line["type"] for hunk in hunks for line in hunk["lines"]]
    check("the diff has hunks", bool(hunks), result["diff"][:80])
    check("it contains an add, a delete and context", {"add", "del", "ctx"} <= set(types), types)
    added = [line for hunk in hunks for line in hunk["lines"] if line["type"] == "add"]
    check("the changed text is the added line", added and added[0]["text"] == "CHANGED", added)

    untracked = git_view.diff(repo, "name with spaces.txt")
    check("an untracked file shows its contents instead of an empty diff",
          untracked.get("untracked") is True and "+spaces" in untracked["diff"], untracked.get("diff", "")[:80])

    staged = git_view.diff(repo, "staged.txt", staged=True)
    check("a staged diff is available", "+staged" in staged["diff"], staged["diff"][:80])
    check("an unstaged file diff is empty for a staged-only change",
          git_view.diff(repo, "staged.txt").get("empty") is True,
          git_view.diff(repo, "staged.txt")["diff"][:60])

    print("\n=== the HTTP endpoints ===")
    config = get("/api/config")
    print(f"    workspace: {config['workspace']}")

    files = get("/api/files?path=")
    check("the file listing responds", "files" in files, list(files))
    check("it lists entries", len(files.get("files") or []) > 0, len(files.get("files") or []))
    first_file = next((f for f in files["files"] if not f.get("is_dir")), None)
    if first_file:
        check("a file entry has a path", bool(first_file.get("path")), first_file)
        one = get("/api/file?path=" + urllib.parse.quote(first_file["path"]))
        check("reading a file returns text", "text" in one or one.get("too_large"), list(one))
    else:
        check("a file entry has a path", False, "no files in the workspace")
        check("reading a file returns text", False, "no files in the workspace")

    git_state = get("/api/git")
    check("the git endpoint responds", "is_repo" in git_state, list(git_state))
    print(f"    git: repo={git_state.get('is_repo')} branch={git_state.get('branch')!r} "
          f"files={len(git_state.get('files') or [])}")

    diff_payload = get("/api/git/diff?path=")
    check("the whole-tree diff responds", "diff" in diff_payload, list(diff_payload))
    check("hunks travel with the diff", "hunks" in diff_payload, list(diff_payload))
    check("no error for a real repository", not diff_payload.get("error"), diff_payload.get("error"))

    print("\n=== escaping: a diff is data, never markup ===")
    nasty = scratch / "nasty"
    nasty.mkdir(exist_ok=True)
    git_in_nasty = lambda *a: subprocess.run(["git", *a], cwd=str(nasty), capture_output=True, text=True)
    git_in_nasty("init", "-q")
    git_in_nasty("config", "user.email", "t@example.com")
    git_in_nasty("config", "user.name", "Test")
    (nasty / "x.txt").write_text("safe\n", encoding="utf-8")
    git_in_nasty("add", "x.txt")
    git_in_nasty("commit", "-q", "-m", "init")
    (nasty / "x.txt").write_text('<script>alert("xss")</script>\n', encoding="utf-8")
    payload = git_view.diff(nasty, "x.txt")
    hunks = git_view.parse_unified(payload["diff"])
    texts = [line["text"] for hunk in hunks for line in hunk["lines"]]
    check("the markup is carried as plain text", any("<script>" in t for t in texts), texts[:3])
    check("the response is JSON, not HTML", isinstance(payload["diff"], str))
    # The endpoint must not wrap diff text in markup either.
    check("hunks are structured data", all("type" in line for hunk in hunks for line in hunk["lines"]))

    print("\n=== the panel markup the JS depends on ===")
    from myriaxon.dashboard import ui as ui_module

    markup = ui_module.INDEX_HTML
    for element in ("codemodal", "codebtn", "codelist", "codechanges", "codebody", "codetitle",
                    "codebranch", "codecount", "codepath", "codepane-files", "codepane-changes",
                    "codeviewer", "codestagedtoggle", "codeback", "coderefresh", "codeclose"):
        check(f"#{element} exists in the page", f'id="{element}"' in markup)
    check("the tabs are wired by data attribute", 'data-codetab="files"' in markup and 'data-codetab="changes"' in markup)
    check("the get() helper exists for the explorer", "const get = (p) => api(" in markup)
    check("diff lines use textContent, never innerHTML", ".textContent = line.text" in markup)
    check("the diff line classes are styled",
          all(cls in markup for cls in (".cl.add", ".cl.del", ".cl.hunk")))

    shutil.rmtree(scratch, ignore_errors=True)
    print(f"\n{len(passed)} passed, {len(failed)} failed")
    if failed:
        print("failed:", failed)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
