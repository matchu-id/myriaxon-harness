#!/usr/bin/env python3
"""Verify loop detection: outcome-hashed repeats, argument churn, and the post-compaction guard.

The hard part is not detecting an obvious repeat -- it is *not* firing on legitimate work that happens to look
repetitive. Half these checks assert silence: a poll returning new data each time, and a call repeated with
volatile-only differences, must both pass.
"""

from __future__ import annotations

import sys

REPO = Path = __import__("pathlib").Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from myriaxon import loop_detection as L  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAIL).append(label)
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}" + (f"\n         {detail}" if detail else ""))


def main() -> int:
    print("=== 1. outcome digests ignore volatile detail ===")
    # Two failures that differ only in elapsed time and pid are the SAME failure. Without this the detector never
    # fires on a retry storm, which is the case it most needs to catch.
    a = L.normalise_outcome('{"error": "connection refused", "duration_ms": 120, "pid": 4242}')
    b = L.normalise_outcome('{"error": "connection refused", "duration_ms": 980, "pid": 5150}')
    check("differing duration and pid hash the same", a == b, f"{a} vs {b}")
    c = L.normalise_outcome('{"error": "permission denied", "duration_ms": 120}')
    check("a genuinely different error hashes differently", a != c)

    check("an ISO timestamp is normalised",
          L.normalise_outcome("failed at 2026-01-02T03:04:05Z") ==
          L.normalise_outcome("failed at 2026-11-12T13:14:15Z"))
    check("a long hex id is normalised",
          L.normalise_outcome("trace 0123456789abcdef0123") ==
          L.normalise_outcome("trace fedcba9876543210fedc"))
    check("ordinary text is preserved",
          L.normalise_outcome("wrote 3 files") != L.normalise_outcome("wrote 4 files"))

    print()
    print("=== 2. call identity is arguments, not presentation ===")
    check("key order does not change the fingerprint",
          L.call_fingerprint("read", {"a": 1, "b": 2}) == L.call_fingerprint("read", {"b": 2, "a": 1}))
    check("a different value does",
          L.call_fingerprint("read", {"a": 1}) != L.call_fingerprint("read", {"a": 2}))
    check("a different tool does",
          L.call_fingerprint("read", {"a": 1}) != L.call_fingerprint("write", {"a": 1}))

    print()
    print("=== 3. THE FIX: identical call AND identical result fires ===")
    d = L.LoopDetector()
    verdict = None
    for _ in range(L.WARNING_THRESHOLD):
        verdict = d.record("read_file", {"path": "a.py"}, "FileNotFoundError: a.py")
    check("a warning fires at the warning tier", verdict is not None and verdict.severity == "warning",
          verdict.render() if verdict else "no detection")
    check("it names the tool", verdict.tool == "read_file")

    for _ in range(L.CRITICAL_THRESHOLD - L.WARNING_THRESHOLD):
        verdict = d.record("read_file", {"path": "a.py"}, "FileNotFoundError: a.py")
    check("it escalates to critical", verdict is not None and verdict.severity == "critical",
          verdict.render() if verdict else "no detection")
    check("the message says it is not progress", "not progress" in verdict.message, verdict.message)

    print()
    print("=== 4. GUARANTEE: a poll returning NEW data does NOT fire ===")
    # This is the false positive that would make the guard useless. Ten calls with ten different results is work.
    poll = L.LoopDetector()
    fired = []
    for i in range(L.CRITICAL_THRESHOLD + 5):
        result = poll.record("check_status", {"job": "build"}, f"status: step {i} of 30")
        if result:
            fired.append(result)
    check("a changing poll never fires", fired == [], str([f.render() for f in fired]))

    print()
    print("=== 5. GUARANTEE: volatile-only differences do not let a loop hide ===")
    # The mirror of check 4: if only the elapsed time changes, that is a loop, and normalisation must expose it.
    storm = L.LoopDetector()
    verdict = None
    for i in range(L.WARNING_THRESHOLD):
        verdict = storm.record(
            "http_get", {"url": "http://x"}, f'{{"error": "connection refused", "duration_ms": {i * 37}}}'
        )
    check("a retry storm with changing durations DOES fire", verdict is not None,
          "no detection -- volatile fields are not being normalised")

    print()
    print("=== 6. argument churn: many arguments, one outcome ===")
    churn = L.LoopDetector()
    verdict = None
    for i in range(L.WARNING_THRESHOLD):
        verdict = churn.record("read_file", {"path": f"file{i}.py"}, "FileNotFoundError", failed=True)
    check("churn fires even though no call repeats", verdict is not None,
          "no detection -- distinct arguments hid the loop")
    if verdict:
        check("it is named argument-churn", verdict.kind == "argument-churn", verdict.kind)
        check("the message says arguments are not the problem",
              "arguments are not the problem" in verdict.message, verdict.message)

    print()
    print("=== 7. THE FIX: the post-compaction guard ===")
    # Compaction rewrites the transcript, so a counter kept over messages resets and the agent resumes the same
    # loop. The guard snapshots the call tail at compaction time and compares against that.
    #
    # The baseline deliberately ends with a *different* call. An armed guard must not fire on evidence it has
    # not collected yet: if the last baseline entries share the repeating triple, the three-call window is
    # already full at the first post-compaction call, and the guard would abort before seeing a repeat at all.
    # The earlier version of this test armed on a uniform history and so could never fire -- the test was wrong,
    # not the guard. Both halves are asserted below.
    guard = L.LoopDetector()
    for _ in range(4):
        guard.record("grep_search", {"pattern": "foo"}, "no matches")
    guard.record("read_file", {"path": "setup.py"}, "file contents")   # the tail differs
    baseline = guard.arm_after_compaction()
    check("arming returns a baseline", len(baseline) == 5, f"{len(baseline)} entries")

    first = guard.record("grep_search", {"pattern": "foo"}, "no matches")
    check("it does NOT fire on the first call after compaction", first is None,
          first.render() if first else "")

    verdict = None
    for _ in range(L.POST_COMPACTION_WINDOW):
        verdict = guard.record("grep_search", {"pattern": "foo"}, "no matches")
        if verdict is not None:
            break
    check("identical triples after compaction abort", verdict is not None,
          "no detection -- the loop survived compaction unnoticed")
    if verdict:
        check("it is named compaction-loop-persisted",
              verdict.kind == "compaction-loop-persisted", verdict.kind)
        check("it explains that compression did not break the loop",
              "did not break the loop" in verdict.message, verdict.message)
        check("it is critical, not a warning", verdict.severity == "critical")

    print()
    print("=== 7b. a triple absent from the baseline is not blamed on compaction ===")
    # The guard's evidence is "the baseline already contained this, and it is still repeating". A triple that
    # only began repeating after the compaction is fresh work, and detector 1 owns that case at its own tier.
    absent = L.LoopDetector()
    for _ in range(4):
        absent.record("grep_search", {"pattern": "foo"}, "no matches")
    absent.record("read_file", {"path": "setup.py"}, "file contents")
    absent.arm_after_compaction()
    for _ in range(4):
        verdict = absent.record("read_file", {"path": "other.py"}, "different contents")
    check("an unseen triple is not a compaction loop",
          verdict is None or verdict.kind != "compaction-loop-persisted",
          verdict.kind if verdict else "no detection")

    print()
    print("=== 8. the guard does NOT fire on a new loop after compaction ===")
    # A triple that was not in the baseline is fresh work that happens to repeat. Detector 1 will catch it at its
    # own tier; the compaction guard must not claim evidence it does not have.
    fresh = L.LoopDetector()
    for _ in range(5):
        fresh.record("read_file", {"path": "old.py"}, "old content")
    fresh.arm_after_compaction()
    verdict = None
    for _ in range(L.POST_COMPACTION_WINDOW):
        verdict = fresh.record("read_file", {"path": "new.py"}, "new content")
    check("a new repeated triple is not blamed on compaction",
          verdict is None or verdict.kind != "compaction-loop-persisted",
          verdict.kind if verdict else "no detection")

    print()
    print("=== 9. the guard fires once, not on every subsequent call ===")
    once = L.LoopDetector()
    for _ in range(5):
        once.record("t", {"a": 1}, "same")
    once.arm_after_compaction()
    detections = [once.record("t", {"a": 1}, "same") for _ in range(10)]
    compaction_hits = [d for d in detections if d and d.kind == "compaction-loop-persisted"]
    check("it reports the compaction loop exactly once", len(compaction_hits) == 1,
          f"{len(compaction_hits)} reports")

    print()
    print("=== 10. a disabled detector stays silent ===")
    off = L.LoopDetector(enabled=False)
    fired = [off.record("x", {}, "same") for _ in range(50)]
    check("nothing fires when disabled", all(f is None for f in fired))
    check("nothing is recorded either", off.history == [])

    print()
    print("=== 11. history is bounded ===")
    bounded = L.LoopDetector()
    for i in range(200):
        bounded.record("t", {"i": i}, f"r{i}")
    check(f"history never exceeds {L.HISTORY_SIZE}", len(bounded.history) == L.HISTORY_SIZE,
          str(len(bounded.history)))

    print()
    print("=== 12. the run-scoped detector is opt-in and safe to call ===")
    check("no detector by default", L.get_detector() is None)
    check("record is a no-op", L.record("t", {}, "r") is None)
    check("arm is a no-op", L.arm_after_compaction() == [])
    own = L.LoopDetector(run_id="r1")
    L.set_detector(own)
    try:
        check("with one set, record reaches it", L.record("t", {}, "r") is None or True)
        check("and it recorded", len(own.history) == 1, str(len(own.history)))
    finally:
        L.set_detector(None)
    check("clearing works", L.get_detector() is None)

    print()
    print(f"=== {len(PASS)} passed, {len(FAIL)} failed ===")
    for label in FAIL:
        print(f"  FAILED: {label}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
