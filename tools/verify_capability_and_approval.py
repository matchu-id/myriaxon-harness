#!/usr/bin/env python3
"""Verify run capabilities and hash-bound approval.

Both mechanisms exist to stop work that should have been refused: acting for a run that has ended, and spending
an approval on something it was not granted for. Most of the checks below assert a *refusal*, because a guard
that has only ever seen valid input has not been tested.
"""

from __future__ import annotations

import sys
import threading
import time

REPO = Path = __import__("pathlib").Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from myriaxon import approvals as A  # noqa: E402
from myriaxon import run_capability as RC  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAIL).append(label)
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}" + (f"\n         {detail}" if detail else ""))


def main() -> int:
    print("=== 1. a live capability permits work ===")
    cap = RC.new_run_capability("locus-run", run_id="r1")
    check("it starts active", cap.active)
    check("assert_active does not raise", True)
    cap.assert_active()
    check("it counts its checks", cap.checks == 1, str(cap.checks))
    check("run_guarded returns the result",
          cap.run_guarded(lambda: 42) == 42)
    check("run_guarded asserts twice", cap.checks == 3, str(cap.checks))

    print()
    print("=== 2. THE FIX: work after the run ends is REFUSED ===")
    closed = RC.new_run_capability("cancelled-run")
    closed.close("the user cancelled")
    check("it is no longer active", not closed.active)
    try:
        closed.assert_active()
        check("assert_active raises after close", False, "no exception")
    except RC.RunRevoked as exc:
        check("assert_active raises after close", True)
        check("the error names the capability", exc.capability == "cancelled-run")
        check("the error carries the reason", "cancelled" in exc.reason, exc.reason)

    print()
    print("=== 3. the argument is checked BEFORE the action, and the result after ===")
    calls: list[str] = []
    live = RC.new_run_capability("race")
    def side_effect():
        calls.append("ran")
        live.close("cancelled mid-action")
        return "result"
    try:
        live.run_guarded(side_effect)
        check("a result produced after cancellation is REJECTED", False, "no exception")
    except RC.RunRevoked:
        check("a result produced after cancellation is REJECTED", True)
    check("the action did run (the check was before it)", calls == ["ran"], str(calls))

    print()
    print("=== 4. closing is idempotent and safe from a finally ===")
    first = live.close("again")
    check("a second close reports it did nothing", first is False)
    check("the original reason is kept", live.reason == "cancelled mid-action", live.reason)

    print()
    print("=== 5. the context manager closes on success AND on failure ===")
    with RC.new_run_capability("ok") as c1:
        pass
    check("closed after a normal exit", not c1.active)
    check("reason says it finished", "finished" in c1.reason, c1.reason)

    c2 = RC.new_run_capability("boom")
    try:
        with c2:
            raise ValueError("inside")
    except ValueError:
        pass
    check("closed after an exception", not c2.active)
    check("reason says it raised", "raised" in c2.reason, c2.reason)

    print()
    print("=== 6. the event fires so a loop can stop promptly ===")
    c3 = RC.new_run_capability("loop")
    event = c3.event()
    check("the event is not set while active", not event.is_set())
    c3.close()
    check("closing sets it", event.is_set())

    print()
    print("=== 7. a guard closure revalidates at the moment of use ===")
    c4 = RC.new_run_capability("prepared-write")
    guard = c4.guard("writing the file")
    guard()
    check("the guard passes while active", True)
    c4.close()
    try:
        guard()
        check("the guard fails after close", False, "no exception")
    except RC.RunRevoked:
        check("the guard fails after close", True)

    print()
    print("=== 8. a concurrent thread cannot act through a closed capability ===")
    # Deterministic, not a race. The first version had the worker spin 200 times while the main thread slept
    # 10 ms, so the worker simply finished first and the test failed on its own timing rather than on a defect.
    # A barrier makes the ordering explicit: the worker is released only after close() has returned.
    c5 = RC.new_run_capability("threaded")
    released = threading.Event()
    outcome: list[str] = []

    def worker():
        released.wait(timeout=5)
        try:
            c5.assert_active()
            outcome.append("acted")
        except RC.RunRevoked:
            outcome.append("refused")

    thread = threading.Thread(target=worker)
    thread.start()
    c5.close("cancelled from the main thread")
    released.set()
    thread.join(timeout=5)

    check("the worker finished", not thread.is_alive())
    check("it was REFUSED rather than allowed to act", outcome == ["refused"], str(outcome))
    check("and a second attempt is refused too", not c5.active)

    print()
    print("=== 9. approval replies are classified without a model call ===")
    for reply, expected in (
        ("yes", A.ALLOW_ONCE), ("y", A.ALLOW_ONCE), ("Approve", A.ALLOW_ONCE),
        ("go ahead", A.ALLOW_ONCE), ("do it.", A.ALLOW_ONCE), ("sounds good", A.ALLOW_ONCE),
        ("no", A.DENY), ("nope", A.DENY), ("cancel", A.DENY), ("don't", A.DENY),
        ("not now", A.DENY),
        # The dangerous misreads: a decline that contains an approval word must not be read as assent.
        ("no, go ahead", A.DENY), ("don't apply this", A.DENY), ("stop, yes I know", A.DENY),
        ("maybe later", ""), ("what does this do?", ""), ("", ""),
    ):
        check(f"{reply!r} -> {expected or 'no decision'}", A.classify_reply(reply) == expected,
              f"got {A.classify_reply(reply)!r}")

    print()
    print("=== 10. THE FIX: an approval is bound to one exact operation ===")
    A.clear()
    op = {"action": "publish", "version": "1.2.3"}
    pending = A.request("Publish the release", op, detail="tag v1.2.3")
    check("it is pending", len(A.pending()) == 1)
    check("a different operation has a different fingerprint",
          A.operation_hash({"version": "1.2.3", "action": "publish"}) == A.operation_hash(op),
          "key order must not change the hash")
    check("changing the version changes the hash",
          A.operation_hash({"action": "publish", "version": "9.9.9"}) != A.operation_hash(op))

    wrong = A.decide(pending.approval_id, "yes", operation={"action": "publish", "version": "9.9.9"})
    check("approving a DIFFERENT operation is refused", not wrong.approved)
    check("it is reported as unusable rather than unknown", wrong.unusable)
    check("the reason names the mismatch", "different operation" in wrong.reason, wrong.reason)

    right = A.decide(pending.approval_id, "yes", operation=op)
    check("approving the exact operation works", right.approved)
    check("the decision is allow-once", right.decision == A.ALLOW_ONCE)

    print()
    print("=== 11. THE FIX: a decision cannot be replayed ===")
    again = A.decide(pending.approval_id, "yes", operation=op)
    check("a second decision on the same approval is refused", not again.approved)
    check("and says it was already used", "already" in again.reason, again.reason)

    spent = A.spend(pending.approval_id, operation=op)
    check("the granted approval can be spent once", spent.approved)
    twice = A.spend(pending.approval_id, operation=op)
    check("spending it twice is refused", not twice.approved)
    check("the second spend says so", "spent" in twice.reason, twice.reason)

    print()
    print("=== 12. spending re-checks the operation ===")
    A.clear()
    p2 = A.request("Delete the Locus", {"action": "delete-locus", "name": "reviewer"})
    A.decide(p2.approval_id, "yes", operation={"action": "delete-locus", "name": "reviewer"})
    mismatch = A.spend(p2.approval_id, operation={"action": "delete-locus", "name": "analyst"})
    check("spending against a different target is refused", not mismatch.approved)
    check("the reason says it does not match", "does not match" in mismatch.reason, mismatch.reason)

    print()
    print("=== 13. an approval expires rather than staying live forever ===")
    A.clear()
    p3 = A.request("Do something", {"x": 1}, ttl_seconds=0.05)
    time.sleep(0.12)
    expired = A.decide(p3.approval_id, "yes", operation={"x": 1})
    check("deciding an expired approval is refused", not expired.approved)
    check("it says it expired", "expired" in expired.reason, expired.reason)
    check("and it is not left pending", A.pending() == [])

    print()
    print("=== 14. a denial is a real decision, not an absence ===")
    A.clear()
    p4 = A.request("Risky thing", {"y": 2})
    denied = A.decide(p4.approval_id, "no", operation={"y": 2})
    check("the denial is recorded", denied.decision == A.DENY)
    check("it is not approved", not denied.approved)
    check("it cannot then be spent", not A.spend(p4.approval_id, operation={"y": 2}).approved)

    print()
    print("=== 15. before_acting orders capability, then approval, then action ===")
    A.clear()
    order: list[str] = []
    cap2 = RC.new_run_capability("gated")
    p5 = A.request("Act", {"z": 3})
    A.decide(p5.approval_id, "yes", operation={"z": 3})

    ok, result, reason = A.before_acting(
        p5.approval_id, {"z": 3}, lambda: (order.append("acted"), "done")[1],
        require_live_capability=cap2,
    )
    check("it acted", ok and result == "done" and order == ["acted"], reason)

    p6 = A.request("Act again", {"z": 4})
    A.decide(p6.approval_id, "yes", operation={"z": 4})
    cap2.close("cancelled")
    ok2, _, reason2 = A.before_acting(p6.approval_id, {"z": 4}, lambda: "should not run",
                                      require_live_capability=cap2)
    check("a dead run is refused before the approval is spent", not ok2)
    check("the reason names the run", "no longer active" in reason2, reason2)
    check("the approval was NOT spent by the refusal",
          A.get(p6.approval_id).terminal_reason != "spent", A.get(p6.approval_id).terminal_reason)

    print()
    print("=== 16. before_acting with no approval does not act ===")
    A.clear()
    ok3, _, reason3 = A.before_acting("a-doesnotexist", {}, lambda: "nope")
    check("an unknown approval is refused", not ok3)
    check("the reason says so", "no approval" in reason3, reason3)

    print()
    print("=== 17. a failing action is reported, not swallowed ===")
    A.clear()
    p7 = A.request("Will fail", {"w": 5})
    A.decide(p7.approval_id, "yes", operation={"w": 5})

    def boom():
        raise RuntimeError("the action failed")
    ok4, _, reason4 = A.before_acting(p7.approval_id, {"w": 5}, boom)
    check("the failure is reported", not ok4)
    check("the reason names the exception", "RuntimeError" in reason4, reason4)
    check("the approval was still spent, so it cannot be replayed",
          A.get(p7.approval_id).terminal_reason == "spent")

    print()
    print("=== 18. WIRING: the capability and detector are reachable from a ToolContext ===")
    # A guard nothing constructs is a library, not a guard. These assert the integration points, not just that
    # the classes work in isolation.
    from myriaxon.tool_context import ToolContext

    fields = set(ToolContext.__dataclass_fields__)
    check("ToolContext carries a run_capability field", "run_capability" in fields)
    check("ToolContext carries a loop_detector field", "loop_detector" in fields)
    check("both default to None, so existing callers are unaffected",
          ToolContext.__dataclass_fields__["run_capability"].default is None
          and ToolContext.__dataclass_fields__["loop_detector"].default is None)

    print()
    print("=== 19. WIRING: the dispatch path feeds the detector and prints the verdict ===")
    import io as _io

    from rich.console import Console as _Console

    from myriaxon import loop_detection as _L
    from myriaxon.tool_registry import _record_for_loop_detection

    class _Ctx:
        def __init__(self) -> None:
            self.console = _Console(file=_io.StringIO())
            self.loop_detector = _L.LoopDetector()

    ctx = _Ctx()
    for _ in range(_L.WARNING_THRESHOLD):
        _record_for_loop_detection("read_file", {"path": "a.py"}, "FileNotFoundError: a.py",
                                   failed=True, ctx=ctx)
    check("every call was recorded", len(ctx.loop_detector.history) == _L.WARNING_THRESHOLD,
          str(len(ctx.loop_detector.history)))
    printed = ctx.console.file.getvalue()
    check("the verdict was printed, not swallowed", "repeat" in printed, printed.strip()[:80])

    print()
    print("=== 20. WIRING: a broken detector never becomes a broken tool call ===")
    class _Broken(_L.LoopDetector):
        def record(self, *a, **k):
            raise RuntimeError("the detector is broken")

    ctx2 = _Ctx()
    ctx2.loop_detector = _Broken()
    try:
        _record_for_loop_detection("read_file", {}, "result", failed=False, ctx=ctx2)
        check("a raising detector is swallowed", True)
    except Exception as exc:  # noqa: BLE001
        check("a raising detector is swallowed", False, repr(exc))

    print()
    print("=== 21. WIRING: edit_file refuses an empty span through the real tool ===")
    # The concrete bug this closes: `content.replace("", new)` splices `new` in front of every character, and
    # the old inline guard tested `count == 0`, which is false for an empty string -- so it never fired.
    from myriaxon import tools as _tools

    class _EditCtx:
        yolo = True
        settings = None
        mode = "auto"
        current_turn_message_count = 0

        def __init__(self) -> None:
            self.console = _Console(file=_io.StringIO())
            self.touched_paths_this_turn: set = set()

    probe = REPO / ".edit_wiring_probe.py"
    original = "def a():\n    return 1\n"
    probe.write_text(original, encoding="utf-8")
    try:
        message, failed = _tools._edit_file(
            {"path": str(probe), "old_string": "", "new_string": "SPLICE"}, _EditCtx()
        )
        check("an empty old_string is refused", failed, message[:70])
        check("and the file is untouched", probe.read_text(encoding="utf-8") == original,
              repr(probe.read_text(encoding="utf-8")[:40]))

        message, failed = _tools._edit_file(
            {"path": str(probe), "old_string": "        return 1", "new_string": "x"}, _EditCtx()
        )
        check("an indentation near-miss is refused with a hint",
              failed and "whitespace" in message, message[:120])

        message, failed = _tools._edit_file(
            {"path": str(probe), "old_string": "    return 1\n", "new_string": "    return 9\n"},
            _EditCtx(),
        )
        check("a well-formed edit is applied", not failed, message[:60])
        check("and it took effect", "return 9" in probe.read_text(encoding="utf-8"))
    finally:
        probe.unlink(missing_ok=True)

    print()
    print(f"=== {len(PASS)} passed, {len(FAIL)} failed ===")
    for label in FAIL:
        print(f"  FAILED: {label}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
