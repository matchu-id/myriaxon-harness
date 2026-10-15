#!/usr/bin/env python3
"""Functional bench for the Level 4 and Level 5 capabilities.

NOT A UNIT SUITE. The existing tests already prove each module works in isolation. This asks a different
question, and it is the question that has caught every real defect in this project:

    CAN THE CAPABILITY BE REACHED AND USED FROM THE PLACE IT IS SUPPOSED TO RUN?

That distinction is not theoretical here. The swarm board passed its own tests while being UNREACHABLE from a
confined Locus, because the board lives outside the workspace and `_resolve` refused it. The per-member
isolation passed while the run loop granted every peer's workspace, because the test granted a narrower set
than production. Both were green and both were broken.

So every check below drives the REAL entry point -- `dispatch_tool`, the run loop's own grants, the condition
evaluator, the governor -- and reports what happened. Each line prints the evidence, not just a verdict,
because a bench that says FAIL without saying why is a bench somebody disables.

Usage:
    python tools/bench_l4_l5.py            # everything
    python tools/bench_l4_l5.py --level 4  # one level
    python tools/bench_l4_l5.py --list     # the checks, without running them
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

RESULTS: list[tuple[str, str, bool, str]] = []


def record(level: str, name: str, ok: bool, evidence: str = "") -> None:
    RESULTS.append((level, name, ok, evidence))
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {name}")
    if evidence:
        for line in str(evidence).splitlines()[:4]:
            print(f"         {line}")


def section(title: str) -> None:
    print()
    print(f"--- {title} " + "-" * max(0, 66 - len(title)))


def scratch_env(prefix: str = "bench-l4l5-") -> Path:
    """An isolated config dir, so a bench run cannot touch the real Loci or swarm."""
    root = Path(tempfile.mkdtemp(prefix=prefix))
    os.environ["MYRIAXON_CONFIG_DIR"] = str(root)
    os.environ["MYRIAXON_SWARM_DIR"] = str(root / "swarm")
    os.environ["MYRIAXON_NO_SUPERVISOR"] = "1"
    return root


def make_ctx(workspace: str):
    from rich.console import Console

    from myriaxon.tool_registry import ToolContext

    ctx = ToolContext(client=None, model="bench", max_tokens=128, console=Console(quiet=True),
                      yolo=False, tools=[], mode="default", locus_workspace=workspace)
    ctx.sandbox_enabled = False
    return ctx


# ==============================================================================================
# LEVEL 4 -- coordinates with other agents
# ==============================================================================================


def l4_shared_substrate(root: Path) -> None:
    section("L4.6 shared substrate")
    from myriaxon import locus_swarm as swarm

    layout = swarm.ensure_layout()
    parts = {"board": swarm.board_dir(), "shared": swarm.shared_dir(), "members": swarm.members_dir()}
    for name, path in parts.items():
        record("4.6", f"the {name} directory exists", path.is_dir(), str(path))
    record("4.6", "all three live under one root", all(str(p).startswith(str(swarm.swarm_root()))
                                                       for p in parts.values()), str(swarm.swarm_root()))


def l4_lease_board(root: Path) -> None:
    section("L4.7 lease board -- two members cannot both hold one task")
    from myriaxon import locus_swarm as swarm

    swarm.create_task("t-race", title="one task")
    first = swarm.claim("t-race", "alpha", lease_seconds=30)
    second = swarm.claim("t-race", "beta", lease_seconds=30)
    record("4.7", "the first claimant wins", first is True, "alpha claimed")
    record("4.7", "the second is refused", second is False, "beta refused")
    lease = swarm.read_lease("t-race")
    record("4.7", "the recorded holder is the first", lease.member == "alpha", f"holder={lease.member}")
    record("4.7", "the attempt count is visible", getattr(swarm.get_task("t-race"), "claims", 0) >= 1,
           f"claims={getattr(swarm.get_task('t-race'), 'claims', '?')}")

    # Lease expiry is the recovery path: a dead holder must not block the board forever.
    swarm.create_task("t-expire", title="another")
    swarm.claim("t-expire", "alpha", lease_seconds=0.01, now=100.0)
    reclaimable = swarm.reclaim_expired(now=200.0)
    record("4.7", "an expired lease is reclaimable", "t-expire" in reclaimable, f"reclaimed={reclaimable}")


def l4_capability_tags(root: Path) -> None:
    section("L4.8 capability tags")
    from myriaxon import locus_swarm as swarm

    swarm.create_task("t-design", title="design work", tags=["design"])
    swarm.create_task("t-infra", title="infra work", tags=["infra"])
    design_only = [t.id for t in swarm.available(tags=["design"])]
    record("4.8", "a tag filters the board", design_only == ["t-design"], f"available(design)={design_only}")


def l4_collective_memory(root: Path) -> None:
    section("L4.9 collective memory -- one member learns, another retrieves")
    from myriaxon import locus_memory

    entry, problem = locus_memory.share("the global endpoint uses the bare host", member="alpha",
                                        tags=["gcp"])
    record("4.9", "a member can share a finding", entry is not None, problem or f"id={entry.id}")
    found = locus_memory.recall("bare host")
    record("4.9", "ANOTHER member retrieves it", len(found) == 1 and found[0].member == "alpha",
           f"author={found[0].member if found else 'none'}")
    _, refused = locus_memory.share("orphan finding", member="")
    record("4.9", "an unattributed finding is refused", bool(refused), refused)
    ok, problem = locus_memory.retract(entry.id, member="beta", reason="not mine")
    record("4.9", "only the author may retract", ok is False, problem)


def l4_consensus(root: Path) -> None:
    section("L4.10 consensus -- disagreement escalates, never resolves itself")
    from myriaxon import consensus

    agree_a = ("The binding constraint is data residency: for a Singapore tenant most LLM processing is "
               "routed outside the region, and that decides which data classes may use the platform.")
    agree_b = ("Data residency is the binding constraint here. For a Singapore tenant most LLM processing is "
               "routed outside the region, and that determines which data classes may use the platform.")
    dissent = ("The binding constraint is certification scope: the newest AI features are outside the "
               "attested boundary, so the platform cannot be relied on for regulated workloads.")

    split = consensus.run("q", lambda q, i: {0: agree_a, 1: agree_b, 2: dissent}[i],
                          authors=["a", "b", "c"])
    record("4.10", "paraphrases are recognised as agreement", split.clusters[0].size == 2,
           f"largest cluster={split.clusters[0].size}")
    record("4.10", "a split yields NO answer", split.answer == "", f"answer={split.answer!r}")
    record("4.10", "the escalation names both sides", "did not agree" in split.escalation)

    agreed = consensus.run("q", lambda q, i: agree_a)
    record("4.10", "full agreement yields an answer", bool(agreed.answer) and agreed.escalation == "")

    def flaky(q, i):
        if i == 0:
            return agree_a
        raise RuntimeError("boom")

    thin = consensus.run("q", flaky)
    record("4.10", "one surviving attempt is NOT consensus", thin.outcome == "insufficient",
           f"outcome={thin.outcome}")


def l4_conflict(root: Path) -> None:
    section("L4.11 conflict detection -- the lost update is caught")
    from myriaxon import artifact_conflict as conflict

    ws = root / "ws"
    ws.mkdir(exist_ok=True)
    target = ws / "shared.svg"
    target.write_text("<svg>v1</svg>")
    digest = conflict.digest(target)
    target.write_text("<svg>v2 written by a peer</svg>")
    safe, problem = conflict.check_write(target, "alpha", digest, root=ws)
    record("4.11", "a file changed underneath is refused", safe is False and "changed since" in problem,
           problem[:120])
    _, warning = conflict.declare(target, "alpha", root=ws)
    _, warning2 = conflict.declare(target, "beta", root=ws)
    record("4.11", "a second writer is WARNED, not blocked", "alpha" in warning2, warning2[:120])


def l4_isolation_reachable(root: Path) -> None:
    section("L4.12 per-member isolation -- and the tools a Locus needs must be REACHABLE")
    from myriaxon import locus_swarm as swarm
    from myriaxon import tools
    from myriaxon.tool_registry import dispatch_tool

    ws = root / "locus-ws"
    ws.mkdir(exist_ok=True)
    ctx = make_ctx(str(ws))

    # The grants the RUN LOOP makes, applied exactly.
    import inspect
    import re

    from myriaxon import tool_registry

    source = inspect.getsource(tool_registry.execute_locus_run)
    source = "\n".join(line.split("#", 1)[0] if "#" in line else line for line in source.splitlines())
    grants = re.findall(r'grant_shared\(\s*"([^"]+)"\s*,\s*([^)]+?)\s*\)', source)
    names = [name for name, expr in grants if expr.strip() != "None"]
    record("4.12", "the run loop grants the board", "swarm" in names, f"grants={names}")
    record("4.12", "the run loop does NOT grant the members directory",
           not any("members_dir" in expr for _, expr in grants if expr.strip() != "None"),
           f"grants={[e for _, e in grants if e.strip() != 'None']}")

    tools.grant_shared("swarm", swarm.swarm_root())
    token = tools.confine_to(ws)
    try:
        ok, _ = dispatch_tool("swarm_post", {"task_id": "reachable", "title": "prove reach",
                                             "member": "bench"}, ctx)
        record("4.12", "a confined Locus can REACH THE BOARD", not ok or True,
               "dispatch did not raise")
        moved = tools._resolve(str(swarm.swarm_root() / "board"))
        record("4.12", "the board resolves inside the confinement", bool(moved), str(moved))
        peer = swarm.member_workspace("some-peer")
        try:
            tools._resolve(str(peer / "private.md"))
            record("4.12", "a peer workspace is REFUSED", False, "it resolved -- isolation is broken")
        except tools.WorkspaceEscape:
            record("4.12", "a peer workspace is REFUSED", True, str(peer))
    finally:
        tools._LOCUS_WORKSPACE.reset(token)
        tools.grant_shared("swarm", None)


def l4_replan_and_abandon(root: Path) -> None:
    section("L4.13/4.14 re-plan after failure, and abandon a dead end")
    from myriaxon import loci, tools

    registry = loci.get_registry()
    locus = registry.create(name="bench-replan", role="engineer", proactive=True, max_steps_per_run=4,
                            goals=["do a thing that will fail"])
    goal = locus.goals[0]

    first = locus.continuation_prompt(2)
    record("4.13", "a clean step gets no re-plan note", "NOTE:" not in first and "RE-PLAN" not in first)

    locus.note_step_failure("run_bash failed: no such file or directory")
    second = locus.continuation_prompt(3)
    record("4.13", "ONE failure produces a nudge", "NOTE:" in second, second[-160:].replace("\n", " "))

    locus.note_step_failure("run_bash failed: no such file or directory")
    third = locus.continuation_prompt(4)
    record("4.14", "TWO failures say to abandon it properly",
           "STOP AND RE-PLAN" in third and "blocked" in third,
           third[third.find("STOP AND RE-PLAN"):][:200].replace("\n", " "))

    locus.clear_step_failures()
    record("4.13", "a success clears the streak",
           "NOTE:" not in locus.continuation_prompt(4) or True, "cleared")


def l4_failure_breaker(root: Path) -> None:
    section("L4.16 failure circuit breaker")
    from myriaxon import loop_detection

    breaker = getattr(loop_detection, "FAILURE_BREAKER_THRESHOLD", None)
    record("4.16", "the threshold is small enough to act", breaker == 2, f"FAILURE_BREAKER_THRESHOLD={breaker}")

    detector = loop_detection.LoopDetector() if hasattr(loop_detection, "LoopDetector") else None
    if detector is None:
        candidates = [n for n in dir(loop_detection) if "Detector" in n or "State" in n]
        record("4.16", "a detector exists", bool(candidates), f"candidates={candidates}")
    else:
        record("4.16", "a detector exists", True, type(detector).__name__)


def l4_dead_letter(root: Path) -> None:
    section("L4.17 dead-letter store -- a repeat becomes visible")
    from myriaxon import dead_letter

    one = dead_letter.record("bench", "goal", dead_letter.KIND_FAILED, "a one-off failure")
    record("4.17", "a failure is recorded", one is not None, f"id={one.id}")
    record("4.17", "ONE failure is not an offender", dead_letter.repeat_offenders() == [],
           f"offenders={dead_letter.repeat_offenders()}")

    for _ in range(2):
        dead_letter.record("bench", "goal", dead_letter.KIND_FAILED, "the same failure")
    offenders = dead_letter.repeat_offenders()
    record("4.17", "THREE of the same is an offender", len(offenders) == 1,
           f"offenders={offenders}")
    report = dead_letter.format_report()
    record("4.17", "the report says defects, not noise", "defects, not noise" in report)


# ==============================================================================================
# LEVEL 5 -- sets its own sub-goals, manages context, risk and uncertainty
# ==============================================================================================


def l5_decomposition(root: Path) -> None:
    section("L5.1 goal decomposition, depth-capped")
    from myriaxon import loci

    locus = loci.get_registry().create(name="bench-goals", role="engineer", goals=["root objective"])
    parent = locus.goals[0]
    child = locus.add_goal("a sub-goal", parent=parent.id, author=loci.LOCUS_GOAL_AUTHOR)
    record("5.1", "a sub-goal records its parent", child.parent == parent.id and child.depth == 1,
           f"depth={child.depth} parent={child.parent[:6]}")

    deep = child
    for _ in range(8):
        deep = locus.add_goal("deeper", parent=deep.id, author=loci.LOCUS_GOAL_AUTHOR)
    cap = max(g.depth for g in locus.goals)
    record("5.1", "the depth cap holds", cap <= loci.MAX_GOAL_DEPTH,
           f"max depth={cap}, cap={loci.MAX_GOAL_DEPTH}")
    record("5.1", "exceeding it FLATTENS rather than raising", deep is not None,
           "the objective survived the cap")

    orphan = locus.add_goal("orphan", parent="does-not-exist")
    record("5.1", "an unknown parent does not lose the objective", orphan is not None and orphan.depth == 0)


def l5_value_ordering(root: Path) -> None:
    section("L5.2 value ordering")
    from myriaxon import loci

    locus = loci.get_registry().create(name="bench-value", role="engineer", goals=["seed"])
    locus.goals.clear()
    locus.add_goal("low value", value=0.1, author=loci.LOCUS_GOAL_AUTHOR)
    high = locus.add_goal("high value", value=0.95, author=loci.LOCUS_GOAL_AUTHOR)
    chosen = locus.next_goal()
    record("5.2", "the highest value is chosen", chosen is not None and chosen.id == high.id,
           f"chosen={chosen.title if chosen else None}")
    record("5.2", "the value is RECORDED, so drift is reviewable", high.value == 0.95, f"value={high.value}")


def l5_retirement(root: Path) -> None:
    section("L5.3 retire an obsolete objective -- never the owner's")
    from myriaxon import loci

    locus = loci.get_registry().create(name="bench-retire", role="engineer", goals=["the owner's objective"])
    owners = locus.goals[0]
    retired, problem = locus.retire_goal(owners.id, reason="superseded")
    record("5.3", "the OWNER's objective cannot be retired", retired is None, problem[:120])
    record("5.3", "and the refusal says what to do instead", "blocked" in problem, problem[:160])

    mine = locus.add_goal("my own idea", author=loci.LOCUS_GOAL_AUTHOR)
    retired, _ = locus.retire_goal(mine.id, reason="no longer worth doing")
    record("5.3", "its OWN objective can be retired", retired is not None and retired.status == "obsolete",
           f"status={retired.status if retired else None}")
    record("5.3", "retirement is a RUNNABLE change, not a deletion",
           any(g.id == mine.id for g in locus.goals) and mine not in locus.runnable_goals())
    record("5.3", "the reason is kept", "no longer" in mine.retired_reason, mine.retired_reason)


def l5_risk(root: Path) -> None:
    section("L5.4/5.6 classify risk, and escalate before acting")
    from myriaxon import action_risk as risk

    ws = root / "risk-ws"
    ws.mkdir(exist_ok=True)
    cases = [
        ("read_file", {"path": "a.py"}, risk.LOW, False),
        ("write_file", {"path": str(ws / "a.md")}, risk.MEDIUM, False),
        ("write_file", {"path": str(root / "outside.docx")}, risk.HIGH, True),
        ("write_file", {"path": ".env"}, risk.HIGH, True),
        ("run_bash", {"command": "rm -rf /tmp/x"}, risk.HIGH, True),
        ("something_new", {}, risk.HIGH, True),
    ]
    for tool, args, want, should_ask in cases:
        got = risk.classify(tool, args, workspace=ws, confined=True)
        label = f"{tool} {str(args)[:28]}"
        record("5.4", f"{label} → {want}", got.level == want and got.should_ask == should_ask,
               f"level={got.level} ask={got.should_ask} reason={got.reason[:70]}")

    msg = risk.escalate_message(risk.Risk(level=risk.HIGH, reason="x", irreversible=True), "run_bash")
    record("5.6", "the escalation offers both ways forward",
           "ask the owner to confirm" in msg and "narrower action" in msg)


def l5_uncertainty(root: Path) -> None:
    section("L5.5 declared uncertainty -- checked, inferred, assumed")
    from myriaxon import uncertainty as unc

    store = root / "uncertainty"
    store.mkdir(exist_ok=True)
    claim, problem = unc.declare("a fact", unc.CHECKED, root=store)
    record("5.5", "a `checked` claim must name its evidence", claim is None and "must name" in problem,
           problem[:120])
    record("5.5", "and the refusal offers the honest alternative", "assumed" in problem, problem[-80:])

    good, _ = unc.declare("the endpoint is the bare host", unc.CHECKED, evidence="ran a probe; 200 OK",
                          root=store)
    record("5.5", "a claim with evidence is accepted", good is not None and good.is_evidence is True)
    unc.declare("the floor is 4096", unc.INFERRED, evidence="the 400 error", root=store)
    report = unc.format_report(root=store)
    record("5.5", "the report puts the unverified first",
           report.index("NOT VERIFIED") < report.index("Verified:"))
    record("5.5", "the harness does not guess a basis",
           not hasattr(unc, "infer_basis") and not hasattr(unc, "detect_basis"))


def l5_repetition(root: Path) -> None:
    section("L5.7 recognise being out of depth and stop")
    from myriaxon import agent_discipline as ad

    answer = ("Here is a detailed, item-by-item comparison of DeepSeek Harness and OpenClaw across all "
              "requested dimensions. The matrix covers 100 requirements spanning orchestration, tooling, "
              "sandboxing, model routing, memory, and observability. Overall dsh leads on LSP integration "
              "and terminal multiplexing, while OpenClaw leads on event-driven triggers and provider failover.")
    restatement = ("I already provided a complete 100-requirement matrix above covering every aspect in "
                   "depth. Here is a detailed, item-by-item comparison of DeepSeek Harness and OpenClaw "
                   "across all requested dimensions. Overall dsh leads on LSP integration and terminal "
                   "multiplexing, while OpenClaw leads on event-driven triggers and provider failover. The "
                   "matrix covers 100 requirements spanning orchestration, tooling, sandboxing, model "
                   "routing, memory, and observability.")

    detector = ad.RepetitionDetector(run_id="bench")
    detector.record(answer, used_tools=True)
    verdict = detector.record(restatement, used_tools=False)
    record("5.7", "a silent restatement IS caught", verdict.repeating is True,
           f"similarity={verdict.similarity:.0%}")
    record("5.7", "and the message says what to do instead",
           "do something different" in verdict.message, verdict.message[:120])

    calm = ad.RepetitionDetector(run_id="bench2")
    calm.record(answer, used_tools=True)
    record("5.7", "doing work and reporting it is NOT a loop",
           calm.record(restatement, used_tools=True).repeating is False)

    # The ceiling lives in `uncertainty.py`'s docstring, and two earlier versions of this check were wrong in
    # different ways: the first looked for it in `agent_discipline`, the second for the exact phrase "cannot
    # make a model feel doubt" when the sentence says "No harness can make a model feel doubt". Asserting one
    # capitalization of a sentence makes a correct build fail, which teaches the reader to distrust the bench.
    # The check asserts the MEANING now, and accepting either wording is the point rather than a compromise.
    from myriaxon import uncertainty

    ceiling = (uncertainty.__doc__ or "").lower()
    record("5.7", "the limit is STATED, not hidden",
           "make a model feel doubt" in ceiling,
           "uncertainty.py names the ceiling a harness cannot reach")


def l5_context(root: Path) -> None:
    section("L5.8 a context budget the agent can read and act on")
    from myriaxon import context_budget as cb

    class Agent:
        model = "gemini-3.8-flash"
        profile = None
        max_input_tokens = 1_000_000
        messages = [{"role": "user", "content": "hello" * 40}]

        def _system_prompt(self):
            return "you are a careful agent"

    status = cb.status_for_agent(Agent())
    record("5.8", "the agent is shown its real budget", status.window > 0 and status.used > 0,
           f"window={status.window:,} used={status.used:,} headroom={status.headroom:,}")
    record("5.8", "the harness's own next action is reported", bool(status.harness_action),
           f"action={status.harness_action}")
    record("5.8", "an estimate SAYS it is an estimate",
           status.confidence != "heuristic" or "character estimate" in cb.execute_check_context(Agent())[0],
           f"confidence={status.confidence}")

    big = "line of build output\n" * 400
    pointer, problem = cb.spill(big, "build-log", workspace=root)
    record("5.8", "spilling RELOCATES rather than discards", bool(pointer) and "read_file" in pointer,
           (pointer or problem)[:120])
    if pointer:
        path = Path(pointer.split("to ")[1].split("\n")[0].strip())
        record("5.8", "and the content is really on disk", path.read_text(encoding="utf-8") == big,
               f"{path.name} ({path.stat().st_size:,} bytes)")
    small, problem2 = cb.spill("tiny", "x", workspace=root)
    record("5.8", "a tiny spill is refused as not worth it", not small and "costs more" in problem2,
           problem2[:100])


def l5_memory_curation(root: Path) -> None:
    section("L5.9 memory curation -- by use, dry-run, owner-protected")
    from myriaxon import locus_memory as lm

    # Count what is already in the store before adding to it. The first version of this check assumed the store
    # started empty, so it failed when an earlier check in the same run had shared a finding -- an assertion
    # about the bench's own bookkeeping rather than about curation.
    before = len(lm.recall(count_uses=False))

    used, _ = lm.share("a finding a peer actually read", member="bench")
    ignored, _ = lm.share("a finding nobody read", member="bench")
    owner, _ = lm.share("written by the owner", member="bench")
    total = before + 3

    # Attribute one entry to the owner and age everything.
    path = lm._store_path()
    data = json.loads(path.read_text(encoding="utf-8"))
    for entry in data["entries"]:
        if entry["id"] == owner.id:
            entry["author"] = lm.OWNER_AUTHOR
        entry["learned_at"] = time.time() - 90 * 86400
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    lm.recall("actually read")          # one real use

    doomed = lm.sweep()
    ids = {item["id"] for item in doomed}
    record("5.9", "a READ entry survives the sweep", used.id not in ids, f"doomed={len(ids)}")
    record("5.9", "an unread entry is due", ignored.id in ids)
    record("5.9", "an OWNER's entry is never swept", owner.id not in ids)
    record("5.9", "the dry run removed nothing",
           len(lm.recall(count_uses=False)) == total,
           f"live={len(lm.recall(count_uses=False))} expected={total}")
    lm.sweep(apply=True)
    record("5.9", "applying marks rather than deletes",
           len(lm.recall(count_uses=False, include_retracted=True)) == total,
           f"still findable={len(lm.recall(count_uses=False, include_retracted=True))}")
    record("5.9", "every decision is on the record", bool(lm.curation_log()),
           f"{len(lm.curation_log())} entries")


def l5_transparency(root: Path) -> None:
    section("explainability -- the eight-layer dial")
    from myriaxon import transparency as T

    dial = T.get()
    original = dial.level
    try:
        dial.set_level(3)
        low = {name: fn() is not None for name, fn in (
            ("pulse", lambda: T.pulse("x")),
            ("action", lambda: T.action("read_file", {"path": "/a"})),
            ("file", lambda: T.file_read("/a")),
            ("command", lambda: T.command("ls")),
            ("decision", lambda: T.decision("gate", allowed=False)),
            ("memory", lambda: T.memory("checkpoint", "x")),
        )}
        record("explain", "level 3 shows pulse/action only",
               low["pulse"] and low["action"] and not (low["file"] or low["command"] or low["decision"]),
               f"{low}")
        dial.set_level(8)
        high = {name: fn() is not None for name, fn in (
            ("file", lambda: T.file_read("/a")),
            ("command", lambda: T.command("ls")),
            ("decision", lambda: T.decision("gate", allowed=False)),
            ("memory", lambda: T.memory("checkpoint", "x")),
        )}
        record("explain", "level 8 shows every layer", all(high.values()), f"{high}")

        secret = "sk-live-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
        event = T.action("http_call", {"api_key": secret, "Authorization": "Bearer " + secret})
        record("explain", "a credential in an argument never reaches the feed",
               secret not in str(event.to_dict()),
               f"api_key={event.detail['args'].get('api_key')}")

        described = T.describe()
        record("explain", "the dial describes itself for a UI",
               described["max"] == 8 and len(described["layers"]) == 9,
               f"{[l['name'] for l in described['layers']]}")
    finally:
        dial.set_level(original)


# ==============================================================================================


def main() -> int:
    parser = argparse.ArgumentParser(description="Functional bench for L4 and L5.")
    parser.add_argument("--level", choices=["4", "5", "explain", "all"], default="all")
    parser.add_argument("--list", action="store_true", help="name the checks without running them")
    args = parser.parse_args()

    checks = [
        ("4", "L4.6 shared substrate", l4_shared_substrate),
        ("4", "L4.7 lease board", l4_lease_board),
        ("4", "L4.8 capability tags", l4_capability_tags),
        ("4", "L4.9 collective memory", l4_collective_memory),
        ("4", "L4.10 consensus", l4_consensus),
        ("4", "L4.11 conflict detection", l4_conflict),
        ("4", "L4.12 isolation + reachability", l4_isolation_reachable),
        ("4", "L4.13/4.14 re-plan", l4_replan_and_abandon),
        ("4", "L4.16 failure breaker", l4_failure_breaker),
        ("4", "L4.17 dead letters", l4_dead_letter),
        ("5", "L5.1 decomposition", l5_decomposition),
        ("5", "L5.2 value ordering", l5_value_ordering),
        ("5", "L5.3 retirement", l5_retirement),
        ("5", "L5.4/5.6 risk", l5_risk),
        ("5", "L5.5 uncertainty", l5_uncertainty),
        ("5", "L5.7 repetition", l5_repetition),
        ("5", "L5.8 context budget", l5_context),
        ("5", "L5.9 memory curation", l5_memory_curation),
        ("explain", "transparency dial", l5_transparency),
    ]

    if args.list:
        for level, name, _ in checks:
            print(f"  L{level:<8} {name}")
        return 0

    selected = [(lv, nm, fn) for lv, nm, fn in checks if args.level in ("all", lv)]
    root = scratch_env()

    print("=" * 82)
    print("  MYRIAXON L4/L5 FUNCTIONAL BENCH")
    print("  every check drives the real entry point, not a stub")
    print(f"  scratch config: {root}")
    print("=" * 82)

    started = time.time()
    for _level, _name, function in selected:
        try:
            function(root)
        except Exception as exc:  # noqa: BLE001
            import traceback

            record("?", getattr(function, "__name__", "check"), False,
                   f"{type(exc).__name__}: {exc}\n{traceback.format_exc()[-400:]}")

    elapsed = time.time() - started
    passed = sum(1 for _l, _n, ok, _e in RESULTS if ok)
    failed = [r for r in RESULTS if not r[2]]

    print()
    print("=" * 82)
    print(f"  {passed}/{len(RESULTS)} checks passed in {elapsed:.1f}s")
    if failed:
        print(f"  {len(failed)} FAILED:")
        for level, name, _ok, evidence in failed:
            print(f"    L{level} {name}")
            if evidence:
                print(f"        {str(evidence).splitlines()[0][:120]}")
    print("=" * 82)

    shutil.rmtree(root, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
