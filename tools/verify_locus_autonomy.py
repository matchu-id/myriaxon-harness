#!/usr/bin/env python3
"""Verify that a Locus wakes the idle prompt on its own.

The gap this closes: `is_due()` already knew when a Locus should run, but the only caller was the top of the REPL
loop, which runs when the user submits a line. A Locus with a 900-second interval therefore fired only if the user
happened to press Enter after 900 seconds -- "autonomous" in name, prompt-driven in practice.

Three things are checked here, and the third is the one that matters:

1. `LocusRegistry.next_wakeup_seconds()` returns the right delay for the state of the loci.
2. The sentinel survives a real prompt_toolkit prompt exit, byte for byte, so the REPL can recognise it.
3. Arming the wake-up against a live prompt loop actually causes an idle prompt to return, with the sentinel,
   without any input. This is asserted against a real PromptSession driven by a pipe, not a mock.
"""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

PASS: list[str] = []
FAIL: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAIL).append(label)
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}" + (f"\n         {detail}" if detail else ""))


def main() -> int:
    from myriaxon import loci as loci_module

    tmp = Path(tempfile.mkdtemp())
    registry = loci_module.LocusRegistry(path=tmp / "loci.json")

    print("=== 1. no loci means nothing to wake for, and the cap is honoured ===")
    check("empty registry returns the idle cap", registry.next_wakeup_seconds(idle_cap=30.0) == 30.0,
          str(registry.next_wakeup_seconds(idle_cap=30.0)))
    check("headless() is empty", registry.headless() == [])

    print()
    print("=== 2. an enabled, proactive locus with a goal is scheduled ===")
    locus = registry.create(name="worker", role="worker", instructions="do the work")
    locus.add_goal("advance the objective", "keep going until it is done")
    locus.enabled = True
    locus.proactive = True
    locus.interval_seconds = 900
    registry.persist(locus)

    check("it is counted as headless", [d.name for d in registry.headless()] == ["worker"],
          str([d.name for d in registry.headless()]))
    # Never run before -> due now -> wake immediately.
    check("never-run locus wakes immediately", registry.next_wakeup_seconds() == 0.0,
          str(registry.next_wakeup_seconds()))

    print()
    print("=== 3. a recently-run locus is not woken early ===")
    locus.last_run_at = time.time()
    locus.interval_seconds = 900
    registry.persist(locus)
    wait = registry.next_wakeup_seconds(idle_cap=30.0)
    check("wait is capped below the 900s interval", wait == 30.0, f"got {wait}")
    wait_uncapped = registry.next_wakeup_seconds(idle_cap=10_000.0)
    check("uncapped wait is near the interval",
          880 <= wait_uncapped <= 900, f"got {wait_uncapped}")

    print()
    print("=== 4. a locus with no runnable goal is not woken at all ===")
    locus.last_run_at = 0.0
    for goal in locus.goals:
        goal.status = loci_module.GOAL_DONE
    registry.persist(locus)
    check("headless() excludes it", registry.headless() == [], str([d.name for d in registry.headless()]))
    check("nothing to wake for", registry.next_wakeup_seconds(idle_cap=30.0) == 30.0)

    print()
    print("=== 5. a disabled or non-proactive locus is not woken ===")
    locus.add_goal("a fresh objective worth working on")
    locus.enabled = False
    registry.persist(locus)
    check("disabled locus excluded", registry.next_wakeup_seconds(idle_cap=30.0) == 30.0)
    locus.enabled = True
    locus.proactive = False
    registry.persist(locus)
    check("non-proactive locus excluded", registry.next_wakeup_seconds(idle_cap=30.0) == 30.0)

    print()
    print("=== 6. the sentinel survives a real prompt exit byte for byte ===")
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput
    from prompt_toolkit.shortcuts import PromptSession

    sentinel = "\x00myriaxon-locus-due"
    with create_pipe_input() as pipe_input:
        session = PromptSession(input=pipe_input, output=DummyOutput())

        def arm() -> None:
            session.app.loop.call_later(0.05, lambda: session.app.exit(result=sentinel))

        returned = session.prompt("> ", pre_run=arm)
    check("prompt returned without input", True)
    check("it returned the sentinel exactly", returned == sentinel, repr(returned))
    check("strip() would not mangle it", returned.strip() == sentinel, repr(returned.strip()))

    print()
    print("=== 7. arming via the real helper makes an idle prompt return ===")
    # This drives the actual `_arm_locus_wakeup` from cli.py against a live prompt, so it tests the mechanism
    # rather than a copy of it.
    from myriaxon import cli as cli_module
    from myriaxon import loci as live_loci

    live = live_loci.reset_registry(tmp / "live-loci.json")
    live_dot = live.create(name="live", role="worker", instructions="work")
    live_dot.add_goal("something worth doing unattended")
    live_dot.enabled = True
    live_dot.proactive = True
    live.persist(live_dot)

    class _FakeAgent:
        tool_context = object()

    original = cli_module._dot_wakeup_limit
    try:
        # A one-second cap keeps the test quick while still exercising the real scheduling path.
        cli_module._dot_wakeup_limit = lambda: 1.0
        with create_pipe_input() as pipe_input:
            session = PromptSession(input=pipe_input, output=DummyOutput())
            started = time.time()
            result = session.prompt(
                "> ",
                pre_run=lambda: cli_module._arm_locus_wakeup(session, _FakeAgent()),
            )
            elapsed = time.time() - started
        check("the idle prompt returned by itself", result == cli_module.LOCUS_WAKE_SENTINEL, repr(result))
        check("it returned promptly", elapsed < 5.0, f"{elapsed:.2f}s")
    finally:
        cli_module._dot_wakeup_limit = original

    print()
    print("=== 8. the escape hatch disables self-waking ===")
    import os

    original_env = os.environ.get("MYRIAXON_DOT_WAKEUP")
    try:
        os.environ["MYRIAXON_DOT_WAKEUP"] = "0"
        check("limit is None when disabled", cli_module._dot_wakeup_limit() is None)
        os.environ["MYRIAXON_DOT_WAKEUP"] = "15"
        check("limit is 15 when set", cli_module._dot_wakeup_limit() == 15.0)
        os.environ["MYRIAXON_DOT_WAKEUP"] = "nonsense"
        check("garbage falls back rather than raising", cli_module._dot_wakeup_limit() == 15.0)
    finally:
        if original_env is None:
            os.environ.pop("MYRIAXON_DOT_WAKEUP", None)
        else:
            os.environ["MYRIAXON_DOT_WAKEUP"] = original_env

    print()
    print("=== 9. arming is safe when there is nothing to do ===")
    class _DeadAgent:
        tool_context = None

    try:
        with create_pipe_input() as pipe_input:
            session = PromptSession(input=pipe_input, output=DummyOutput())
            cli_module._arm_locus_wakeup(session, _DeadAgent())
            check("no ToolContext -> arming is a no-op, not a crash", True)
    except Exception as exc:  # noqa: BLE001
        check("no ToolContext -> arming is a no-op, not a crash", False, repr(exc))

    try:
        cli_module._arm_locus_wakeup(None, _FakeAgent())
        check("no session -> no crash", True)
    except Exception as exc:  # noqa: BLE001
        check("no session -> no crash", False, repr(exc))

    print()
    print("=== 10. take_run returns a goal and its prompt together, or neither ===")
    # The REPL uses this instead of the old next_goal()+proactive_prompt() pair, so a run can never start with
    # an empty prompt, or with a goal that the prompt was not built for.
    reg2 = loci_module.reset_registry(tmp / "take-run.json")
    d2 = reg2.create(name="taker", role="worker", instructions="work")
    d2.add_goal("an objective to advance")
    d2.enabled = True
    d2.proactive = True
    reg2.persist(d2)

    taken = d2.take_run()
    check("take_run returns a pair", taken is not None and len(taken) == 2)
    if taken:
        goal, prompt = taken
        check("the prompt names the goal it returned", goal.title in prompt, prompt.splitlines()[0][:80])
        check("the prompt is non-empty", bool(prompt.strip()))
        check("the goal is now active", goal.status == loci_module.GOAL_ACTIVE, goal.status)
        check("the attempt was counted", goal.attempts == 1, str(goal.attempts))

    print()
    print("=== 11. the attempt ceiling stops an unfinishable goal ===")
    # This is the bound that keeps an autonomous locus from retrying a paid API forever. Driving it past the
    # ceiling must end in `blocked`, and the locus must then stop being a wake-up candidate.
    for _ in range(loci_module.MAX_GOAL_ATTEMPTS + 2):
        d2.take_run()
    statuses = [(g.title, g.status, g.attempts) for g in d2.goals]
    check("the goal ended up blocked rather than looping",
          all(g.status == loci_module.GOAL_BLOCKED for g in d2.goals), str(statuses))
    check("take_run then reports nothing to run", d2.take_run() is None)
    check("and the locus is no longer headless", reg2.headless() == [],
          str([d.name for d in reg2.headless()]))
    check("nothing wakes for it any more", reg2.next_wakeup_seconds(idle_cap=30.0) == 30.0)

    print()
    print(f"=== {len(PASS)} passed, {len(FAIL)} failed ===")
    for label in FAIL:
        print(f"  FAILED: {label}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
