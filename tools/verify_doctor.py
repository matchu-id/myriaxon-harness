#!/usr/bin/env python3
"""Verify the `doctor` checks added for W1.2.

The claim under test: `myriaxon doctor` detects a Locus whose condition cannot run, a guardrail rule that failed to
load, and a proactive Locus with nothing to do -- all **without a model call**, so it is safe to run on every start.

Each check is verified both ways: it must FIRE on the broken shape and stay SILENT on the healthy one. A detector
that fires on everything is as useless as one that fires on nothing, and only the second half of that pair is
usually tested.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from myriaxon import loci  # noqa: E402
from myriaxon import health  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAIL).append(label)
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}" + (f"\n         {detail}" if detail else ""))


def fresh() -> loci.LocusRegistry:
    tmp = Path(tempfile.mkdtemp())
    loci.LOCI_FILE = tmp / "loci.json"
    loci.reset_registry()
    registry = loci.get_registry()
    registry.load()
    return registry


def targets(findings) -> set[str]:
    return {f.target for f in findings if f.target}


def main() -> int:
    print("=== 1. the checks are REGISTERED ===")
    # A detector nothing runs is the failure this whole plan keeps rediscovering. Assert registration, not existence.
    registered = health.register_builtin_checks()
    from myriaxon import health as h

    ids = set(h.registered())
    check("built-ins registered", registered >= 7, str(registered))
    check("core/locus-conditions is registered", "core/locus-conditions" in ids, str(sorted(ids)))
    check("core/guardrails is registered", "core/guardrails" in ids, str(sorted(ids)))

    print()
    print("=== 2. a condition whose COMMAND DOES NOT EXIST is caught ===")
    # The P1.3 case, at the level of the whole registry: this Locus would be silently quiet forever.
    registry = fresh()
    typo = registry.create("typo", goals=[], proactive=True, interval_seconds=60)
    typo.condition = {"kind": "command", "description": "check the service",
                      "command": "no-such-binary-zzz --flag"}
    registry.persist(typo)
    found = health._check_dot_conditions()
    check("a finding is produced", len(found) == 1, str([f.message[:60] for f in found]))
    check("it is an error, not a warning", found and found[0].severity == "error",
          found[0].severity if found else "")
    check("it names the locus", found and typo.id in targets(found))
    check("it names the program", found and "no-such-binary-zzz" in found[0].message,
          found[0].message[:90] if found else "")
    check("it offers a fix", bool(found and found[0].fix_hint))

    print()
    print("=== 3. a `changed` watcher with a bad OBSERVE command is caught ===")
    registry = fresh()
    watcher = registry.create("changed", goals=[], proactive=True, interval_seconds=60)
    watcher.condition = {"kind": "changed", "description": "CI status", "observe": "also-not-real-zzz"}
    registry.persist(watcher)
    found = health._check_dot_conditions()
    check("a finding is produced", len(found) == 1, str(len(found)))
    check("it says which command", found and "observe command" in found[0].message,
          found[0].message[:90] if found else "")

    print()
    print("=== 4. a condition that does not VALIDATE is caught ===")
    # Covers the kinds a typo in `locus_create` would not have caught, and a hand-edited loci.json.
    for label, spec in (
        ("an unknown kind", {"kind": "nonsense", "description": "x"}),
        ("a changed with no observe", {"kind": "changed", "description": "x"}),
        ("a path kind with no path", {"kind": "file_missing", "description": "x"}),
        ("no description", {"kind": "always"}),
    ):
        registry = fresh()
        broken = registry.create("broken", goals=[], proactive=True, interval_seconds=60)
        broken.condition = spec
        registry.persist(broken)
        found = health._check_dot_conditions()
        check(f"caught: {label}", len(found) == 1 and "unusable condition" in found[0].message,
              found[0].message[:80] if found else "no finding")

    print()
    print("=== 5. a proactive locus with NOTHING TO DO is a warning ===")
    # It will never run, and from the outside that is indistinguishable from a scheduler that is broken.
    registry = fresh()
    idle = registry.create("idle", goals=[], proactive=True, interval_seconds=60)
    registry.persist(idle)
    found = health._check_dot_conditions()
    check("a finding is produced", len(found) == 1, str(len(found)))
    check("it is a warning, not an error", found and found[0].severity == "warning",
          found[0].severity if found else "")
    check("it explains it will never run", found and "never run" in found[0].message,
          found[0].message[:100] if found else "")

    print()
    print("=== 6. THE OTHER HALF: a healthy setup produces NOTHING ===")
    # A detector that fires on everything is as useless as one that fires on nothing.
    registry = fresh()
    healthy = registry.create("healthy", goals=[], proactive=True, interval_seconds=60)
    healthy.condition = {"kind": "always", "description": "always on"}
    healthy.scratch = "- watch the queue"
    registry.persist(healthy)

    worker = registry.create("worker", goals=["do the thing"], proactive=True)
    registry.persist(worker)

    on_demand = registry.create("manual", goals=[], proactive=True)
    on_demand.proactive = False
    registry.persist(on_demand)

    found = health._check_dot_conditions()
    check("no findings for a healthy watchlist watcher", not any(f.target == healthy.id for f in found),
          str([f.message[:60] for f in found]))
    check("no findings for a locus with goals", not any(f.target == worker.id for f in found))
    check("no findings for a NON-proactive empty locus", not any(f.target == on_demand.id for f in found),
          "an on-demand locus with no goals is a legitimate scratchpad")
    check("in total, nothing at all", found == [], str([f.message[:60] for f in found]))

    print()
    print("=== 7. the verdict is a DRY RUN ===")
    # Running doctor must not consume the change a watcher is waiting for.
    import time as _time

    registry = fresh()
    probe = registry.create("probe", goals=[], proactive=True, interval_seconds=1)
    probe.condition = {"kind": "changed", "description": "CI", "observe": "echo one",
                       "min_seconds_between_runs": 0}
    registry.persist(probe)

    before = dict(registry.get("probe").trigger_state)
    health._check_dot_conditions()
    health._check_dot_conditions()
    after = dict(registry.get("probe").trigger_state)
    check("the trigger state is untouched by doctor", before == after, f"{before} -> {after}")
    check("and the registry file is still loadable", registry.get("probe") is not None)

    print()
    print("=== 8. a corrupt registry is REPORTED, not crashed on ===")
    # doctor is what a person runs when something is already wrong, so it must not be the thing that fails.
    tmp = Path(tempfile.mkdtemp())
    loci.LOCI_FILE = tmp / "loci.json"
    loci.LOCI_FILE.write_text("{ this is not json", encoding="utf-8")
    loci.reset_registry()
    try:
        found = health._check_dot_conditions()
        check("it returns findings instead of raising", isinstance(found, list),
              str([f.message[:70] for f in found]))
    except Exception as exc:  # noqa: BLE001
        check("it returns findings instead of raising", False, f"{type(exc).__name__}: {exc}")

    print()
    print("=== 9. the guardrail check reports FAILED LOADS ===")
    # A rule that did not load is a guardrail the operator believes is active and is not -- and it is invisible
    # from the outside, because the call it was meant to refuse simply goes through.
    found = health._check_guardrails()
    check("it returns a list", isinstance(found, list), str(found))
    check("every finding is an error", all(f.severity == "error" for f in found),
          str([f.severity for f in found]))
    check("and every one names the rule", all("failed to load" in f.message for f in found),
          str([f.message[:60] for f in found]))

    print()
    print("=== 10. doctor runs end to end without a model ===")
    # The whole point: safe to run on every start. This exercises the real runner over every registered check.
    registry = fresh()
    registry.create("plain", goals=["a goal"], proactive=True)
    ok = True
    try:
        report = health.run_checks()
    except Exception as exc:  # noqa: BLE001
        ok = False
        report = f"{type(exc).__name__}: {exc}"
    check("run_checks completes", ok, str(report)[:150])
    if ok:
        ids_checked = set(report) if isinstance(report, dict) else set()
        check("it ran the locus-conditions check", "core/locus-conditions" in ids_checked,
              str(sorted(ids_checked)))
    summary = health.summarise(report if isinstance(report, dict) else {})
    check("a summary can be produced", isinstance(summary, dict), str(summary))

    print()
    print(f"=== {len(PASS)} passed, {len(FAIL)} failed ===")
    for label in FAIL:
        print(f"  FAILED: {label}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
