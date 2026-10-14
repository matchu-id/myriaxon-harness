#!/usr/bin/env python3
"""Verify tool-result spill.

The claim under test is that a spilled result is *recoverable*, not merely smaller. So these checks read the
spilled file back and compare it to the original byte for byte -- a preview that names a file the model cannot
actually read would be worse than the truncation it replaced.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from myriaxon import spill as S  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAIL).append(label)
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}" + (f"\n         {detail}" if detail else ""))


def main() -> int:
    root = Path(tempfile.mkdtemp())

    print("=== 1. a small result is untouched ===")
    small = "a short result"
    outcome = S.spill(small, root=root, max_inline_tokens=1000)
    check("nothing was spilled", not outcome.spilled)
    check("the content is byte-identical", outcome.content == small)
    check("there is no record", outcome.record is None)
    check("and no warnings", outcome.warnings == [])
    check("no file was written", not any(root.rglob("*.txt")))

    print()
    print("=== 2. THE FIX: an oversized result is spilled and the preview names the file ===")
    huge = "\n".join(f"line {i}: {'x' * 60}" for i in range(4000))
    outcome = S.spill(huge, tool_name="grep_search", call_id="call_1", session_id="s1",
                      root=root, max_inline_tokens=500)
    check("it was spilled", outcome.spilled, str(outcome.warnings))
    check("a record came back", outcome.record is not None)
    check("the preview is within budget",
          S.estimate_tokens(outcome.content) <= 500,
          f"{S.estimate_tokens(outcome.content)} tokens vs a 500 budget")
    check("the preview names the file", str(outcome.record.path) in outcome.content)
    check("it says how to read it", "read_file" in outcome.content)
    check("the head survived", "line 0:" in outcome.content)
    check("the tail survived", "line 3999:" in outcome.content)
    check("the middle is marked as omitted", "middle omitted" in outcome.content)
    check("the record reports the shrink",
          outcome.record.retained_tokens < outcome.record.original_tokens,
          f"{outcome.record.original_tokens} -> {outcome.record.retained_tokens}")

    print()
    print("=== 3. RECOVERABILITY: the spilled file is the complete original ===")
    # The whole point. If this fails, the preview names a file that does not hold what it claims to.
    stored = Path(outcome.record.path)
    check("the file exists", stored.is_file())
    check("it holds every byte of the original", stored.read_text(encoding="utf-8") == huge,
          f"{stored.stat().st_size} bytes on disk vs {len(huge.encode('utf-8'))} expected")

    print()
    print("=== 4. the budget is respected across a range of sizes ===")
    for budget in (200, 500, 1000, 3000):
        sized = S.spill(huge, tool_name="t", session_id="s", root=root, max_inline_tokens=budget)
        got = S.estimate_tokens(sized.content)
        check(f"a {budget}-token budget is respected", got <= budget, f"{got} tokens")

    print()
    print("=== 5. THE FIX: a failure leaves the ORIGINAL content, not a truncated one ===")
    # A spill failure must not make a storage problem look like a result problem.
    blocked = root / "not-a-directory"
    blocked.write_text("I am a file", encoding="utf-8")
    outcome = S.spill(huge, tool_name="t", session_id="s", root=blocked, max_inline_tokens=200)
    check("it reports that it did not spill", not outcome.spilled)
    check("the content is the full original", outcome.content == huge)
    check("a warning explains why", bool(outcome.warnings), str(outcome.warnings))
    check("the warning names the path", "not-a-directory" in outcome.warnings[0], outcome.warnings[0])

    print()
    print("=== 6. a budget too small for a USABLE preview returns the original ===")
    # The floor is on retained content, not just on the notice. Without it a small budget yields a notice with a
    # few characters attached, which tells the model less than the original did while still costing a file read.
    outcome = S.spill(huge, tool_name="t", session_id="s", root=root, max_inline_tokens=10)
    check("nothing was spilled", not outcome.spilled)
    check("the original came back intact", outcome.content == huge)
    check("and it says the budget was the problem",
          "cannot hold a usable spill preview" in outcome.warnings[0], outcome.warnings[0])
    check("the warning names the minimum it would need",
          "needs at least" in outcome.warnings[0], outcome.warnings[0])

    print()
    print("=== 7. code points are never split ===")
    # Emoji beyond the BMP are surrogate pairs in UTF-16. Python strings are code points, so a slice is safe --
    # this pins the guarantee so a future bytes-level optimisation cannot quietly break it.
    emoji = "🎉" * 2000
    outcome = S.spill(emoji, tool_name="t", session_id="s", root=root, max_inline_tokens=100)
    if outcome.spilled:
        check("every retained character is a whole code point",
              all(ch == "🎉" or ch in "\n[].;: " or ch.isascii() or ord(ch) > 0xFFFF
                  for ch in outcome.content),
              repr(outcome.content[:40]))
        check("no replacement character appeared", "\ufffd" not in outcome.content)
        check("the stored file round-trips", Path(outcome.record.path).read_text(encoding="utf-8") == emoji)
    else:
        check("the emoji case spilled", False, str(outcome.warnings))

    print()
    print("=== 8. code-point slicing cannot raise on an astral boundary ===")
    for budget in range(50, 300, 7):
        try:
            S.spill(emoji, tool_name="t", session_id="s", root=root, max_inline_tokens=budget)
        except Exception as exc:  # noqa: BLE001
            check(f"budget {budget} did not raise", False, repr(exc))
            break
    else:
        check("every budget in the sweep was handled", True)

    print()
    print("=== 9. spilling is content-addressed, so a retry does not multiply files ===")
    once = S.spill(huge, tool_name="t", call_id="c", session_id="s", root=root, max_inline_tokens=200)
    twice = S.spill(huge, tool_name="t", call_id="c", session_id="s", root=root, max_inline_tokens=200)
    check("the same content and call produce the same path", once.record.path == twice.record.path,
          f"{once.record.path} vs {twice.record.path}")
    different = S.spill(huge + "extra", tool_name="t", call_id="c", session_id="s",
                        root=root, max_inline_tokens=200)
    check("different content produces a different path",
          different.record.path != once.record.path)

    print()
    print("=== 10. different sessions get different directories ===")
    a = S.spill(huge, tool_name="t", session_id="session-a", root=root, max_inline_tokens=200)
    b = S.spill(huge, tool_name="t", session_id="session-b", root=root, max_inline_tokens=200)
    check("two sessions spill to different paths", a.record.path != b.record.path)
    check("the path does not contain the raw session id",
          "session-a" not in a.record.path, a.record.path)

    print()
    print("=== 11. an image block becomes an actionable reference ===")
    text = S.spill_image_reference("C:/tmp/x.png", "image/png", 800, 600)
    check("it names the path", "x.png" in text)
    check("it carries the mime and size", "image/png" in text and "800x600" in text)
    check("it says how to view it", "read_image" in text, text)
    bare = S.spill_image_reference("C:/tmp/y.png")
    check("it works without optional detail", "y.png" in bare)

    print()
    print("=== 12. SECURITY: a locator cannot escape the spill root ===")
    # The locator travelled through the model's context, so it is untrusted input.
    for hostile in (
        str(root / ".." / ".." / "etc" / "passwd"),
        "C:/Windows/System32/drivers/etc/hosts",
        "../../../etc/shadow",
        "",
        "not-a-real-file-anywhere",
    ):
        check(f"refuses {hostile[:38]!r}", S.resolve(hostile) is None)
    good = S.spill(huge, tool_name="t", session_id="s", root=root, max_inline_tokens=200)
    # `resolve` is anchored at the configured `S.SPILL_ROOT`, and this test spills into a scratch root, so a
    # scratch locator is legitimately refused. The containment rule is what is under test here, not the scratch
    # path -- asserting otherwise would have been asserting the wrong thing.
    check("a scratch-root locator is refused because it is outside the live root",
          S.resolve(good.record.path) is None,
          "containment is anchored at the configured spill root, not at a caller-supplied one")
    check("a locator inside the live root but naming no file is refused",
          S.resolve(str(S.SPILL_ROOT / "does-not-exist.txt")) is None)

    print()
    print("=== 13. the sweep respects retention and refuses to remove the root ===")
    old = root / "session-old"
    old.mkdir(parents=True, exist_ok=True)
    stale = old / "stale.txt"
    stale.write_text("old", encoding="utf-8")
    ancient = time.time() - 90 * 86_400
    os.utime(stale, (ancient, ancient))
    fresh = old / "fresh.txt"
    fresh.write_text("new", encoding="utf-8")

    report = S.sweep(root, retention_days=30)
    check("the sweep examined files", report["examined"] >= 2, str(report))
    check("the stale file was removed", not stale.exists())
    check("the fresh file was kept", fresh.exists())
    check("the root still exists", root.is_dir())
    check("the report counts what it did", report["removed"] == 1, str(report))

    print()
    print("=== 14. a non-string or empty result is passed through ===")
    check("empty content is returned unchanged", S.spill("", root=root).content == "")
    check("a non-string is coerced", S.spill(123, root=root).content == "123")

    print()
    print("=== 15. pricing is stable and monotonic ===")
    check("a longer string prices higher",
          S.estimate_tokens("x" * 400) > S.estimate_tokens("x" * 40))
    check("pricing never returns zero",
          S.estimate_tokens("") >= 1, "a zero price would make every budget check pass")

    print()
    print(f"=== {len(PASS)} passed, {len(FAIL)} failed ===")
    for label in FAIL:
        print(f"  FAILED: {label}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
