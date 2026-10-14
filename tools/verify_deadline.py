#!/usr/bin/env python3
"""Verify per-tool cooperative deadlines.

The claim under test is not "a slow tool is stopped" -- Python cannot stop a thread. It is that a deadline is
reported **only when that deadline is what expired**, which is what stops a timeout being blamed for a
cancellation.
"""

from __future__ import annotations

import sys
import threading
import time

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from myriaxon import deadline as D  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAIL).append(label)
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}" + (f"\n         {detail}" if detail else ""))


def main() -> int:
    print("=== 1. a fast tool is untouched ===")
    outcome = D.run_with_deadline(lambda: "done", seconds=5.0, tool="read_file")
    check("it ran", outcome.ok, str(outcome.error))
    check("the result is exactly what the tool returned", outcome.result == "done")
    check("it is not marked as a timeout", not outcome.timed_out)
    check("the deadline is recorded for reporting", outcome.deadline is not None)
    check("and it did NOT fire", not outcome.deadline.fired)

    print()
    print("=== 2. THE FIX: a slow tool is abandoned and the result says so ===")
    started = time.perf_counter()
    outcome = D.run_with_deadline(lambda: time.sleep(30) or "never", seconds=0.25, tool="slow_tool")
    elapsed = time.perf_counter() - started
    check("the call returned promptly rather than waiting", elapsed < 3.0, f"{elapsed:.2f}s")
    check("it is marked as a timeout", outcome.timed_out)
    check("the deadline records that IT fired", outcome.deadline.fired)
    check("the result names the budget", "0s" in str(outcome.result) or "0" in str(outcome.result),
          str(outcome.result)[:120])
    check("the result names the tool", "slow_tool" in str(outcome.result), str(outcome.result)[:120])
    check("is_timeout recognises it", D.is_timeout(str(outcome.result)))

    print()
    print("=== 3. the result is HONEST about cooperative cancellation ===")
    # Python cannot kill a thread. A caller told the tool was cancelled would wrongly assume the workspace is
    # untouched, so the text must say the work may still land.
    text = str(outcome.result)
    check("it says the call was abandoned, not cancelled", "abandoned, not cancelled" in text, text[:200])
    check("it says the tool may still be running", "may still be running" in text)
    check("it says side effects may still land", "side effects may still land" in text)
    check("it says what to do instead", "Narrow the work" in text)
    check("it warns to check the workspace", "check the workspace" in text)

    print()
    print("=== 4. THE MISATTRIBUTION GUARD: an outside abort is NOT reported as a tool timeout ===")
    # This is the idea worth copying. A timeout reported for the wrong cause sends the reader looking in the
    # wrong place, so the verdict is withheld when an outside cause is known to have aborted first.
    cancelled = threading.Event()
    cancelled.set()
    outcome = D.run_with_deadline(
        lambda: time.sleep(30), seconds=0.25, tool="slow_tool",
        outer_aborted=cancelled.is_set,
    )
    check("it is NOT marked as a timeout", not outcome.timed_out,
          "an outside cause aborted first, so blaming the deadline would be wrong")
    check("the deadline did not claim to fire", not outcome.deadline.fired)
    check("no timeout text was produced", not D.is_timeout(str(outcome.result)))

    print()
    print("=== 5. an unreadable abort flag falls back to the timeout verdict ===")
    # The deadline DID elapse; claiming an outside cause we could not confirm would be the misattribution.
    def broken() -> bool:
        raise RuntimeError("cannot read the abort flag")

    outcome = D.run_with_deadline(lambda: time.sleep(30), seconds=0.25, tool="t", outer_aborted=broken)
    check("the timeout verdict stands", outcome.timed_out, "an unconfirmable abort must not suppress it")

    print()
    print("=== 6. a genuine tool failure is reported as ITSELF, never as a timeout ===")
    def explodes() -> str:
        raise ValueError("the tool is broken")

    outcome = D.run_with_deadline(explodes, seconds=5.0, tool="broken")
    check("the error is carried through", isinstance(outcome.error, ValueError), repr(outcome.error))
    check("it is not marked as a timeout", not outcome.timed_out)
    check("an error outcome is not ok", not outcome.ok)

    print()
    print("=== 7. THE PASSTHROUGH: no declared budget means no deadline ===")
    # Their rule: a tool with no declared budget is untouched. Inventing one would be a behaviour change
    # disguised as a safety feature.
    for absent in (None, 0, -1):
        outcome = D.run_with_deadline(lambda: "ran", seconds=absent, tool="no_budget")
        check(f"seconds={absent} runs inline and returns the result", outcome.result == "ran")
        check(f"seconds={absent} never reports a timeout", not outcome.timed_out)

    print()
    print("=== 8. a tool that ignores its deadline PROVABLY keeps running ===")
    # Not a defect to be hidden -- the documented consequence of cooperative cancellation. Asserted so the
    # docstring cannot quietly become false.
    marker: list[str] = []

    def ignores_deadline() -> None:
        time.sleep(1.0)
        marker.append("finished anyway")

    outcome = D.run_with_deadline(ignores_deadline, seconds=0.1, tool="stubborn")
    check("the harness stopped waiting", outcome.timed_out)
    check("the tool had not finished yet", marker == [], str(marker))
    time.sleep(1.2)
    check("and it finished anyway, afterwards", marker == ["finished anyway"],
          "cooperative cancellation means the work still lands")

    print()
    print("=== 9. the abandonment is COUNTED, not silent ===")
    before = D.abandoned_count()
    D.run_with_deadline(lambda: time.sleep(5), seconds=0.1, tool="t")
    check("the counter advanced", D.abandoned_count() > before,
          f"{before} -> {D.abandoned_count()}")

    print()
    print("=== 10. declared_timeout reads both schema shapes and both units ===")
    check("a top-level timeout_s is read",
          D.declared_timeout({"timeout_s": 30}) == 30.0)
    check("a nested function shape is read",
          D.declared_timeout({"function": {"timeout_s": 12}}) == 12.0)
    check("timeoutMs is CONVERTED to seconds",
          D.declared_timeout({"timeoutMs": 30_000}) == 30.0,
          "a silent unit mismatch would turn a 30-second budget into 30 milliseconds")
    check("a nested input_schema is searched too",
          D.declared_timeout({"input_schema": {"timeout_seconds": 5}}) == 5.0)
    check("a definition with no budget returns None", D.declared_timeout({"name": "x"}) is None)
    check("a non-dict returns None", D.declared_timeout("not a dict") is None)
    check("zero is treated as no budget, not as instantaneous",
          D.declared_timeout({"timeout_s": 0}) is None)
    check("a negative is treated as no budget",
          D.declared_timeout({"timeout_s": -5}) is None)

    print()
    print("=== 11. tools that must never get an automatic deadline are named ===")
    # Each of these legitimately runs until a condition the harness cannot predict; a deadline would break the
    # feature rather than the tool.
    for tool in ("ask_user_question", "task", "parallel_tasks", "send_to_subagent", "schedule_prompt"):
        check(f"{tool} is exempt", tool in D.NO_DEADLINE)
        check(f"{tool}'s exemption has a reason", bool(D.NO_DEADLINE[tool]))

    print()
    print("=== 12. concrete tools declare a usable budget ===")
    from myriaxon import tool_registry as TR

    declared = {}
    for definition in TR.build_tool_defs():
        name = definition.get("name") or (definition.get("function") or {}).get("name") or ""
        value = D.declared_timeout(definition, name)
        if value:
            declared[name] = value
    print(f"         declared budgets: {declared or 'none'}")
    # Reported rather than asserted: whether a tool declares a budget is a policy choice, and this states the
    # current answer instead of inventing a requirement.
    check("declared_timeout is safe to call on every live definition", True,
          f"{len(declared)} of {len(TR.build_tool_defs())} tools declare one")

    print()
    print("=== 13. the shared pool is bounded ===")
    # A wedged tool must not spawn unboundedly many abandoned workers.
    check("there is a cap on abandoned workers", D.MAX_ABANDONED_WORKERS >= 1,
          str(D.MAX_ABANDONED_WORKERS))
    for _ in range(3):
        D.run_with_deadline(lambda: time.sleep(5), seconds=0.05, tool="t")
    check("several abandonments do not raise", True)

    print()
    print("=== 14. WIRING: the real dispatch path enforces a deadline ===")
    # The module working is not the claim; `dispatch_tool` applying it is. The inner dispatch is replaced for one
    # tool so the deadline is what is under test rather than the network.
    import io as _io

    from rich.console import Console as _Console

    from myriaxon import tool_registry as TR

    real_inner = TR._dispatch_tool_inner

    def wedged(name, tool_input, ctx):
        if name == "web_fetch":
            time.sleep(30)
            return "never returned", False
        return real_inner(name, tool_input, ctx)

    class _Ctx:
        bench_trace = None
        current_tool_name = ""

        def __init__(self) -> None:
            self.console = _Console(file=_io.StringIO())

    original_budget = TR.TOOL_DEADLINE_SECONDS.get("web_fetch")
    TR._dispatch_tool_inner = wedged
    TR.TOOL_DEADLINE_SECONDS["web_fetch"] = 0.3
    try:
        ctx = _Ctx()
        before = D.abandoned_count()
        started = time.perf_counter()
        content, is_error = TR.dispatch_tool("web_fetch", {"url": "http://x"}, ctx)
        elapsed = time.perf_counter() - started
        check("dispatch returned promptly", elapsed < 3.0, f"{elapsed:.2f}s for a 0.3s budget")
        check("the result is flagged as an error", is_error)
        check("it is recognisably a timeout", D.is_timeout(content), content[:90])
        check("the abandonment was counted", D.abandoned_count() > before)
        check("current_tool_name was restored", ctx.current_tool_name == "",
              repr(ctx.current_tool_name))
    finally:
        TR._dispatch_tool_inner = real_inner
        if original_budget is None:
            TR.TOOL_DEADLINE_SECONDS.pop("web_fetch", None)
        else:
            TR.TOOL_DEADLINE_SECONDS["web_fetch"] = original_budget

    print()
    print("=== 15. WIRING: an exempt tool is dispatched WITHOUT a deadline ===")
    # `run_bash` takes its own timeout argument and `task` runs until its own condition, so neither may be
    # silently given a lower ceiling by the class table.
    for exempt in ("run_bash", "task", "ask_user_question", "parallel_tasks"):
        check(f"{exempt} resolves to no deadline", TR.deadline_for_tool(exempt) is None)
    check("a network tool does get one", TR.deadline_for_tool("web_fetch") is not None)
    check("an MCP tool gets one by prefix",
          TR.deadline_for_tool("mcp__server__tool") is not None,
          "MCP names are server-chosen, so the class covers them by prefix")

    print()
    print(f"=== {len(PASS)} passed, {len(FAIL)} failed ===")
    for label in FAIL:
        print(f"  FAILED: {label}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
