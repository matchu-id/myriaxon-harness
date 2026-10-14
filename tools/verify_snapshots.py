#!/usr/bin/env python3
"""Verify the snapshot harness.

The claim under test is that this catches drift. So most of these checks *introduce* drift -- a reworded
description, a renamed schema field, a reordered prompt, a changed file -- and assert that it is reported with a
diff that names what changed.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from myriaxon import snapshots as S  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAIL).append(label)
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}" + (f"\n         {detail}" if detail else ""))


def build_case(root: Path, name: str = "demo") -> Path:
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / S.MANIFEST).write_text(
        json.dumps({"format_version": 1, "scenario": name, "workspace": {"final": False}}),
        encoding="utf-8",
    )
    (directory / S.PROMPT_EXPECTED).write_text("You are a test harness.\n", encoding="utf-8")
    (directory / S.SCHEMAS_EXPECTED).write_text(json.dumps({
        "initial": [
            {"name": "read_file", "description": "Read a file",
             "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}}},
            {"name": "run_bash", "description": "Run a shell command",
             "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}}},
        ],
        "changes": [],
    }, indent=2), encoding="utf-8")
    S.write_jsonl(directory / S.TRANSCRIPT, [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
    ])
    (directory / S.RESULT_EXPECTED).write_text(json.dumps({"answer": "hi"}), encoding="utf-8")
    return directory


def main() -> int:
    root = Path(tempfile.mkdtemp())
    build_case(root)

    print("=== 1. a case loads and reports its shape ===")
    case = S.load_case(root, "demo")
    check("the prompt was read", "test harness" in case.prompt)
    check("the schemas were read", len(case.schemas["initial"]) == 2)
    check("the transcript was read", len(case.transcript) == 2)
    check("the manifest was read", case.manifest["scenario"] == "demo")
    check("the workspace oracle is off by default", not case.checks_workspace)
    check("discover finds it", S.discover(root) == ["demo"], str(S.discover(root)))

    print()
    print("=== 2. THE CHECK THAT IS EASIEST TO OMIT: the normalizer is a fixed point ===")
    # Their `expect(scrub(expected)).toBe(expected)`. If this does not hold the normalizer is still rewriting
    # content, so every later comparison is between two altered values and a pass means nothing.
    good = S.Normalizer(cwd="/work/project", session_id="abc123", home="/home/person")
    try:
        good.assert_fixed_point()
        check("a correct normalizer passes the fixed-point check", True)
    except S.SnapshotError as exc:
        check("a correct normalizer passes the fixed-point check", False, str(exc))

    class Broken(S.Normalizer):
        """A normalizer that keeps rewriting its own output -- the common real bug."""
        def scrub_text(self, text: str) -> str:
            return text + "!"

    try:
        Broken(cwd="/x").assert_fixed_point()
        check("a non-idempotent normalizer is CAUGHT", False, "no error was raised")
    except S.SnapshotError as exc:
        check("a non-idempotent normalizer is CAUGHT", True)
        check("the error shows both passes", "once:" in str(exc) and "twice:" in str(exc), str(exc)[:150])

    print()
    print("=== 3. scrubbing replaces machine-specific values with stable tokens ===")
    normalizer = S.Normalizer(cwd="/work/proj", session_id="sess-9", home="/home/me")
    scrubbed = normalizer.scrub_text("cwd=/work/proj file=/home/me/notes.txt session=sess-9")
    check("the cwd became a token", "{{cwd}}" in scrubbed, scrubbed)
    check("the home became a token", "{{home}}" in scrubbed, scrubbed)
    check("the session became a token", "{{session}}" in scrubbed, scrubbed)
    check("no real path survived", "/work/proj" not in scrubbed and "/home/me" not in scrubbed, scrubbed)
    check("scrubbing is idempotent on real input",
          normalizer.scrub_text(scrubbed) == scrubbed, normalizer.scrub_text(scrubbed))

    # The forward-slash variant only exists for a Windows-style cwd. The earlier version of this check used
    # `/work/proj` and asked for `C:\work\proj`, which cannot occur, so it tested nothing.
    windows = S.Normalizer(cwd=r"C:\work\proj", session_id="s", home=r"C:\Users\me")
    for written in (r"cwd=C:\work\proj", "cwd=C:/work/proj"):
        got = windows.scrub_text(written)
        check(f"the {written.split('=')[1][:12]!r} form is scrubbed", "{{cwd}}" in got, got)

    check("scrubbing is idempotent for both forms",
          windows.scrub_text(windows.scrub_text(r"a C:\work\proj b C:/work/proj")) ==
          windows.scrub_text(r"a C:\work\proj b C:/work/proj"))

    print()
    print("=== 4. an identical harness PASSES ===")
    outcome = S.check_case(
        case,
        prompt_builder=lambda: "You are a test harness.\n",
        schema_builder=lambda: case.schemas,
        transcript_runner=lambda _case: case.transcript,
        normalizer=S.Normalizer(),
    )
    check("a matching case passes", outcome.ok, outcome.comparison.render()[:300])

    print()
    print("=== 5. THE FIX: a reworded tool description FAILS, and names the tool ===")
    # This is the change most likely to slip through a unit test suite, and the reason their verifyHeaders()
    # reconstructs the request and asserts equality.
    drifted = json.loads(json.dumps(case.schemas))
    drifted["initial"][0]["description"] = "Read a file from disk"
    outcome = S.check_case(
        case,
        prompt_builder=lambda: case.prompt,
        schema_builder=lambda: drifted,
        normalizer=S.Normalizer(),
    )
    check("a reworded description fails", not outcome.ok)
    rendered = outcome.comparison.render()
    check("the failure names the tool", "read_file" in rendered, rendered[:200])
    check("it shows both versions", "Read a file" in rendered and "Read a file from disk" in rendered,
          rendered[:300])

    print()
    print("=== 6. an added or removed tool is reported as that, not as a change ===")
    removed = json.loads(json.dumps(case.schemas))
    removed["initial"] = [removed["initial"][0]]
    outcome = S.check_case(case, prompt_builder=lambda: case.prompt,
                           schema_builder=lambda: removed, normalizer=S.Normalizer())
    check("a removed tool is named as REMOVED", "REMOVED" in outcome.comparison.render(),
          outcome.comparison.render()[:220])

    added = json.loads(json.dumps(case.schemas))
    added["initial"].append({"name": "new_tool", "description": "brand new",
                             "input_schema": {"type": "object", "properties": {}}})
    outcome = S.check_case(case, prompt_builder=lambda: case.prompt,
                           schema_builder=lambda: added, normalizer=S.Normalizer())
    check("an added tool is named as ADDED", "ADDED" in outcome.comparison.render(),
          outcome.comparison.render()[:220])

    print()
    print("=== 7. a changed system prompt fails ===")
    outcome = S.check_case(case, prompt_builder=lambda: "You are a DIFFERENT harness.\n",
                           schema_builder=lambda: case.schemas, normalizer=S.Normalizer())
    check("a changed prompt fails", not outcome.ok)
    check("the difference is labelled", "system prompt" in outcome.comparison.render(),
          outcome.comparison.render()[:150])

    print()
    print("=== 8. a diverging transcript fails and points at the turn ===")
    shorter = case.transcript[:1]
    outcome = S.check_case(case, prompt_builder=lambda: case.prompt,
                           schema_builder=lambda: case.schemas,
                           transcript_runner=lambda _case: shorter, normalizer=S.Normalizer())
    check("a shorter transcript fails", not outcome.ok)
    check("the length is reported separately", "transcript length" in outcome.comparison.render(),
          outcome.comparison.render()[:200])

    changed = [case.transcript[0], {"role": "assistant", "content": [{"type": "text", "text": "different"}]}]
    outcome = S.check_case(case, prompt_builder=lambda: case.prompt,
                           schema_builder=lambda: case.schemas,
                           transcript_runner=lambda _case: changed, normalizer=S.Normalizer())
    check("a changed turn is reported with its number", "turn 2" in outcome.comparison.render(),
          outcome.comparison.render()[:200])

    print()
    print("=== 9. the workspace is an INDEPENDENT oracle ===")
    # Model prose and tool-result text are claims about effects; the file tree is the effect. Their post-mortem
    # 0002 recorded a refresh where seven scenarios passed while every tool was absent.
    workspace_root = Path(tempfile.mkdtemp())
    build_case(workspace_root)
    case_dir = workspace_root / "demo"
    expected = case_dir / S.WORKSPACE_EXPECTED
    expected.mkdir(parents=True, exist_ok=True)
    (expected / "output.txt").write_text("the tool wrote this\n", encoding="utf-8")
    (case_dir / S.MANIFEST).write_text(
        json.dumps({"format_version": 1, "workspace": {"final": True}}), encoding="utf-8")

    ws_case = S.load_case(workspace_root, "demo")
    check("the workspace oracle is now on", ws_case.checks_workspace)

    actual = Path(tempfile.mkdtemp())
    (actual / "output.txt").write_text("the tool wrote this\n", encoding="utf-8")
    outcome = S.check_case(ws_case, prompt_builder=lambda: ws_case.prompt,
                           schema_builder=lambda: ws_case.schemas,
                           workspace_dir=actual, normalizer=S.Normalizer())
    check("a matching workspace passes", outcome.ok, outcome.comparison.render()[:300])

    # THE CASE THAT MATTERS: every tool absent, so nothing was written.
    empty = Path(tempfile.mkdtemp())
    outcome = S.check_case(ws_case, prompt_builder=lambda: ws_case.prompt,
                           schema_builder=lambda: ws_case.schemas,
                           workspace_dir=empty, normalizer=S.Normalizer())
    check("a workspace that was never written FAILS", not outcome.ok,
          "this is the post-mortem-0002 case: prose claims an effect the tree does not show")
    check("the missing file is named", "output.txt" in outcome.comparison.render(),
          outcome.comparison.render()[:200])
    check("it says MISSING", "MISSING" in outcome.comparison.render())

    (actual / "extra.txt").write_text("unexpected\n", encoding="utf-8")
    outcome = S.check_case(ws_case, prompt_builder=lambda: ws_case.prompt,
                           schema_builder=lambda: ws_case.schemas,
                           workspace_dir=actual, normalizer=S.Normalizer())
    check("an unexpected file fails", not outcome.ok)
    check("it says UNEXPECTED", "UNEXPECTED" in outcome.comparison.render(),
          outcome.comparison.render()[:200])

    (actual / "output.txt").write_text("the tool wrote SOMETHING ELSE\n", encoding="utf-8")
    outcome = S.check_case(ws_case, prompt_builder=lambda: ws_case.prompt,
                           schema_builder=lambda: ws_case.schemas,
                           workspace_dir=actual, normalizer=S.Normalizer())
    check("changed file content fails", not outcome.ok)
    check("it names the file that differs", "output.txt differs" in outcome.comparison.render(),
          outcome.comparison.render()[:220])

    print()
    print("=== 10. a case requiring the workspace oracle fails without one ===")
    # Silently skipping the oracle would make the strongest check in the suite optional by accident.
    outcome = S.check_case(ws_case, prompt_builder=lambda: ws_case.prompt,
                           schema_builder=lambda: ws_case.schemas, normalizer=S.Normalizer())
    check("a missing workspace directory is a failure, not a skip", not outcome.ok)
    check("the reason says the manifest requires it",
          "requires the workspace oracle" in outcome.comparison.render(),
          outcome.comparison.render()[:200])

    print()
    print("=== 11. a newer format version is refused, not guessed at ===")
    future = root / "future"
    future.mkdir(parents=True, exist_ok=True)
    (future / S.MANIFEST).write_text(json.dumps({"format_version": 99}), encoding="utf-8")
    try:
        S.load_case(root, "future")
        check("a future format is refused", False, "it was read anyway")
    except S.SnapshotError as exc:
        check("a future format is refused", True)
        check("the error names both versions", "v99" in str(exc) and f"v{S.SNAPSHOT_FORMAT_VERSION}" in str(exc),
              str(exc))

    print()
    print("=== 12. a malformed transcript line raises rather than being skipped ===")
    # A dropped turn would replay as a different conversation and still compare equal to itself.
    broken = root / "broken"
    broken.mkdir(parents=True, exist_ok=True)
    (broken / S.MANIFEST).write_text(json.dumps({"format_version": 1}), encoding="utf-8")
    (broken / S.TRANSCRIPT).write_text('{"role": "user"}\nnot json at all\n', encoding="utf-8")
    try:
        S.load_case(root, "broken")
        check("a malformed line raises", False, "it was skipped silently")
    except S.SnapshotError as exc:
        check("a malformed line raises", True)
        check("the error names the line number", "line 2" in str(exc), str(exc))

    print()
    print("=== 13. a missing case is a clear error ===")
    try:
        S.load_case(root, "does-not-exist")
        check("a missing case raises", False, "no error")
    except S.SnapshotError as exc:
        check("a missing case raises", True, str(exc)[:80])

    print()
    print("=== 14. run_all reports every case, including an unloadable one ===")
    outcomes = S.run_all(root, prompt_builder=lambda: "wrong", schema_builder=lambda: {},
                         normalizer=S.Normalizer())
    check("every discovered case produced an outcome", len(outcomes) == len(S.discover(root)),
          f"{len(outcomes)} outcomes for {len(S.discover(root))} cases")
    check("none of them passed against a wrong prompt", all(not o.ok for o in outcomes),
          str([(o.case, o.ok) for o in outcomes]))
    check("each outcome names its case", all(o.case for o in outcomes))

    # An unloadable case must be reported, not abort the run. Otherwise one malformed file means no case runs, the
    # suite looks broken rather than the case, and the natural response is to delete the file.
    broken_outcome = next((o for o in outcomes if o.case == "broken"), None)
    check("the malformed case is present", broken_outcome is not None,
          str([o.case for o in outcomes]))
    if broken_outcome is not None:
        check("it failed rather than aborting the run", not broken_outcome.ok)
        check("its mode says it was a load failure", broken_outcome.mode == "load", broken_outcome.mode)
        check("the reason carries the parse error",
              "not valid JSON" in broken_outcome.comparison.render(),
              broken_outcome.comparison.render()[:180])

    print()
    print("=== 15. notes explain what was NOT checked ===")
    # A case with no pinned schemas passes trivially on that axis; saying so is what stops a reader assuming
    # full coverage.
    sparse = root / "sparse"
    sparse.mkdir(parents=True, exist_ok=True)
    (sparse / S.MANIFEST).write_text(json.dumps({"format_version": 1}), encoding="utf-8")
    (sparse / S.PROMPT_EXPECTED).write_text("only a prompt\n", encoding="utf-8")
    outcome = S.check_case(S.load_case(root, "sparse"), prompt_builder=lambda: "only a prompt\n",
                           schema_builder=lambda: {}, normalizer=S.Normalizer())
    check("it passes on what it has", outcome.ok)
    check("it notes the schemas were not pinned",
          any("no pinned tool schemas" in note for note in outcome.comparison.notes),
          str(outcome.comparison.notes))

    print()
    print(f"=== {len(PASS)} passed, {len(FAIL)} failed ===")
    for label in FAIL:
        print(f"  FAILED: {label}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
