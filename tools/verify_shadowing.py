#!/usr/bin/env python3
"""Verify session-log shadowing: a pruned tool result is recoverable by the model.

The claim under test is not that pruning works -- it is that what pruning removed comes back **byte for byte**,
and that a handle the model supplies cannot be turned into a filesystem read outside the store.
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from myriaxon import compaction as C  # noqa: E402
from myriaxon import compaction_audit as A  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAIL).append(label)
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}" + (f"\n         {detail}" if detail else ""))


HANDLE_RE = re.compile(r"handle '([0-9a-f]{64})'")


def main() -> int:
    scratch = Path(tempfile.mkdtemp())
    original_audit_dir = A.AUDIT_DIR
    A.AUDIT_DIR = scratch
    try:
        return run(scratch)
    finally:
        A.AUDIT_DIR = original_audit_dir
        A.set_active(None)


def run(scratch: Path) -> int:
    print("=== 1. THE FIX: a pruned result is replaced by a handle, not a dead end ===")
    recorder = A.Recorder(session="s1", enabled=True, root=scratch)
    A.set_active(recorder)

    original = "\n".join(f"line {i}: " + "y" * 60 for i in range(400))
    messages = [
        {"role": "user", "content": "go"},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "tu_1", "content": original},
        ]},
        {"role": "assistant", "content": "done"},
    ]
    pruned = C.prune_stale_tool_results(messages, max_age_turns=0, min_chars=100)
    check("one result was pruned", pruned == 1, str(pruned))

    placeholder = messages[1]["content"][0]["content"]
    match = HANDLE_RE.search(placeholder)
    check("the placeholder names a handle", match is not None, placeholder[:100])
    # The point of the change: the old placeholder said "re-run the tool", which costs a whole call to recover
    # content the harness already holds.
    check("it does NOT tell the model to re-run the tool",
          "re-run the tool" not in placeholder, placeholder[:140])
    check("it says to call recall", "call recall" in placeholder, placeholder[:140])
    check("the placeholder is much smaller than what it replaced",
          len(placeholder) < len(original) // 20, f"{len(placeholder)} vs {len(original)}")

    if match is None:
        return finish()
    handle = match.group(1)

    print()
    print("=== 2. RECOVERABILITY: recall returns exactly what was removed ===")
    content, problem = A.recall(handle, session="s1", root=scratch)
    check("no problem", not problem, problem)
    check("byte-for-byte identical", content == original,
          f"{len(content or '')} vs {len(original)} chars")
    check("the first line survived", content is not None and content.startswith("line 0:"))
    check("the LAST line survived -- the part a log preview would have lost",
          content is not None and "line 399:" in content,
          "the audit log keeps only a 2,000-char preview, so this is what the store is for")

    print()
    print("=== 3. the handle is VERIFIED, not trusted ===")
    # A digest is a claim about content. If the store were edited, the model has no other way to tell.
    stored = scratch / "s1" / "content" / handle[:2] / handle
    check("the content is stored under its digest", stored.is_file())
    stored.write_text("tampered content", encoding="utf-8")
    corrupted, problem2 = A.recall(handle, session="s1", root=scratch)
    check("tampered content is REFUSED", corrupted is None, "it returned the tampered text")
    check("the reason says the store was modified", "modified or corrupted" in problem2, problem2)
    stored.write_text(original, encoding="utf-8")
    check("restoring the content restores recall", A.recall(handle, session="s1", root=scratch)[0] == original)

    print()
    print("=== 4. SECURITY: a handle cannot become a filesystem read ===")
    # The handle arrives from the model's context, so it is untrusted input. The digest IS the filename, so
    # validating its shape and confining the path are the same check -- which is why the shape check comes first.
    for hostile in (
        "../../../etc/passwd",
        "../../config.json",
        "/etc/shadow",
        "C:/Windows/System32/drivers/etc/hosts",
        "..",
        ".",
        "a" * 64,                     # right shape, no such content
        "Z" * 64,                     # not hex
        "a" * 63,                     # one short
        "a" * 65,                     # one long
        "  " + "a" * 64 + "  ",       # padded: stripped, then not found
        "",
    ):
        result, problem = A.recall(hostile, session="s1", root=scratch)
        check(f"refuses {hostile[:26]!r}", result is None)
        check(f"  and explains why for {hostile[:18]!r}", bool(problem))

    print()
    print("=== 5. a valid handle from another session still resolves ===")
    # A summary can quote a handle that outlives the session it came from, so the search is not limited to the
    # session the caller names.
    other = A.Recorder(session="s2", enabled=True, root=scratch)
    second = "content from the other session"
    other.record_prune(second, tool="read_file")
    second_handle = next(
        e["digest"] for e in other.read_log() if e.get("kind") == "prune"
    )
    found, problem3 = A.recall(second_handle, session="s1", root=scratch)
    check("a handle from another session resolves", found == second, problem3)

    print()
    print("=== 6. THE OTHER HALF: search finds content the log preview lost ===")
    # The log keeps 2,000 characters. Searching only the log would miss anything beyond that, which is exactly
    # the content worth recovering -- so the search reads the store.
    long_text = ("filler " * 600) + "NEEDLE_AT_THE_END"
    recorder.record_prune(long_text, tool="grep_search")
    deep_handle = next(
        e["digest"] for e in recorder.read_log()
        if e.get("kind") == "prune" and int(e.get("chars") or 0) == len(long_text)
    )
    stored_entry = next(e for e in recorder.read_log() if e.get("digest") == deep_handle)
    check("the needle is NOT in the log preview",
          "NEEDLE_AT_THE_END" not in str(stored_entry.get("preview") or ""),
          "if it were, this check would not be testing the store")
    hits = A.search("NEEDLE_AT_THE_END", session="s1", root=scratch)
    check("search finds it anyway", any(h["handle"] == deep_handle for h in hits),
          str([h["handle"][:10] for h in hits]))
    check("the hit says it matched in the content",
          any(h["matched_in"] == "content" for h in hits), str([h["matched_in"] for h in hits]))
    check("no spurious hit for a term that is absent",
          A.search("NOT_PRESENT_ANYWHERE_XYZ", session="s1", root=scratch) == [])

    print()
    print("=== 7. trace answers 'where did this come from?' ===")
    events = A.trace(handle, session="s1", root=scratch)
    check("at least one event refers to the handle", len(events) >= 1, str(len(events)))
    check("the storing event is marked", any(e["relation"] == "stored-here" for e in events),
          str([e["relation"] for e in events]))
    check("a malformed handle traces nothing", A.trace("nope", session="s1", root=scratch) == [])

    print()
    print("=== 8. recall is BOUNDED, so a recall cannot undo the compaction ===")
    # The reason this content left the transcript is that it was too large to keep. Handing it back unbounded
    # would put the harness exactly where it started, one call later.
    rendered = A.render_recall("x" * 100_000, "a" * 64, max_chars=5_000)
    check("the rendering is bounded", len(rendered) < 6_000, str(len(rendered)))
    check("it says it was truncated", "truncated" in rendered)
    check("it says how to get the rest", "still in the store" in rendered)
    check("it labels itself as recalled text", rendered.startswith("[recalled"))
    check("it reports the original size", "100,000" in rendered)
    short = A.render_recall("small", "b" * 64, max_chars=5_000)
    check("a short recall is not marked truncated", "truncated" not in short)

    print()
    print("=== 9. auditing disabled changes the placeholder honestly ===")
    # With no store there is no handle, so the placeholder must not name one that would not resolve.
    A.set_active(None)
    messages2 = [
        {"role": "user", "content": "go"},
        {"role": "tool", "content": "z" * 5_000, "name": "read_file"},
    ]
    C.prune_stale_tool_results(messages2, max_age_turns=0, min_chars=100)
    plain = messages2[1]["content"]
    check("the placeholder was still written", plain.startswith("[pruned:"))
    check("it does NOT name a handle", HANDLE_RE.search(plain) is None, plain[:120])
    check("it falls back to re-running the tool", "re-run the tool" in plain, plain[:140])
    A.set_active(recorder)

    print()
    print("=== 10. a tool failure during recall does not break the audit ===")
    # Auditing runs mid-turn; a record failure must not fail compaction. The counter exists so it is visible.
    broken = A.Recorder(session="s3", enabled=True, root=Path("Z:/definitely/not/a/real/path"))
    broken.record_prune("some content")
    check("the failure was counted, not raised", broken.failures >= 1, str(broken.failures))
    check("the recorder reports it", broken.summary()["failures"] >= 1)

    print()
    print("=== 11. three provider shapes all get a handle ===")
    # Compaction prunes all three transcript shapes, and each one's placeholder must carry the handle or that
    # provider silently loses recoverability.
    #
    # Each fixture needs a real turn start. `prune_stale_tool_results` counts turns by finding messages that
    # `_is_turn_start` accepts, and a bare tool result is not one -- so a fixture made only of a tool result
    # yields `current_turn == 0`, hits the `current_turn <= max_age_turns` early return, and prunes nothing. The
    # first version of this test did exactly that and failed for a reason that had nothing to do with handles.
    # The tool result must be in an EARLIER turn than the current one, or `age < max_age_turns` skips it. A
    # message belongs to the turn its preceding user message opened, so `[user, tool_result]` puts the result at
    # age 0 and nothing is pruned. Two turns put it at age 1, which is the real shape: a fresh user message
    # arrives, and the previous turn's oversized output becomes prunable.
    shapes = {
        "openai": [
            {"role": "user", "content": "read it"},
            {"role": "tool", "name": "read_file", "content": "o" * 3_000},
            {"role": "user", "content": "now something else"},
        ],
        "gemini": [
            {"type": "user_input", "content": "read it"},
            {"type": "function_result", "name": "read_file", "result": "g" * 3_000},
            {"type": "user_input", "content": "now something else"},
        ],
        "anthropic": [
            {"role": "user", "content": "read it"},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "tu_9", "content": "a" * 3_000}]},
            {"role": "user", "content": "now something else"},
        ],
    }
    for label, messages3 in shapes.items():
        count = C.prune_stale_tool_results(messages3, max_age_turns=1, min_chars=100)
        check(f"{label}: exactly one result was pruned", count == 1, str(count))
        if label == "anthropic":
            text = messages3[1]["content"][0]["content"]
        elif label == "openai":
            text = messages3[1]["content"]
        else:
            text = messages3[1]["result"]
        check(f"{label}: the placeholder names a handle", HANDLE_RE.search(text) is not None, text[:90])
        found = HANDLE_RE.search(text)
        if found:
            recovered, problem = A.recall(found.group(1), session="s1", root=scratch)
            check(f"{label}: and it resolves to the original",
                  recovered == ("o" * 3_000 if label == "openai"
                                else "g" * 3_000 if label == "gemini" else "a" * 3_000),
                  problem or f"{len(recovered or '')} chars")

    return finish()


def finish() -> int:
    print()
    print(f"=== {len(PASS)} passed, {len(FAIL)} failed ===")
    for label in FAIL:
        print(f"  FAILED: {label}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
