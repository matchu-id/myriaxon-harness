#!/usr/bin/env python3
"""Verify runtime context snapshots.

The two properties that matter: a snapshot is APPENDED and never rewritten (so the cached prefix survives), and
the volatile clauses appear in the snapshot and NOT in the system prompt (so the prefix does not change at all).
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from myriaxon import runtime_context as R  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAIL).append(label)
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}" + (f"\n         {detail}" if detail else ""))


def main() -> int:
    print("=== 1. a snapshot renders its clauses under a stable marker ===")
    context = R.RuntimeContext(
        mode="auto", workspace="/work/proj", sandbox="workspace-write",
        approval="ask", budget="12 steps left", when=1_700_000_000.0,
    )
    rendered = context.render()
    check("it starts with the marker", rendered.startswith(R.SOURCE_MARKER), rendered[:60])
    check("every non-empty clause appears", all(
        f"{name}:" in rendered for name in
        (R.CLAUSE_MODE, R.CLAUSE_WORKSPACE, R.CLAUSE_SANDBOX, R.CLAUSE_APPROVAL, R.CLAUSE_BUDGET)
    ), rendered)

    print()
    print("=== 2. empty clauses are omitted, not rendered blank ===")
    # A snapshot full of "mode: (unknown)" trains the reader to skim it, which defeats the message.
    sparse = R.RuntimeContext(mode="auto", when=1_700_000_000.0)
    body = sparse.render()
    check("the set clause is present", "mode:" in body)
    check("an unset clause is absent", "sandbox:" not in body, body)
    check("and not rendered as empty", "workspace: " not in body, body)

    print()
    print("=== 3. THE FIX: the clauses do NOT belong in the system prompt ===")
    # The assertion their snapshot test makes, and the one that keeps the design honest: it is easy to add the
    # message and forget to remove the copy from the prompt, and the duplicate is invisible until a provider bill
    # shows the cache never hitting.
    leaked = R.volatile_clauses_in_system_prompt(rendered)
    check("a snapshot's own text naturally contains its clauses", leaked != [],
          "sanity: the detector can fire")

    stable_prompt = (
        "You are Myriaxon, an agentic coding harness.\n"
        "Be concise and verify your work.\n"
    )
    check("a stable system prompt has NO volatile clauses",
          R.volatile_clauses_in_system_prompt(stable_prompt) == [],
          str(R.volatile_clauses_in_system_prompt(stable_prompt)))
    check("a prompt that leaked the workspace is CAUGHT",
          R.volatile_clauses_in_system_prompt("Working dir is set.\nworkspace: /work\n") == ["workspace"],
          "it names what leaked rather than only that something did")

    print()
    print("=== 4. the detector finds every clause, not just one ===")
    all_clauses = "\n".join(f"{name}: x" for name in R.CLAUSES)
    found = R.volatile_clauses_in_system_prompt(all_clauses)
    check("all six clauses are detected", sorted(found) == sorted(R.CLAUSES), str(sorted(found)))

    print()
    print("=== 4b. THE FIX: the detector matches clause POSITION, not a bare substring ===")
    # A substring check produced a false positive on live text: the system prompt contains "Work one step at a
    # time: inspect what you need", and `"time:" in prompt` matched the `time:` inside it. A detector that
    # reports a leak where there is none is worse than no detector, because the next real leak gets dismissed as
    # the same false alarm.
    prose = "Work one step at a time: inspect what you need, make the change, then verify it."
    check("prose containing 'time:' is NOT a leak",
          R.volatile_clauses_in_system_prompt(prose) == [],
          str(R.volatile_clauses_in_system_prompt(prose)))
    check("a real clause at line start IS a leak",
          R.volatile_clauses_in_system_prompt("time: 2026-01-01 12:00") == ["time"])
    check("a clause mid-line after another clause IS found",
          R.volatile_clauses_in_system_prompt("mode: auto\nsandbox: workspace-write")
          == ["mode", "sandbox"])

    print()
    print("=== 4c. THE LIVE SYSTEM PROMPT carries no volatile clauses ===")
    # The assertion that actually keeps the design honest for this harness: it is easy to add the snapshot message
    # and forget the copy that was already in the prompt.
    try:
        from myriaxon.system_prompt import build_system_prompt

        class _Agent:
            profile = None
            project_instructions = ""
            provider = "anthropic"
            url_mcp_servers: list = []
            skills: dict = {}
            mode = "auto"
            model = "claude-sonnet-4-5"
            effort = "medium"

        live = build_system_prompt(_Agent())
        leaked_live = R.volatile_clauses_in_system_prompt(live)
        check("the live system prompt is clean", leaked_live == [], str(leaked_live))
        check("it is a real prompt, not an empty string", len(live) > 500, f"{len(live)} chars")
    except Exception as exc:  # noqa: BLE001
        check("the live system prompt could be built", False, f"{type(exc).__name__}: {exc}")

    print()
    print("=== 5. THE FIX: nothing is appended when nothing changed ===")
    tracker = R.RuntimeContextTracker()
    context = R.RuntimeContext(mode="auto", workspace="/w", when=1_700_000_000.0)
    first = tracker.observe(context)
    check("the first observation produces a snapshot", first is not None)
    check("it counted the append", tracker.appended == 1)
    second = tracker.observe(context)
    check("an identical observation produces NOTHING", second is None)
    check("it counted the skip", tracker.skipped == 1)
    check("the appended count did not move", tracker.appended == 1)

    print()
    print("=== 6. time is QUANTISED, so a step a second later is not a change ===")
    # Without this the tracker appends on every step, which is the failure it exists to avoid.
    quantised = R.RuntimeContextTracker(time_bucket_seconds=60)
    base = 1_700_000_000.0
    quantised.observe(R.RuntimeContext(mode="auto", when=base))
    same_minute = quantised.observe(R.RuntimeContext(mode="auto", when=base + 5))
    check("5 seconds later in the same minute is not a change", same_minute is None,
          "a per-second timestamp would re-send the snapshot constantly")
    next_minute = quantised.observe(R.RuntimeContext(mode="auto", when=base + 90))
    check("a later minute IS a change", next_minute is not None)
    check("the snapshot reports minute resolution",
          "%Y" not in next_minute and ":" in next_minute, next_minute.replace("\n", " | "))

    print()
    print("=== 7. a real change appends a NEW snapshot rather than rewriting the old one ===")
    changing = R.RuntimeContextTracker()
    messages: list = []
    base_ctx = dict(mode="auto", workspace="/w", when=1_700_000_000.0)
    R.append_if_changed(messages, R.RuntimeContext(**base_ctx), changing)
    check("one snapshot after the first", R.count_snapshots(messages) == 1)

    check("a change appends", R.append_if_changed(
        messages, R.RuntimeContext(**{**base_ctx, "approval": "never"}), changing))
    check("there are now TWO snapshots", R.count_snapshots(messages) == 2,
          "appended, not rewritten")
    check("the earlier one is untouched",
          messages[0]["content"] == R.RuntimeContext(**base_ctx).render(),
          "rewriting it would discard the provider's cached prefix for the whole conversation")
    check("the newest reflects the change", "approval: never" in R.last_snapshot(messages))

    check("no change appends nothing", not R.append_if_changed(
        messages, R.RuntimeContext(**{**base_ctx, "approval": "never"}), changing))
    check("still two snapshots", R.count_snapshots(messages) == 2)

    print()
    print("=== 8. a snapshot is a SOURCED message ===")
    # A reader must be able to tell harness-supplied context from something the user or a tool said.
    message = R.RuntimeContextTracker().message(R.RuntimeContext(mode="auto", when=1.0))
    check("it carries a source field", message.get("source") == R.SOURCE, str(message.get("source")))
    check("it is a user-role message", message.get("role") == "user")
    check("it is recognised as a snapshot", R.is_runtime_context_message(message))

    print()
    print("=== 9. recognition survives a provider conversion that drops extra keys ===")
    # The text marker is the fallback, because a converted message may have lost its extra keys.
    converted = {"role": "user", "content": R.SOURCE_MARKER + "\nmode: auto"}
    check("a marker-only message is still recognised", R.is_runtime_context_message(converted))
    check("a plain user message is not",
          not R.is_runtime_context_message({"role": "user", "content": "hello"}))
    check("a tool result is not",
          not R.is_runtime_context_message({"role": "user", "content": [{"type": "tool_result"}]}))
    check("a non-dict is not", not R.is_runtime_context_message("hello"))
    check("None is not", not R.is_runtime_context_message(None))

    print()
    print("=== 10. counting and last_snapshot tolerate an empty or odd transcript ===")
    check("an empty list counts zero", R.count_snapshots([]) == 0)
    check("an empty list has no last snapshot", R.last_snapshot([]) == "")
    check("last_snapshot ignores non-snapshots",
          R.last_snapshot([{"role": "user", "content": "hi"}]) == "")
    check("count_snapshots ignores non-snapshots",
          R.count_snapshots([{"role": "user", "content": "hi"}, {"role": "assistant", "content": "yo"}]) == 0)
    check("last_snapshot returns the NEWEST",
          R.last_snapshot([
              {"role": "user", "content": R.SOURCE_MARKER + "\nmode: a"},
              {"role": "assistant", "content": "ok"},
              {"role": "user", "content": R.SOURCE_MARKER + "\nmode: b"},
          ]).endswith("mode: b"))

    print()
    print("=== 11. reset forces the next observation to append ===")
    # For a genuine discontinuity: a resumed session, or a compaction that rewrote the history. Without it the
    # tracker believes the facts were communicated when the message carrying them is gone.
    resetting = R.RuntimeContextTracker()
    fixed = R.RuntimeContext(mode="auto", when=1_700_000_000.0)
    resetting.observe(fixed)
    check("a repeat is skipped before the reset", resetting.observe(fixed) is None)
    resetting.reset()
    check("after a reset it appends again", resetting.observe(fixed) is not None)
    check("the append count reflects both", resetting.appended == 2, str(resetting.appended))

    print()
    print("=== 12. clauses_present reports per clause ===")
    present = R.clauses_present("mode: auto\napproval: ask")
    check("a present clause is true", present[R.CLAUSE_MODE] is True)
    check("an absent clause is false", present[R.CLAUSE_SANDBOX] is False)
    check("empty text reports all absent",
          not any(R.clauses_present("").values()))

    print()
    print("=== 13. the marker is stable enough to match on ===")
    check("the marker is a fixed literal", R.SOURCE_MARKER == "[runtime context]")
    check("the source label is a fixed literal", R.SOURCE == "runtime-context")
    check("the clause vocabulary is closed and ordered",
          R.CLAUSES == ("time", "mode", "workspace", "sandbox", "approval", "budget"), str(R.CLAUSES))

    print()
    print("=== 14. a real tracker used the way an agent would behaves ===")
    # The end-to-end shape: a session that changes state twice should hold exactly three snapshots, and the
    # system prompt should never have carried any of it.
    agentish = R.RuntimeContextTracker()
    history: list = []
    R.append_if_changed(history, R.RuntimeContext(mode="auto", workspace="/w", when=1_700_000_000.0), agentish)
    R.append_if_changed(history, R.RuntimeContext(mode="auto", workspace="/w", when=1_700_000_001.0), agentish)
    R.append_if_changed(history, R.RuntimeContext(mode="plan", workspace="/w", when=1_700_000_002.0), agentish)
    R.append_if_changed(history, R.RuntimeContext(mode="plan", workspace="/w", when=1_700_000_003.0), agentish)
    check("exactly two snapshots for two distinct states", R.count_snapshots(history) == 2,
          f"{R.count_snapshots(history)} snapshots")
    check("the first reports the first mode", "mode: auto" in history[0]["content"])
    check("the second reports the change", "mode: plan" in R.last_snapshot(history))
    check("the tracker's counters reconcile",
          agentish.appended == 2 and agentish.skipped == 2,
          f"appended={agentish.appended} skipped={agentish.skipped}")

    print()
    print(f"=== {len(PASS)} passed, {len(FAIL)} failed ===")
    for label in FAIL:
        print(f"  FAILED: {label}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
