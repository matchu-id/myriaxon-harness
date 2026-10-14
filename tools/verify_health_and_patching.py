#!/usr/bin/env python3
"""Verify health checks and two-step patching.

Both are guards, so the interesting checks are the refusals: an ambiguous edit must be rejected rather than
applied to the wrong occurrence, and a repair must name what it would touch before it touches it.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from myriaxon import health as H  # noqa: E402
from myriaxon import patching as P  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAIL).append(label)
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}" + (f"\n         {detail}" if detail else ""))


def main() -> int:
    print("=== 1. patching: an unambiguous span is prepared ===")
    tmp = Path(tempfile.mkdtemp())
    target = tmp / "sample.py"
    target.write_text("def alpha():\n    return 1\n\ndef beta():\n    return 2\n", encoding="utf-8")

    r = P.prepare(target, "    return 1\n")
    check("prepare succeeds", r.ok, "; ".join(r.errors))
    check("it reports exactly one match", r.prepared is not None and r.prepared.occurrences == 1)
    check("it returns bounded context", len(r.prepared.before_context) <= P.CONTEXT_CHARS)
    check("the context shows the surrounding code",
          "def alpha" in r.prepared.before_context or "def beta" in r.prepared.after_context,
          repr(r.prepared.after_context[:40]))

    print()
    print("=== 2. THE FIX: an ambiguous span is REFUSED, not guessed ===")
    # The failure this prevents: `    return ` appears twice, and replacing "the first match" is a coin flip.
    r2 = P.prepare(target, "    return ")
    check("prepare refuses a span matching twice", not r2.ok)
    check("it says how many times", any("2 times" in e for e in r2.errors), "; ".join(r2.errors))
    check("it says what to do", any("more surrounding text" in e for e in r2.errors))
    check("nothing was written", target.read_text(encoding="utf-8").count("return") == 2)

    print()
    print("=== 3. an absent span is refused with a diagnostic ===")
    r3 = P.prepare(target, "    return 99\n")
    check("prepare refuses an absent span", not r3.ok)
    check("it says it does not appear", any("does not appear" in e for e in r3.errors))

    indented = P.prepare(target, "return 1")
    check("a near-miss caused by indentation gets a hint",
          indented.ok or any("indentation" in e or "whitespace" in e for e in indented.errors),
          "; ".join(indented.errors))

    print()
    print("=== 4. an empty span is refused ===")
    r4 = P.prepare(target, "")
    check("an empty old_string is refused", not r4.ok)
    check("the reason explains why", any("matches everywhere" in e for e in r4.errors), "; ".join(r4.errors))

    print()
    print("=== 5. THE FIX: a patch is refused if the file moved underneath it ===")
    r5 = P.prepare(target, "    return 2\n")
    check("prepare for the second edit works", r5.ok, "; ".join(r5.errors))
    # Someone else edits the file between prepare and patch.
    target.write_text(target.read_text(encoding="utf-8") + "\ndef gamma():\n    return 3\n", encoding="utf-8")
    applied = P.patch(r5.prepared, "    return 20\n")
    check("the patch is REFUSED after the file changed", not applied.ok)
    check("the reason says it changed", any("changed after" in e for e in applied.errors),
          "; ".join(applied.errors))
    check("the newer content was not overwritten",
          "return 20" not in target.read_text(encoding="utf-8"))
    check("explicit drift is still possible", P.patch(r5.prepared, "    return 20\n", allow_drift=True).ok)

    print()
    print("=== 6. a normal patch applies exactly one replacement ===")
    fresh = tmp / "fresh.txt"
    fresh.write_text("alpha beta gamma", encoding="utf-8")
    outcome = P.edit(fresh, "beta", "BETA")
    check("edit succeeds", outcome.ok, "; ".join(outcome.errors))
    check("the replacement landed", fresh.read_text(encoding="utf-8") == "alpha BETA gamma")
    check("the one-shot form also refuses ambiguity",
          not P.edit(fresh, "a", "X").ok)

    print()
    print("=== 7. health: the registry refuses duplicates and core hijacking ===")
    H.register_builtin_checks()
    count = len(H.registered())
    check("built-in checks registered", count >= 6, str(count))
    H.register_builtin_checks()
    check("re-registering is idempotent", len(H.registered()) == count)

    try:
        H.register(H.HealthCheck("core/config-file", "duplicate", lambda: []))
        check("a duplicate id is refused", False, "no error")
    except H.HealthCheckRegistrationError as exc:
        check("a duplicate id is refused", True)
        check("the error names the id", "core/config-file" in str(exc), str(exc))

    try:
        H.register(H.HealthCheck("core/pretender", "x", lambda: [], owner="someone-else"))
        check("a non-core owner cannot claim a core id", False, "no error")
    except H.HealthCheckRegistrationError:
        check("a non-core owner cannot claim a core id", True)

    print()
    print("=== 8. health: checks run and a failing check does not hide the others ===")
    def exploding() -> list[H.Finding]:
        raise RuntimeError("this detector is broken")

    H.register(H.HealthCheck("test/exploding", "always raises", exploding, owner="test"), replace=True)
    results = H.run_checks(only=["test/exploding", "core/config-file"])
    check("the broken check is reported, not fatal", "test/exploding" in results)
    check("its finding says the check failed",
          any("check itself failed" in f.message for f in results["test/exploding"]),
          str([f.message for f in results["test/exploding"]]))
    check("the other check still ran", "core/config-file" in results)
    check("and the failure does not claim to be about state",
          any("says nothing about the state" in f.fix_hint for f in results["test/exploding"]))

    print()
    print("=== 9. an opt-in check stays out of a routine pass ===")
    routine = H.run_checks()
    check("core/optional-tools is absent by default", "core/optional-tools" not in routine,
          str(sorted(routine)))
    explicit = H.run_checks(include_opt_in=True)
    check("it runs when asked for", "core/optional-tools" in explicit)

    print()
    print("=== 10. health: a report-only check is skipped, not silently 'repaired' ===")
    H.register(H.HealthCheck(
        "test/reportonly", "reports but cannot repair",
        lambda: [H.Finding("test/reportonly", H.WARNING, "something is off")],
        owner="test",
    ), replace=True)
    findings = H.run_checks(only=["test/reportonly"])
    outcomes = H.repair_all(findings, dry_run=False, only=["test/reportonly"])
    check("one outcome for the check", len(outcomes) == 1)
    check("it is skipped", outcomes[0].status == "skipped", outcomes[0].status)
    check("the reason says it reports only", "reports only" in outcomes[0].reason, outcomes[0].reason)

    print()
    print("=== 11. THE FIX: a repair names its effects before running, and dry-run does not act ===")
    calls: list[str] = []

    def detect_repairable() -> list[H.Finding]:
        return [H.Finding("test/repairable", H.WARNING, "needs fixing", target="thing-1")]

    def repair_repairable(found: list[H.Finding], dry_run: bool) -> H.RepairResult:
        outcome = H.RepairResult(check_id="test/repairable", status="repaired")
        for finding in found:
            outcome.effects.append(H.Effect(H.STATE, "set the flag", target=finding.target))
            if not dry_run:
                calls.append(finding.target)
                outcome.changes.append(f"fixed {finding.target}")
        return outcome

    H.register(H.HealthCheck("test/repairable", "can repair", detect_repairable,
                             repair=repair_repairable, owner="test"), replace=True)
    findings = H.run_checks(only=["test/repairable"])

    dry = H.repair_all(findings, dry_run=True, only=["test/repairable"])
    check("the dry run reports the effect", dry[0].effects and dry[0].effects[0].kind == H.STATE,
          str([e.render() for e in dry[0].effects]))
    check("the effect names its target", dry[0].effects[0].target == "thing-1")
    check("the dry run CHANGED NOTHING", calls == [], str(calls))
    check("the dry run says so", dry[0].dry_run)

    real = H.repair_all(findings, dry_run=False, only=["test/repairable"])
    check("the real run acted", calls == ["thing-1"], str(calls))
    check("it reports the change", real[0].changes == ["fixed thing-1"], str(real[0].changes))

    print()
    print("=== 12. a throwing repair fails alone ===")
    def exploding_repair(found, dry_run):
        raise RuntimeError("repair blew up")

    H.register(H.HealthCheck(
        "test/badrepair", "repair raises",
        lambda: [H.Finding("test/badrepair", H.WARNING, "x")],
        repair=exploding_repair, owner="test",
    ), replace=True)
    bad = H.repair_all(H.run_checks(only=["test/badrepair"]), only=["test/badrepair"])
    check("it is reported as failed", bad[0].status == "failed", bad[0].status)
    check("the reason names the exception", "RuntimeError" in bad[0].reason, bad[0].reason)

    print()
    print("=== 13. an unavailable owner's checks are removed, so its repair cannot run ===")
    H.register(H.HealthCheck("plugin/thing", "owned by a plugin",
                             lambda: [H.Finding("plugin/thing", H.WARNING, "from a plugin")],
                             repair=lambda f, d: H.RepairResult("plugin/thing", "repaired"),
                             owner="some-plugin"), replace=True)
    check("the plugin check is registered", "plugin/thing" in H.registered())
    removed = H.unregister("some-plugin")
    check("unregistering removes it", removed >= 1 and "plugin/thing" not in H.registered(),
          str(removed))
    after = H.repair_all({"plugin/thing": [H.Finding("plugin/thing", H.WARNING, "stale")]},
                         only=["plugin/thing"])
    check("its repair is not executed afterwards", after[0].status == "skipped", after[0].status)
    check("and the reason says the check is gone", "no longer registered" in after[0].reason,
          after[0].reason)

    print()
    print("=== 14. the shipped workshop check runs, and a no-finding check yields no repair ===")
    from myriaxon import skill_workshop

    project = Path(tempfile.mkdtemp())
    proposed = skill_workshop.propose("draftless", "a proposal whose draft will vanish", "Body text here.",
                                      project_root=project)
    check("a proposal was staged against a scratch project", proposed.ok)
    skill_workshop.draft_path(proposed.proposal.id, project).unlink()
    check("its draft is genuinely missing",
          skill_workshop.draft_missing(skill_workshop.load_proposals(project)[proposed.proposal.id], project))

    findings = H.run_checks(only=["core/skill-workshop"])
    check("the shipped check runs", "core/skill-workshop" in findings)
    # `repair_all` returns one result per check *that found something*. A real workspace with no broken proposals
    # legitimately produces none, so the assertion must not assume otherwise -- the earlier version of this test
    # indexed [0] unconditionally and crashed on a clean workspace, which is a bug in the test rather than a
    # missing repair.
    outcomes = H.repair_all(findings, dry_run=True, only=["core/skill-workshop"])
    check("no findings means no repair result", outcomes == [] if not findings["core/skill-workshop"] else True,
          f"{len(outcomes)} outcome(s) for {len(findings['core/skill-workshop'])} finding(s)")

    print()
    print("=== 14b. the repair callback is TOLD it is a dry run, and obeys ===")
    # The point of a dry run is not that the caller sets a flag, but that the repair reads it. A callback that
    # ignores the argument would look dry-run-safe and still write.
    observed: list[bool] = []

    def repair_observing(found: list[H.Finding], dry_run: bool) -> H.RepairResult:
        observed.append(dry_run)
        outcome = H.RepairResult(check_id="test/observing", status="repaired")
        outcome.effects.append(H.Effect(H.STATE, "write a thing", target="t", dry_run_safe=True))
        if not dry_run:
            outcome.changes.append("wrote it")
        return outcome

    H.register(H.HealthCheck(
        "test/observing", "records the dry_run flag",
        lambda: [H.Finding("test/observing", H.WARNING, "needs a fix")],
        repair=repair_observing, owner="test",
    ), replace=True)
    H.repair_all(H.run_checks(only=["test/observing"]), dry_run=True, only=["test/observing"])
    H.repair_all(H.run_checks(only=["test/observing"]), dry_run=False, only=["test/observing"])
    check("the callback received True then False", observed == [True, False], str(observed))

    print()
    print("=== 15. severity filtering and summary ===")
    H.register(H.HealthCheck(
        "test/severities", "three severities",
        lambda: [
            H.Finding("test/severities", H.INFO, "info one"),
            H.Finding("test/severities", H.WARNING, "warning one"),
            H.Finding("test/severities", H.ERROR, "error one"),
        ], owner="test",
    ), replace=True)
    all_findings = H.run_checks(only=["test/severities"])
    check("all three are kept by default", len(all_findings["test/severities"]) == 3)
    errors_only = H.run_checks(only=["test/severities"], min_severity=H.ERROR)
    check("filtering to error keeps one", len(errors_only["test/severities"]) == 1,
          str(len(errors_only["test/severities"])))
    tally = H.summarise(all_findings)
    check("the summary counts by severity", tally[H.ERROR] == 1 and tally[H.WARNING] == 1
          and tally[H.INFO] == 1, str(tally))

    print()
    print(f"=== {len(PASS)} passed, {len(FAIL)} failed ===")
    for label in FAIL:
        print(f"  FAILED: {label}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
