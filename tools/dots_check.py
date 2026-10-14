"""Verification for the persistent-agent (dot) layer.

Runs entirely offline against a scratch registry: no API key, no network, no real state
directory touched. Every property asserted here is one a user would notice if it broke --
that a dot remembers its objectives, that a proactive dot only wakes when it has open work,
that a blocked objective is not retried in a loop, and that a corrupt registry degrades
instead of taking the harness down with it.
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from myriaxon import dots  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    results.append((label, bool(ok), detail))


def fresh() -> dots.DotRegistry:
    return dots.DotRegistry(Path(tempfile.mkdtemp(prefix="mx-dots-")) / "dots.json")


def main() -> int:
    # ---- create and persist ------------------------------------------------------------
    registry = fresh()
    dot = registry.create(
        "Sentinel",
        role="dependency watcher",
        instructions="Never upgrade a major version without a note.",
        goals=["Report outdated direct dependencies"],
        proactive=True,
        interval_seconds=60,
    )
    check("created a dot", dot.id and dot.name == "Sentinel")
    check("role recorded", dot.role == "dependency watcher")
    check("objective recorded", len(dot.goals) == 1)
    check("activity log started", any(a.kind == "created" for a in dot.activity))

    reloaded = dots.DotRegistry(registry.path)
    again = reloaded.get("Sentinel")
    check("dot survives a reload", again is not None)
    check("objectives survive a reload", again is not None and len(again.goals) == 1)
    check("instructions survive a reload", again is not None and "major version" in again.instructions)
    check("lookup is case-insensitive", reloaded.get("sentinel") is not None)
    check("lookup by id works", reloaded.get(dot.id) is not None)
    check("unknown name returns None", reloaded.get("nope") is None)

    try:
        registry.create("Sentinel")
        check("duplicate name rejected", False)
    except ValueError:
        check("duplicate name rejected", True)

    # ---- goals -------------------------------------------------------------------------
    goal = dot.add_goal("Write the weekly summary")
    check("goal added", len(dot.open_goals()) == 2)
    check("goal resolved by id", dot.find_goal(goal.id) is goal)
    check("goal resolved by exact title", dot.find_goal("Write the weekly summary") is goal)
    check("goal resolved by partial title", dot.find_goal("weekly") is goal)
    check("ambiguous/absent goal returns None", dot.find_goal("zzz") is None)

    check("status set to active", dot.set_goal_status(goal.id, "active") is not None)
    check("goal is now active", dot.find_goal(goal.id).status == "active")
    check("unknown status rejected", dot.set_goal_status(goal.id, "banana") is None)
    check("note appended", (dot.set_goal_status(goal.id, "active", "needs the API key") or goal).notes[-1] == "needs the API key")

    # next_goal picks by status, not by insertion order: an active goal must win over every
    # pending one, so adding a new objective cannot starve work already in progress.
    nxt = dot.next_goal()
    check("next goal prefers the active goal over a pending one", nxt is not None and nxt.id == goal.id)

    # A blocked goal is never selected while any active or pending goal remains.
    dot.set_goal_status(goal.id, "done")
    dot.set_goal_status(dot.goals[0].id, "blocked")
    check("no selectable goal when all are blocked or done", dot.next_goal() is None)
    check("a blocked goal is still counted as open", len(dot.open_goals()) == 1)

    # ---- proactive gate ----------------------------------------------------------------
    p = registry.create("Worker", goals=["do the thing"], proactive=True, interval_seconds=30)
    check("a brand-new dot is due immediately", p.is_due())
    p.last_run_at = time.time()
    check("not due before the interval", not p.is_due(now=time.time() + 1))
    check("due once the interval has elapsed", p.is_due(now=time.time() + 31))
    p.last_run_at = time.time()
    check("not due immediately after a run", not p.is_due())
    p.proactive = False
    check("a non-proactive dot is never due", not p.is_due(now=time.time() + 9999))
    p.proactive = True
    p.enabled = False
    check("a disabled dot is never due", not p.is_due(now=time.time() + 9999))
    p.enabled = True
    p.set_goal_status(p.goals[0].id, "done")
    check("a dot with no open work is not due", not p.is_due(now=time.time() + 9999))

    idle = registry.create("Idle")
    check("empty proactive prompt when there is no work", idle.proactive_prompt() == "")

    # ---- prompting ---------------------------------------------------------------------
    brief = dot.briefing()
    check("briefing names the dot", "Sentinel" in brief)
    check("briefing states the role", "dependency watcher" in brief)
    check("briefing carries standing instructions", "major version" in brief)
    check("briefing lists objectives", "Report outdated direct dependencies" in brief)
    check("briefing includes recent activity", "created" in brief)

    # The idle case is its own dot with nothing open: a dot with open work must *not* be told to
    # stand down, and a dot with no work must be, or it invents tasks to look busy.
    idle_brief = dots.Dot(id="zz", name="Idle2", role="generalist").briefing()
    check("an objective-less briefing stands the dot down", "Do not invent work" in idle_brief)

    worker = registry.get("Worker")
    worker.set_goal_status(worker.goals[0].id, "pending")
    prompt = worker.proactive_prompt()
    check("proactive prompt targets the objective", "do the thing" in prompt)
    check("first proactive run marks the goal active", worker.find_goal(worker.goals[0].id).status == "active")
    check("attempt counted", worker.find_goal(worker.goals[0].id).attempts == 1)
    check("prompt tells it how to finish", "dot_goal_update" in prompt)

    # ---- registry queries --------------------------------------------------------------
    check("due() finds only due dots", all(d.is_due() for d in registry.due()))
    nxt_due = registry.next_due()
    check("next_due returns a dot and a countdown", nxt_due is not None and nxt_due[1] >= 0)
    check("countdown is bounded by the interval", nxt_due is not None and nxt_due[1] <= nxt_due[0].interval_seconds)

    # ---- rendering ---------------------------------------------------------------------
    listing = dots.format_registry(registry.all())
    check("listing shows every dot", all(d.name in listing for d in registry.all()))
    check("empty registry explains what a dot is", "persistent agent" in dots.format_registry([]))
    detail = dots.format_dot(dot, verbose=True)
    check("verbose view shows the workspace", "workspace" in detail)
    check("verbose view shows activity", "recent activity" in detail)

    # ---- tool handlers -----------------------------------------------------------------
    dots.reset_registry(registry.path)
    text, failed = dots.execute_dot_list({})
    check("dot_list handler works", not failed and "Sentinel" in text)
    text, failed = dots.execute_dot_create({"name": "Fresh", "goals": ["one thing"]})
    check("dot_create handler works", not failed and "Fresh" in text)
    text, failed = dots.execute_dot_create({"name": "Fresh"})
    check("dot_create reports a duplicate as an error", failed)
    text, failed = dots.execute_dot_goal_update({"dot": "Fresh", "add": "another thing"})
    check("dot_goal_update adds", not failed and "another thing" in text)
    text, failed = dots.execute_dot_goal_update({"dot": "Fresh", "goal": "another", "status": "done"})
    check("dot_goal_update completes", not failed and "done" in text)
    text, failed = dots.execute_dot_goal_update({"dot": "Ghost", "add": "x"})
    check("dot_goal_update rejects an unknown dot", failed)

    # ---- resilience --------------------------------------------------------------------
    broken = Path(tempfile.mkdtemp(prefix="mx-dots-bad-")) / "dots.json"
    broken.write_text("{ this is not json", encoding="utf-8")
    recovered = dots.DotRegistry(broken)
    check("a corrupt registry does not raise", recovered.all() == [])
    check("the corrupt file is preserved for recovery", broken.with_suffix(".json.corrupt").exists())

    partial = Path(tempfile.mkdtemp(prefix="mx-dots-part-")) / "dots.json"
    partial.write_text(
        json.dumps({"version": 1, "dots": [{"id": "aa", "name": "Good"}, {"garbage": True}]}),
        encoding="utf-8",
    )
    mixed = dots.DotRegistry(partial)
    check("one bad entry does not lose the good ones", [d.name for d in mixed.all()] == ["Good"])

    # A dot created through the handler must also be deletable through the registry. This is a
    # regression guard as much as a check: a dot created in a way the registry cannot later
    # resolve would show up in the listing and then refuse to be removed.
    check("delete removes a dot", registry.delete("Fresh"))
    check("delete of an unknown dot returns False", not registry.delete("Fresh"))
    check("the deleted dot is gone from the listing", "Fresh" not in dots.format_registry(registry.all()))

    # ---- report ------------------------------------------------------------------------
    for label, ok, detail in results:
        suffix = f"  — {detail}" if detail and not ok else ""
        print(f"  {'PASS' if ok else 'FAIL'}  {label}{suffix}")
    failed_checks = [r for r in results if not r[1]]
    print()
    print(f"{len(results) - len(failed_checks)}/{len(results)} dot checks passed")
    return 1 if failed_checks else 0


if __name__ == "__main__":
    sys.exit(main())
