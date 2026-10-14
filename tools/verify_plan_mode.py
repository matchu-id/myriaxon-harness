#!/usr/bin/env python3
"""Verify plan mode is ENFORCED, not merely displayed.

## The defect this closes

Myriaxon had `MODE_PLAN` in its constants, accepted `--mode plan` on the command line, and displayed it in the
status line — and **consulted it nowhere**. Measured: `ctx.mode` was assigned and passed around, but no tool dispatch
read it. So plan mode was at most a prompt hint, and a model that ignored the hint could edit freely.

Opencode's plan agent is a *structural* denial (its built-in `plan` agent denies every edit tool and exposes only
`question`/`plan_exit`). The distinction is everything: **a mode that depends on the model's cooperation is not a
mode.**

## What these checks are about

Mostly that the refusal is real, fires before anything happens, and is **actionable**. A refusal that does not say
what to do next is how a retry loop starts.
"""

from __future__ import annotations

import importlib.util
import io
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

# rich is absent and there is no network, so a documented test double stands in and says so -- see the note in
# verify_loci_character. Without it the import line kills the run before any assertion.
if importlib.util.find_spec("rich") is None:
    sys.path.insert(0, str(REPO / "tools" / "_testdoubles"))
    print("  [NOTE] rich is absent; using the test double in tools/_testdoubles.", flush=True)

from myriaxon import plan_mode as P  # noqa: E402
from myriaxon import tool_registry as TR  # noqa: E402
from myriaxon.constants import MODE_AUTO, MODE_AUTO_EDIT, MODE_MANUAL, MODE_PLAN  # noqa: E402
from myriaxon.tool_context import ToolContext  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAIL).append(label)
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}" + (f"\n         {detail}" if detail else ""))


def context(mode: str) -> ToolContext:
    from rich.console import Console

    ctx = ToolContext(client=None, model="m", max_tokens=1, console=Console(file=io.StringIO()),
                      yolo=True, tools=[])
    ctx.mode = mode
    return ctx


def main() -> int:
    print("=== 1. THE DEFECT: mode was displayed and never consulted ===")
    # Assert the wiring, not the definition. A rule nothing calls is a library, and this repository has hit that
    # failure five times.
    source = (REPO / "myriaxon" / "tool_registry.py").read_text(encoding="utf-8")
    check("dispatch consults plan mode", "_check_plan_mode(name, ctx)" in source,
          "the rule must be called from the dispatch path")
    check("  and it runs BEFORE guardrails",
          source.index("_check_plan_mode(name, ctx)") < source.index("_check_guardrails(name, tool_input, ctx)"),
          "the session's operating mode outranks a user rule permitting the tool")
    check("  and before validation",
          source.index("_check_plan_mode(name, ctx)") < source.index("_validate_against_schema")
          if "_validate_against_schema" in source else True)

    print()
    print("=== 2. plan mode REFUSES what changes state ===")
    for tool in ("write_file", "edit_file", "apply_patch", "run_bash", "run_python",
                 "checkpoint_restore", "locus_create", "spawn_subagent", "save_skill"):
        verdict = P.check(tool, MODE_PLAN)
        check(f"refuses {tool}", not verdict.allowed, verdict.reason[:70] or "allowed")

    print()
    print("=== 3. plan mode ALLOWS what only reads ===")
    # The purpose of the mode is to investigate and propose, so refusing reads would make it useless.
    for tool in ("read_file", "grep", "glob", "list_directory", "web_fetch", "session_status", "recall",
                 "tool_search", "ask_user"):
        verdict = P.check(tool, MODE_PLAN)
        check(f"allows {tool}", verdict.allowed, verdict.reason[:70])

    print()
    print("=== 4. an UNKNOWN tool is refused when its name says it changes things ===")
    # The backstop for a plugin or MCP tool this module has never heard of. Being wrong permissively here is the bug;
    # being wrong restrictively costs one turn.
    for tool in ("mcp__server__write_thing", "plugin__delete_file", "custom_edit", "remote_exec"):
        check(f"refuses unknown {tool}", not P.check(tool, MODE_PLAN).allowed)
    for tool in ("mcp__server__read_thing", "plugin__list_items", "custom_inspect"):
        check(f"allows unknown {tool}", P.check(tool, MODE_PLAN).allowed)

    print()
    print("=== 5. the other modes are unaffected ===")
    for mode in (MODE_MANUAL, MODE_AUTO_EDIT, MODE_AUTO):
        for tool in ("write_file", "edit_file", "run_bash"):
            verdict = P.check(tool, mode)
            check(f"{mode} permits {tool}", verdict.allowed, verdict.reason[:60])

    print()
    print("=== 6. an UNKNOWN mode resolves to the restrictive default ===")
    # A typo in a mode name must not be the thing that grants write access.
    for typo in ("plna", "PLAN", "  plan  ", "auto-edit"):
        resolved = P.normalise_mode(typo)
        check(f"{typo!r} resolves to a known mode", resolved in (MODE_PLAN, MODE_MANUAL, MODE_AUTO_EDIT, MODE_AUTO),
              resolved)
    check("a nonsense mode is NOT auto", P.normalise_mode("wibble") != MODE_AUTO,
          P.normalise_mode("wibble"))
    check("  it is manual, which still asks before writing", P.normalise_mode("wibble") == MODE_MANUAL)
    check("case and whitespace are normalised", P.normalise_mode("  PLAN ") == MODE_PLAN,
          P.normalise_mode("  PLAN "))
    check("an empty mode is restrictive", P.normalise_mode("") == MODE_MANUAL)
    check("plan mode is still refused after normalisation",
          not P.check("write_file", "  PLAN  ").allowed)

    print()
    print("=== 7. THE REFUSAL IS ACTIONABLE ===")
    # "Blocked" alone leaves a model guessing whether to retry, change approach, or stop. A refusal that does not
    # say what to do next is how a retry loop starts.
    reason = P.check("write_file", MODE_PLAN).reason
    check("it says what was refused", "write_file" in reason, reason[:100])
    check("it says NOTHING WAS CHANGED", "Nothing was changed" in reason, reason[:160])
    check("it names the read-only tools to use instead",
          "read_file" in reason and "grep" in reason, reason[:220])
    check("it says what to do when ready", "plan" in reason.lower() and "reply" in reason.lower(),
          reason[:260])
    check("it says how to LEAVE plan mode", "auto_edit" in reason, reason[:320])
    check("it says NOT to retry", "Do not retry" in reason, reason[-200:])
    check("the same message shape is used everywhere", reason.count("[plan mode]") == 1)

    print()
    print("=== 8. WIRED: dispatch really refuses ===")
    ctx = context(MODE_PLAN)
    text, failed = TR.dispatch_tool("write_file", {"path": "probe.txt", "content": "should not exist"}, ctx)
    check("dispatch refuses a write", failed, text[:80])
    check("  the message is the plan-mode one", "[plan mode]" in text, text[:80])
    check("  and the file was NOT created", not Path("probe.txt").exists(),
          "a refusal that writes anyway is the bug it prevents")

    text, failed = TR.dispatch_tool("run_bash", {"command": "echo probe"}, ctx)
    check("dispatch refuses a shell command", failed and "[plan mode]" in text, text[:80])

    text, failed = TR.dispatch_tool("read_file", {"path": "setup.py"}, ctx)
    check("dispatch still allows a read", "[plan mode]" not in text, text[:80])

    print()
    print("=== 9. leaving plan mode restores the tool ===")
    # The refusal must be a function of the mode, not a latch.
    ctx2 = context(MODE_AUTO_EDIT)
    text, _failed = TR.dispatch_tool("read_file", {"path": "setup.py"}, ctx2)
    check("a non-plan mode is not refused by plan mode", "[plan mode]" not in text, text[:80])
    check("and the rule agrees for the same tool",
          P.check("write_file", MODE_AUTO_EDIT).allowed)

    print()
    print("=== 10. the rule is testable without a context ===")
    # `check()` takes a mode string, so the rule does not require faking a ToolContext -- which is what makes the
    # sections above possible at all.
    check("check takes a mode string", callable(P.check))
    check("check_for_context exists for the dispatch path", callable(P.check_for_context))
    try:
        verdict = P.check_for_context("write_file", context(MODE_PLAN))
        check("check_for_context reads ctx.mode", not verdict.allowed)
    except Exception as exc:  # noqa: BLE001
        check("check_for_context reads ctx.mode", False, f"{type(exc).__name__}: {exc}")
    try:
        bare = P.check_for_context("write_file", object())
        check("a contextless object does not raise", isinstance(bare.allowed, bool), str(bare))
    except Exception as exc:  # noqa: BLE001
        check("a contextless object does not raise", False, f"{type(exc).__name__}: {exc}")

    print()
    print("=== 11. describe() explains each mode, and plan mode says it is enforced ===")
    for mode in (MODE_PLAN, MODE_MANUAL, MODE_AUTO_EDIT, MODE_AUTO):
        text = P.describe(mode)
        check(f"describe({mode}) is non-empty", bool(text.strip()), text[:80])
    check("plan's description says it is read-only", "read-only" in P.describe(MODE_PLAN),
          P.describe(MODE_PLAN))
    check("  and that the refusal is structural",
          "refused before it runs" in P.describe(MODE_PLAN), P.describe(MODE_PLAN))

    print()
    print("=== 12. no tool in the live registry is misclassified as read-only ===")
    # A tool that writes but is listed as read-only would be permitted in plan mode, which is the one direction of
    # error this module must not make.
    live = [TR._def_name(d) for d in TR.build_tool_defs()]
    check("the registry was readable", len(live) > 20, str(len(live)))
    overlaps = sorted(set(P.READ_ONLY_TOOLS) & set(P.STATEFUL_TOOLS))
    check("the two sets are disjoint", not overlaps, str(overlaps))
    wrongly_readonly = [n for n in live if n in P.READ_ONLY_TOOLS and P.is_stateful(n)]
    check("no live tool is both listed read-only and stateful", not wrongly_readonly,
          str(wrongly_readonly))

    print()
    print(f"=== {len(PASS)} passed, {len(FAIL)} failed ===")
    for label in FAIL:
        print(f"  FAILED: {label}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
