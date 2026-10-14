#!/usr/bin/env python3
"""Verify Myriaxon's self-awareness surface.

"Self-aware" here means what it means in the reference harnesses, which is NOT runtime introspection: it is
deterministic prompt artifacts plus a discovery surface, so the agent can answer "what can I do?" and "what state
am I in?" without guessing. This verifies both halves are present and wired.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from myriaxon import self_status, skill_trust, tool_registry as TR  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAIL).append(label)
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}" + (f"\n         {detail}" if detail else ""))


class Ctx:
    def __init__(self, **kw):
        self.model = kw.get("model", "test-model")
        self.provider = kw.get("provider", "anthropic")
        self.mode = kw.get("mode", "auto")
        self.tools = kw.get("tools", [])
        self.skills = kw.get("skills", {})
        self.locus_run_source = kw.get("locus_run_source", "")
        self.result_char_limit = kw.get("result_char_limit", None)
        self.truncated_any_result = kw.get("truncated_any_result", False)
        self.spilled_any_result = kw.get("spilled_any_result", False)
        self.run_budget = kw.get("run_budget", None)
        self.owner = kw.get("owner", None)
        self.sandbox_enabled = kw.get("sandbox_enabled", False)


def main() -> int:
    print("=== 1. ASK 'WHAT CAN I DO': the capability directory is present ===")
    # OpenClaw's shape: a sorted, size-capped directory of hidden tools with names and descriptions.
    defs = TR.build_tool_defs()
    visible = TR.apply_tool_visibility(defs, set())
    check("there is a deferred set to be aware of", len(defs) > len(visible),
          f"{len(defs)} defined, {len(visible)} visible")
    directory = TR.REQUEST_TOOLS_TOOL_DEF["description"]
    check("the group directory names every group",
          all(group in directory for group in TR.DEFERRED_GROUPS),
          f"groups: {sorted(TR.DEFERRED_GROUPS)}")
    check("each group carries a summary",
          all(str(spec.get("summary") or "").strip() for spec in TR.DEFERRED_GROUPS.values()))
    check("the directory is bounded", len(directory) < 1500, f"{len(directory)} chars")

    print()
    print("=== 2. ASK 'WHAT CAN I DO': the searchable catalog is wired ===")
    entries = TR.catalog_for_deferred_tools()
    check("the catalog has entries", len(entries) >= 10, str(len(entries)))
    check("every entry has a real description",
          not any("no description declared" in e.description for e in entries))
    check("tool_search is itself discoverable by name",
          any(e.name == "tool_search" for e in entries) or "tool_search" in
          [TR._def_name(t) for t in visible],
          "the discovery tool must not be hidden from discovery")

    print()
    print("=== 3. ASK 'WHAT DO I KNOW HOW TO DO': skills carry paths ===")
    # OpenClaw carries name, description AND location, and tells the model to read the file.
    sample = {"s1": type("S", (), {"name": "demo", "description": "does a thing",
                                  "source": Path("/tmp/demo/SKILL.md")})()}
    listing = skill_trust.format_skill_listing(sample)
    check("the listing carries the name", "demo" in listing)
    check("the listing carries the description", "does a thing" in listing)
    check("the listing carries the PATH", "SKILL.md" in listing, listing)
    plain = skill_trust.format_skill_listing(sample, include_paths=False)
    check("paths can be turned off", "SKILL.md" not in plain, plain)

    print()
    print("=== 4. ASK 'WHAT STATE AM I IN': session_status exists and is callable ===")
    names = [TR._def_name(t) for t in defs]
    check("session_status is defined", "session_status" in names)
    check("session_status is visible by default", "session_status" in [TR._def_name(t) for t in visible],
          "a self-report you must unlock first is not self-awareness")
    check("it declares a section parameter",
          "section" in TR.SESSION_STATUS_TOOL_DEF["input_schema"]["properties"])
    check("its description separates it from tool_search",
          "what state am I in" in TR.SESSION_STATUS_TOOL_DEF["description"],
          TR.SESSION_STATUS_TOOL_DEF["description"][:200])

    print()
    print("=== 5. THE REPORT: every section renders from a bare context ===")
    # A context carrying almost nothing is the hard case: the report must still render, because that is exactly
    # when a model asks what is wrong.
    bare = Ctx()
    text = self_status.report(bare)
    for section in ("identity", "tools", "context", "budget", "skills", "guardrails"):
        check(f"{section} renders from a bare context", section in text)
    check("it is labelled as a self-report", "self-report" in text, text[:80])
    check("it carries a timestamp", "Status at" in text)

    print()
    print("=== 6. THE REPORT tells the truth about identity ===")
    rich = Ctx(model="claude-opus-4-5", provider="anthropic", mode="plan")
    text = self_status.report(rich, sections=["identity"])
    check("it names the model", "claude-opus-4-5" in text, text)
    check("it names the provider", "anthropic" in text)
    check("it names the mode", "plan" in text)

    print()
    print("=== 7. THE REPORT exposes the turn's PROVENANCE ===")
    # This is the self-awareness the completion gate depends on: an agent running unattended can see that it has
    # no human authority behind it.
    unattended = self_status.report(Ctx(locus_run_source="locus"), sections=["identity"])
    check("an unattended turn is reported as such", "locus" in unattended, unattended)
    interactive = self_status.report(Ctx(locus_run_source=""), sections=["identity"])
    check("an interactive turn is reported as such", "interactive" in interactive, interactive)

    print()
    print("=== 8. THE REPORT says how much budget is LEFT ===")
    from myriaxon import run_budget

    run = run_budget.RunBudget(run_budget.Budget(max_steps=10, max_seconds=60.0, max_tokens=1000))
    run.begin_step()
    text = self_status.report(Ctx(run_budget=run), sections=["budget"])
    check("it reports the ceiling", "10 steps" in text, text)
    check("it reports what is used", "1 step" in text, text)
    check("it reports what remains", "9" in text, text)
    check("a bare context says no budget is attached",
          "no budget" in self_status.report(Ctx(), sections=["budget"]))

    print()
    print("=== 9. THE REPORT surfaces a compaction problem ===")
    # An interrupted compaction means the history may not match the summary. That is exactly the kind of thing a
    # self-report must surface rather than bury.
    import tempfile

    from myriaxon import compaction_audit

    original = compaction_audit.AUDIT_DIR
    scratch = Path(tempfile.mkdtemp())
    try:
        compaction_audit.AUDIT_DIR = scratch
        recorder = compaction_audit.Recorder(session="v", enabled=True, root=scratch)
        compaction_audit.set_active(recorder)
        recorder.record_compaction_start(messages=10, kept_turns=4)
        text = self_status.report(Ctx(), sections=["context"])
        check("an interrupted compaction is REPORTED", "INTERRUPTED" in text, text)
        recorder.record_compaction_end(applied=True)
        text = self_status.report(Ctx(), sections=["context"])
        check("and clears once closed", "INTERRUPTED" not in text, text)
    finally:
        compaction_audit.AUDIT_DIR = original
        compaction_audit.set_active(None)

    print()
    print("=== 10. THE REPORT names guardrail rules, and any that failed to load ===")
    # A rule that did not load is a guardrail the operator believes is active and is not -- the single most
    # important thing this report can say.
    text = self_status.report(Ctx(), sections=["guardrails"])
    check("it reports the rule count", "guardrail rules in force" in text, text)
    check("it reports the sandbox mode", "sandbox mode" in text, text)

    print()
    print("=== 11. THE REPORT is never fatal ===")
    # Every section is best-effort. A self-report that failed as a whole would be useless exactly when the model
    # most needed it.

    class Hostile:
        def __getattr__(self, name):
            raise RuntimeError(f"the context is broken: {name}")

    try:
        text = self_status.report(Hostile())
        check("a hostile context does not raise", isinstance(text, str) and len(text) > 20,
              text[:120])
    except Exception as exc:  # noqa: BLE001
        check("a hostile context does not raise", False, repr(exc))
    check("an unknown section is ignored, not fatal",
          isinstance(self_status.report(Ctx(), sections=["nonsense"]), str))

    print()
    print("=== 12. WIRING: the runtime-context carrier is on all three agent paths ===")
    # A carrier nothing appends is a library, not a mechanism. This is the check that would have caught the state
    # before this session: `runtime_context` was written and verified but called by nobody.
    #
    # The assertion is on the CALL, not on an import. The first version looked for the literal string
    # "import runtime_context", which was true only while each agent had its own private copy of the helper -- and
    # the helper now lives in `model_adapt` so the three cannot drift. Asserting the old spelling would have kept
    # passing after the call was deleted.
    for module in ("agent.py", "openai_agent.py", "gemini_agent.py"):
        source = (REPO / "myriaxon" / module).read_text(encoding="utf-8")
        check(f"{module} appends a snapshot",
              "append_runtime_context" in source,
              "called from the turn, after the user message and before the first request")
    agent_src = (REPO / "myriaxon" / "agent.py").read_text(encoding="utf-8")
    check("agent.py appends inside run_turn",
          agent_src.index("_append_runtime_context(turn_run)") > agent_src.index("def run_turn"))
    check("appending never raises",
          "runtime context snapshot skipped" in agent_src, "a snapshot failure must not fail a turn")

    shared = (REPO / "myriaxon" / "model_adapt.py").read_text(encoding="utf-8")
    check("the shared helper exists once, in model_adapt",
          shared.count("def append_runtime_context") == 1,
          "three copies would drift within a release")
    check("it imports the carrier lazily",
          "from . import runtime_context" in shared,
          "the CLI's startup path must not pay for this")

    print()
    print("=== 13. THE PROPERTY THAT MATTERS: the system prompt stays cacheable ===")
    from myriaxon import runtime_context

    from myriaxon.system_prompt import build_system_prompt

    class Agent:
        profile = None
        project_instructions = ""
        provider = "anthropic"
        url_mcp_servers: list = []
        skills: dict = {}
        mode = "auto"
        model = "claude-sonnet-4-5"
        effort = "medium"

    prompt = build_system_prompt(Agent())
    leaked = runtime_context.volatile_clauses_in_system_prompt(prompt)
    check("NO volatile clause is in the live system prompt", leaked == [], str(leaked))
    check("the prompt is a real prompt", len(prompt) > 500, f"{len(prompt)} chars")

    print()
    print("=== 14. a snapshot distinguishes no-change from change ===")
    tracker = runtime_context.RuntimeContextTracker()
    messages: list = []
    first = runtime_context.append_if_changed(
        messages, runtime_context.RuntimeContext(mode="auto", when=1000.0), tracker)
    second = runtime_context.append_if_changed(
        messages, runtime_context.RuntimeContext(mode="auto", when=1000.0), tracker)
    third = runtime_context.append_if_changed(
        messages, runtime_context.RuntimeContext(mode="plan", when=1000.0), tracker)
    check("the first appends", first)
    check("an unchanged one does NOT", not second)
    check("a changed one does", third)
    check("exactly two snapshots exist", runtime_context.count_snapshots(messages) == 2,
          str(runtime_context.count_snapshots(messages)))

    print()
    print(f"=== {len(PASS)} passed, {len(FAIL)} failed ===")
    for label in FAIL:
        print(f"  FAILED: {label}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
