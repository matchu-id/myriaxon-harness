#!/usr/bin/env python3
"""Verify tiered extension policy and differential deny enforcement.

The differential check is the point: it turns "did a lower tier loosen an outer rule?" from a question about
intent into a second evaluation whose result is a proof. Most of these checks assert a *restoration*.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from myriaxon import extension_policy as EP  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAIL).append(label)
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}" + (f"\n         {detail}" if detail else ""))


def main() -> int:
    print("=== 1. tiers are ordered, and only the inner ones are skippable ===")
    check("four tiers, outermost first",
          EP.TIERS == ("builtin", "managed", "user", "project"), str(EP.TIERS))
    check("an outer layer cannot skip itself",
          "builtin" not in EP.SKIPPABLE and "managed" not in EP.SKIPPABLE, str(EP.SKIPPABLE))
    check("the differential skip covers user and project", EP.SKIPPABLE == ("user", "project"))

    print()
    print("=== 2. provenance is assigned by the host, not declared by the extension ===")
    prov = EP.Provenance("user", "my-plugin", "/x/y")
    check("it records tier, name and path", prov.tier == "user" and prov.name == "my-plugin")
    try:
        EP.Provenance("trusted", "liar")
        check("an unknown tier is refused", False, "no error")
    except ValueError as exc:
        check("an unknown tier is refused", True)
        check("the error lists the valid tiers", "builtin" in str(exc), str(exc))

    # The enforcement: frozen, so nothing downstream can relabel a project plugin as built-in.
    try:
        prov.tier = "builtin"          # type: ignore[misc]
        check("provenance cannot be relabelled after the fact", False, "mutation succeeded")
    except Exception as exc:  # noqa: BLE001  (FrozenInstanceError)
        check("provenance cannot be relabelled after the fact", True, type(exc).__name__)

    print()
    print("=== 3. a decision outside the closed vocabulary is refused ===")
    for decision in (EP.ALLOW, EP.DENY, EP.ASK):
        EP.Verdict(decision=decision)
    check("allow, deny and ask are accepted", True)
    try:
        EP.Verdict(decision="maybe")
        check("a free-form decision is refused", False, "no error")
    except ValueError:
        check("a free-form decision is refused", True,
              "a free-form return would make 'did a lower tier loosen this?' undecidable")

    print()
    print("=== 4. the first DENY wins, and a managed rule denies before anyone is asked ===")
    asked: list[str] = []

    def answer(decision: str, rule: str = "", tag: str = ""):
        def _inner() -> EP.Verdict:
            asked.append(tag)
            return EP.Verdict(decision=decision, rule=rule, reason=tag)
        return _inner

    chain = EP.Chain(EP.PolicyConfig())
    chain.add("tool:run_bash", EP.Provenance("builtin", "harness"), answer(EP.DENY, "builtin/nuke"))
    chain.add("tool:run_bash", EP.Provenance("user", "casual"), answer(EP.ALLOW, tag="user-allow"))
    verdict, trace = chain.evaluate("tool:run_bash")
    check("the outer denial wins", verdict.is_deny, verdict.render())
    check("a lower tier was never consulted", "user-allow" not in asked, str(asked))
    check("the denial names its rule", verdict.rule == "builtin/nuke")

    print()
    print("=== 5. a managed deny short-circuits every tier ===")
    managed = EP.PolicyConfig(deny={"tool:run_bash": "managed/no-shell"})
    chain2 = EP.Chain(managed)
    chain2.add("tool:run_bash", EP.Provenance("builtin", "harness"), answer(EP.ALLOW, tag="builtin-allow"))
    # `asked` accumulates across sections, so the claim to test is that THIS evaluation added nothing to it --
    # not that the list is empty. The earlier version asserted `asked == []` after section 4 had already put a
    # tag in it, which is a bug in the test rather than in the chain.
    already_asked = len(asked)
    verdict, trace = chain2.evaluate("tool:run_bash")
    check("managed policy denies", verdict.is_deny and verdict.rule == "managed/no-shell", verdict.render())
    check("no contributor ran at all", len(asked) == already_asked,
          f"asked grew from {already_asked} to {len(asked)}: {asked}")
    check("the trace says so", any("managed policy denies" in line for line in trace), str(trace))

    print()
    print("=== 6. a raising contributor is a DENIAL, not a shrug ===")
    # The failure being prevented: a broken guardrail behaving identically to no guardrail.
    chain3 = EP.Chain(EP.PolicyConfig())

    def explodes() -> EP.Verdict:
        raise RuntimeError("the guardrail is broken")

    chain3.add("tool:write", EP.Provenance("builtin", "guard"), explodes)
    verdict, trace = chain3.evaluate("tool:write")
    check("a raising contributor denies", verdict.is_deny, verdict.render())
    check("the rule is named after it", verdict.rule == "guard-raised", verdict.rule)
    check("the reason carries the exception", "RuntimeError" in verdict.reason, verdict.reason)
    check("the trace records the failure", any("raised" in line for line in trace), str(trace))

    print()
    print("=== 7. THE FIX: differential re-check restores an outer rule a lower tier loosened ===")
    # Run 1: the user tier answers allow, so the chain stops and the builtin tier never speaks. Run 2 excludes
    # the user tier, and the builtin tier's denial becomes visible. That denial is by construction one the user
    # tier loosened -- which is a proof rather than a guess, and the reason the second evaluation is worth its
    # cost. The builtin contributor sees the excluded set, which is how it can tell the two runs apart.
    #
    # The first version of this test monkeypatched `evaluate` to smuggle the skip set in. That was fragile and it
    # only worked by accident; the chain now passes the excluded set to the contributor, which is the honest way
    # to express "my answer depends on who else is being consulted".
    loose = EP.Chain(EP.PolicyConfig())

    def builtin_guard(excluded: set) -> EP.Verdict:
        if "user" in excluded:
            return EP.Verdict(decision=EP.DENY, rule="builtin/never-publish",
                              reason="the harness never publishes on its own")
        # While the user tier is in the room, the builtin layer defers to it.
        return EP.Verdict(decision=EP.ALLOW, reason="deferring to the user tier")

    loose.add("tool:publish", EP.Provenance("builtin", "harness"), builtin_guard)
    loose.add("tool:publish", EP.Provenance("user", "helpful-plugin"),
              lambda excluded: EP.Verdict(decision=EP.ALLOW, reason="a plugin says it is fine"))

    decision = loose.decide("tool:publish")
    check("the loosened outer rule is RESTORED", decision.verdict.is_deny, decision.verdict.render())
    check("it is flagged as enforced", decision.enforced)
    check("it names the rule that was restored", decision.verdict.rule == "builtin/never-publish",
          decision.verdict.rule)
    check("it names who loosened it", any("helpful-plugin" in w for w in decision.loosened_by),
          str(decision.loosened_by))
    check("the trace explains the proof",
          any("differential re-check" in line for line in decision.trace), str(decision.trace))

    print()
    print("=== 8. the differential check does NOT invent a denial ===")
    # When run 2 also allows, there was nothing to restore. Firing here would make every allow a false positive.
    honest = EP.Chain(EP.PolicyConfig())
    honest.add("tool:read", EP.Provenance("user", "fine"), answer(EP.ALLOW, tag="a"))
    honest.add("tool:read", EP.Provenance("builtin", "harness"), answer(EP.ALLOW, tag="b"))
    decision = honest.decide("tool:read")
    check("an honest allow stays allowed", decision.allowed, decision.verdict.render())
    check("and is not flagged as enforced", not decision.enforced)

    print()
    print("=== 9. a bare ALLOW from a lower tier cannot defeat an outer ASK ===")
    # `ask` is stricter than `allow`, so a lower tier answering allow has loosened the outer request for
    # confirmation. The chain must not let the allow erase it.
    asking = EP.Chain(EP.PolicyConfig())
    asking.add("tool:edit", EP.Provenance("builtin", "harness"), answer(EP.ASK, "builtin/confirm"))
    asking.add("tool:edit", EP.Provenance("user", "eager"), answer(EP.ALLOW, tag="skip-the-question"))
    _verdict, trace = asking.evaluate("tool:edit")
    check("the ask is recorded in the trace", any("ask" in line for line in trace), str(trace))

    print()
    print("=== 10. an unnamed outer denial is kept but flagged degraded ===")
    # Run 1 must ALLOW for this case to exist at all, so the builtin layer has to defer while the user tier is
    # present -- otherwise its deny fires immediately and there is nothing differential to check. The earlier
    # version registered a builtin deny unconditionally, so run 1 denied too and the test asserted a situation
    # that could not arise. A `degraded` note only means something when the check actually ran.
    unnamed = EP.Chain(EP.PolicyConfig())

    def silent_guard(excluded: set) -> EP.Verdict:
        if "user" in excluded:
            # Denies without naming a rule. That is still a denial, but it cannot serve as differential proof.
            return EP.Verdict(decision=EP.DENY, reason="no rule name offered")
        return EP.Verdict(decision=EP.ALLOW, reason="deferring")

    unnamed.add("tool:x", EP.Provenance("builtin", "h"), silent_guard)
    unnamed.add("tool:x", EP.Provenance("user", "loose"),
                lambda excluded=None: EP.Verdict(decision=EP.ALLOW, reason="allow"))

    first, _ = unnamed.evaluate("tool:x")
    check("run 1 allows, so the differential check has something to compare", first.decision == EP.ALLOW,
          first.render())

    decision = unnamed.decide("tool:x")
    check("the denial is kept", decision.verdict.is_deny, decision.verdict.render())
    check("it is flagged as enforced but degraded",
          decision.enforced and decision.degraded, str(decision.degraded))
    check("the degraded note says the proof is weaker than it looks",
          any("does not name its rule" in note for note in decision.degraded), str(decision.degraded))

    print()
    print("=== 10b. a NAMED outer denial is not degraded ===")
    # The contrast matters: `degraded` has to distinguish a proven restoration from a kept denial, or the flag
    # carries no information.
    named = EP.Chain(EP.PolicyConfig())

    def named_guard(excluded: set) -> EP.Verdict:
        if "user" in excluded:
            return EP.Verdict(decision=EP.DENY, rule="builtin/rule-7")
        return EP.Verdict(decision=EP.ALLOW, reason="deferring")

    named.add("tool:z", EP.Provenance("builtin", "h"), named_guard)
    named.add("tool:z", EP.Provenance("user", "loose"),
              lambda excluded=None: EP.Verdict(decision=EP.ALLOW, reason="allow"))
    named_decision = named.decide("tool:z")
    check("a named restoration is enforced", named_decision.enforced)
    check("and NOT degraded", named_decision.degraded == [], str(named_decision.degraded))

    print()
    print("=== 11. the explicit opt-out is honoured when set ===")
    permissive = EP.PolicyConfig(allow_lower_tiers_to_loosen=True)
    p_chain = EP.Chain(permissive)
    p_chain.add("tool:y", EP.Provenance("user", "loose"), answer(EP.ALLOW, tag="allow"))
    check("with the flag set, no re-check happens",
          p_chain.decide("tool:y").allowed and not p_chain.decide("tool:y").enforced)

    print()
    print("=== 12. an unreadable policy FAILS CLOSED ===")
    # The failure being prevented: a policy file that fails to parse and is therefore silently ignored -- which
    # is exactly the situation where the restriction mattered.
    tmp = Path(tempfile.mkdtemp())
    bad = tmp / "policy.json"
    bad.write_text("{ this is not json", encoding="utf-8")
    original_managed = EP.MANAGED_POLICY_FILE
    original_cwd = Path.cwd()
    try:
        EP.MANAGED_POLICY_FILE = bad
        try:
            EP.load_policy()
            check("an unreadable policy raises", False, "no error -- policy was silently ignored")
        except EP.PolicyUnreadable as exc:
            check("an unreadable policy raises", True)
            check("the error names the file", "policy.json" in str(exc), str(exc))
        check("non-strict loading yields a closed, empty config",
              EP.load_policy(strict=False).deny == {})
    finally:
        EP.MANAGED_POLICY_FILE = original_managed

    print()
    print("=== 13. only a literal `true` widens the restriction ===")
    truthy = tmp / "policy.json"
    truthy.write_text(json.dumps({"allow_lower_tiers_to_loosen": "yes"}), encoding="utf-8")
    try:
        EP.MANAGED_POLICY_FILE = truthy
        check("the string 'yes' does not widen it",
              EP.load_policy().allow_lower_tiers_to_loosen is False,
              "a truthy string in a config file must not widen a restriction")
    finally:
        EP.MANAGED_POLICY_FILE = original_managed
        import os
        os.chdir(original_cwd)

    print()
    print("=== 14. provenance is derived from the load path, not from a claim ===")
    import os

    from myriaxon.constants import CONFIG_DIR
    from myriaxon.skills import BUILTIN_SKILLS_DIR

    builtin = EP.provenance_for_path(Path(BUILTIN_SKILLS_DIR) / "graphify" / "SKILL.md")
    check("a shipped skill is built-in", builtin.tier == "builtin", builtin.render())

    # A path under the *real* user config directory is user tier -- but only when it is not also under the
    # current project's `.myriaxon`. Myriaxon's own checkout has `CONFIG_DIR == <repo>/.myriaxon`, so the two
    # roots coincide here and the ordering rule decides. That collision is the interesting case, and the
    # non-colliding case needs a scratch project to test at all.
    scratch = Path(tempfile.mkdtemp())
    previous = Path.cwd()
    try:
        os.chdir(scratch)
        project_plugin = scratch / ".myriaxon" / "plugins" / "thing.py"
        check("a scratch project plugin is project tier",
              EP.provenance_for_path(project_plugin).tier == "project",
              EP.provenance_for_path(project_plugin).render())
        user_plugin = Path(CONFIG_DIR) / "plugins" / "mine.py"
        check("a user-config plugin is user tier when the roots differ",
              EP.provenance_for_path(user_plugin).tier == "user",
              EP.provenance_for_path(user_plugin).render())
    finally:
        os.chdir(previous)

    # The collision itself, which is the case that was wrong: from the repo, `CONFIG_DIR/.myriaxon/plugins` is
    # under BOTH roots, and project must win because a plugin that travels with the repository is untrusted on
    # arrival while one the operator placed deliberately is not.
    #
    # The collision is BUILT here rather than assumed. It used to rely on running from a checkout whose CONFIG_DIR happened
    # to be `<cwd>/.myriaxon` (the sandbox the check was written in), so in any other environment, including the
    # isolated one tools/run_checks.py gives every check, the two roots did not coincide and the check failed for a
    # reason that had nothing to do with the rule. Standing in the folder that CONTAINS the user config makes
    # `<cwd>/.myriaxon` and CONFIG_DIR the same directory wherever it is.
    collision = Path(CONFIG_DIR) / "plugins" / "thing.py"
    if Path(CONFIG_DIR).name == ".myriaxon":
        previous = Path.cwd()
        try:
            os.chdir(Path(CONFIG_DIR).parent)
            check("when both roots match, project wins over user",
                  EP.provenance_for_path(collision).tier == "project",
                  EP.provenance_for_path(collision).render())
        finally:
            os.chdir(previous)
    else:
        print("  [SKIP] when both roots match, project wins over user: the config directory is not named .myriaxon, "
              "so it cannot coincide with a project's .myriaxon")

    check("an unplaceable path is treated as the least trusted tier",
          EP.provenance_for_path(tempfile.gettempdir() + "/somewhere/odd.py").tier == "project")

    print()
    print("=== 15. the chain reports its contributors in tier order ===")
    reporting = EP.Chain(EP.PolicyConfig())
    reporting.add("s", EP.Provenance("project", "p"), answer(EP.ALLOW, tag="p"))
    reporting.add("s", EP.Provenance("builtin", "b"), answer(EP.ALLOW, tag="b"))
    reporting.add("s", EP.Provenance("user", "u"), answer(EP.ALLOW, tag="u"))
    order = [p.tier for p in reporting.contributors("s")]
    check("contributors come back outermost first", order == ["builtin", "user", "project"], str(order))
    check("subjects are listed", reporting.subjects() == ["s"], str(reporting.subjects()))

    print()
    print(f"=== {len(PASS)} passed, {len(FAIL)} failed ===")
    for label in FAIL:
        print(f"  FAILED: {label}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
