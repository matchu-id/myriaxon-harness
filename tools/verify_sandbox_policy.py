#!/usr/bin/env python3
"""Verify the sandbox escalation ladder.

The claim under test is that a grant widens access for exactly one call and cannot be inherited. So most of these
checks assert a *refusal*: a spent grant, an equal mode, a missing justification, a target above the ceiling.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from myriaxon import sandbox_policy as S  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAIL).append(label)
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}" + (f"\n         {detail}" if detail else ""))


def main() -> int:
    print("=== 1. three modes, ordered safest first ===")
    check("the ladder has three modes",
          S.MODES == ("read-only", "workspace-write", "danger-full-access"), str(S.MODES))
    check("read-only is the safest", S.rank(S.READ_ONLY) == 0)
    check("danger-full-access is the widest", S.rank(S.DANGER_FULL_ACCESS) == 2)
    check("the ordering is strictly increasing",
          S.rank(S.READ_ONLY) < S.rank(S.WORKSPACE_WRITE) < S.rank(S.DANGER_FULL_ACCESS))

    print()
    print("=== 2. an unknown mode ranks as the SAFEST, so a typo cannot widen anything ===")
    for typo in ("", "workspace_writes", "DANGER", "full-access", "root", None):
        check(f"{typo!r} normalises to read-only", S.normalise(typo) == S.READ_ONLY,
              S.normalise(typo))
    check("a known mode normalises", S.normalise("workspace-write") == S.WORKSPACE_WRITE)
    check("underscores are accepted", S.normalise("workspace_write") == S.WORKSPACE_WRITE)
    check("case is ignored", S.normalise("DANGER-FULL-ACCESS") == S.DANGER_FULL_ACCESS)
    check("an unknown mode never outranks a known one",
          S.rank("nonsense") <= S.rank(S.READ_ONLY))

    print()
    print("=== 3. is_wider is STRICTLY wider ===")
    check("workspace-write is wider than read-only",
          S.is_wider(S.WORKSPACE_WRITE, S.READ_ONLY))
    check("read-only is NOT wider than workspace-write",
          not S.is_wider(S.READ_ONLY, S.WORKSPACE_WRITE))
    check("a mode is not wider than ITSELF", not S.is_wider(S.WORKSPACE_WRITE, S.WORKSPACE_WRITE),
          "asking for the mode already in force is not an escalation")

    print()
    print("=== 4. a policy starts at its configured mode and cannot exceed its ceiling ===")
    policy = S.SandboxPolicy(S.WORKSPACE_WRITE, workspace="/work")
    check("the mode is set", policy.mode == S.WORKSPACE_WRITE)
    check("the effective mode is the run's own", policy.effective_mode() == S.WORKSPACE_WRITE)
    check("there are no live grants", policy.live_grants() == [])

    clamped = S.SandboxPolicy(S.DANGER_FULL_ACCESS, ceiling=S.WORKSPACE_WRITE)
    check("a configured mode above the ceiling is CLAMPED", clamped.mode == S.WORKSPACE_WRITE,
          "a config file must not be able to widen what the deployment allows")

    container = S.SandboxPolicy(S.DANGER_FULL_ACCESS, ceiling=S.READ_ONLY)
    check("clamping works for the safest ceiling too", container.mode == S.READ_ONLY)

    print()
    print("=== 5. THE FIX: a grant widens for ONE call ===")
    decision = policy.request(S.DANGER_FULL_ACCESS, "install the missing package")
    check("the escalation is allowed", decision.allowed, decision.reason)
    check("the mode is the requested one", decision.mode == S.DANGER_FULL_ACCESS)
    check("a grant was issued", decision.grant is not None)
    check("it is live", policy.live_grants() == [decision.grant])
    check("the effective mode is the WIDER one", policy.effective_mode(decision.grant) == S.DANGER_FULL_ACCESS)

    print()
    print("=== 6. THE FIX: the grant is spent, and a spent grant widens nothing ===")
    check("consuming it succeeds", policy.consume(decision.grant))
    check("it is marked spent", decision.grant.spent)
    check("no live grants remain", policy.live_grants() == [])
    check("the effective mode falls back to the run's own",
          policy.effective_mode(decision.grant) == S.WORKSPACE_WRITE,
          "per-call truth: a forgotten grant must not keep widening calls")
    check("consuming it AGAIN fails", not policy.consume(decision.grant),
          "a grant that can be spent twice is a standing permission")
    check("consuming None fails", not policy.consume(None))

    print()
    print("=== 7. a second call needs its OWN grant ===")
    second = S.SandboxPolicy(S.WORKSPACE_WRITE)
    first = second.request(S.DANGER_FULL_ACCESS, "one install")
    second.consume(first.grant)
    # The next call passes no grant, which is what a model that forgot the first one would do.
    check("with no fresh grant the mode is the run's own",
          second.effective_mode(None) == S.WORKSPACE_WRITE)
    check("a second request issues a second grant",
          second.request(S.DANGER_FULL_ACCESS, "another install").allowed)
    check("two grants are recorded", len(second.grants) == 2, str(len(second.grants)))

    print()
    print("=== 8. a justification is REQUIRED, not decorative ===")
    strict = S.SandboxPolicy(S.WORKSPACE_WRITE)
    for empty in ("", "   ", "\n", None):
        refused = strict.request(S.DANGER_FULL_ACCESS, empty)
        check(f"justification {empty!r} is refused", not refused.allowed)
        check(f"  and the reason names the argument", S.ARG_JUSTIFICATION in refused.reason,
              refused.reason[:100])
        check(f"  and it is flagged retryable", refused.retryable,
              "the caller can fix this by re-asking correctly")
    check("a real justification is accepted",
          strict.request(S.DANGER_FULL_ACCESS, "needs network").allowed)
    check("the justification is trimmed and kept",
          strict.grants[-1].justification == "needs network", repr(strict.grants[-1].justification))

    print()
    print("=== 9. THE FIX: escalation must be STRICTLY wider ===")
    equal = S.SandboxPolicy(S.WORKSPACE_WRITE)
    refused = equal.request(S.WORKSPACE_WRITE, "same mode")
    check("asking for the mode already in force is refused", not refused.allowed)
    check("the reason explains no grant is needed", "strictly wider" in refused.reason,
          refused.reason[:140])
    check("it is retryable, because the fix is to drop the argument", refused.retryable)

    downward = S.SandboxPolicy(S.DANGER_FULL_ACCESS)
    refused = downward.request(S.READ_ONLY, "reduce access")
    check("asking for a NARROWER mode is refused as an escalation", not refused.allowed,
          "narrowing is not escalation; it is a different operation")

    print()
    print("=== 10. a target above the ceiling is refused regardless of justification ===")
    capped = S.SandboxPolicy(S.WORKSPACE_WRITE, ceiling=S.WORKSPACE_WRITE)
    refused = capped.request(S.DANGER_FULL_ACCESS, "a very good reason indeed")
    check("the request is refused", not refused.allowed)
    check("the reason names the ceiling", S.WORKSPACE_WRITE in refused.reason, refused.reason[:140])
    check("it is NOT retryable", not refused.retryable,
          "re-asking cannot help, so saying otherwise would waste the model's turn")
    check("no grant was issued", capped.live_grants() == [])

    print()
    print("=== 11. escalation can be disabled for a run ===")
    locked = S.SandboxPolicy(S.WORKSPACE_WRITE, allow_escalation=False)
    refused = locked.request(S.DANGER_FULL_ACCESS, "please")
    check("the request is refused", not refused.allowed)
    check("the reason says escalation is disabled", "disabled" in refused.reason, refused.reason)
    check("it is not retryable, so no retry hint is offered",
          not refused.retryable and refused.hint == "",
          "offering a retry shape that cannot work would waste the model's turn")
    # The message the MODEL receives is `denial()`, not the structured `hint` field. Checking the field was the
    # wrong assertion: for a non-retryable refusal the field is deliberately empty and the guidance lives in the
    # text appended to the marker.
    check("the denial tells the model re-asking will not help",
          "will not help" in locked.denial(), locked.denial()[:160])
    check("it says to report rather than work around it",
          "Report the limitation" in locked.denial(), locked.denial()[:200])

    print()
    print("=== 12. THE FIX: the retry hint travels INSIDE the denial ===")
    # Their reason: the sanctioned retry must not depend on the model recalling the tool description. Knowing you
    # may ask is not the same as knowing how.
    hinting = S.SandboxPolicy(S.WORKSPACE_WRITE)
    denial = hinting.denial()
    check("the denial carries their stable marker",
          denial.startswith(S.DENIAL_MARKER.format(mode=S.WORKSPACE_WRITE)), denial[:80])
    check("the marker names the mode", S.WORKSPACE_WRITE in denial, denial[:80])
    check("the hint names the exact argument", S.ARG_MODE in denial, denial[:200])
    check("the hint names the justification argument", S.ARG_JUSTIFICATION in denial)
    check("it gives a concrete value to pass", S.DANGER_FULL_ACCESS in denial, denial[:250])
    check("it says the grant is one-call", "this call only" in denial, denial[:280])
    check("it warns a later call needs its own", "needs its own" in denial)
    check("it tells the model to report rather than work around when escalation is off",
          "will not help" in S.SandboxPolicy(S.WORKSPACE_WRITE, allow_escalation=False).denial())

    print()
    print("=== 13. the plain-text marker is stable enough for tooling to match ===")
    for mode in S.MODES:
        rendered = S.DENIAL_MARKER.format(mode=mode)
        check(f"the marker for {mode} is stable", rendered == f"[sandbox: file access denied under {mode} mode]",
              rendered)

    print()
    print("=== 14. the two escalation arguments are validated as a PAIR ===")
    # Each alone is useless differently: a mode with no reason cannot be judged afterwards, and a reason with no
    # mode asks for nothing.
    check("neither given is fine", S.validate_escalation_arguments({}) == "")
    check("both given is fine",
          S.validate_escalation_arguments({S.ARG_MODE: "danger-full-access",
                                           S.ARG_JUSTIFICATION: "needs network"}) == "")
    problem = S.validate_escalation_arguments({S.ARG_MODE: "danger-full-access"})
    check("mode without justification is a problem", problem != "")
    check("  and the problem names the missing one", S.ARG_JUSTIFICATION in problem, problem[:120])
    problem = S.validate_escalation_arguments({S.ARG_JUSTIFICATION: "why not"})
    check("justification without mode is a problem", problem != "")
    check("  and says it asks for nothing", "asks for nothing" in problem, problem[:140])
    check("a non-dict is tolerated", S.validate_escalation_arguments("not a dict") == "")

    print()
    print("=== 15. the schema fragment declares both arguments ===")
    schema = S.escalation_arguments()
    check("both properties are present", set(schema) == {S.ARG_MODE, S.ARG_JUSTIFICATION}, str(set(schema)))
    check("the mode is a closed enum", schema[S.ARG_MODE]["enum"] == list(S.MODES),
          str(schema[S.ARG_MODE]["enum"]))
    check("the mode description says it must be strictly wider",
          "strictly" in schema[S.ARG_MODE]["description"].lower())
    check("the justification is described as read by a person",
          "person" in schema[S.ARG_JUSTIFICATION]["description"])

    print()
    print("=== 16. the policy renders a readable account of itself ===")
    shown = S.SandboxPolicy(S.WORKSPACE_WRITE, ceiling=S.DANGER_FULL_ACCESS)
    shown.request(S.DANGER_FULL_ACCESS, "install a package")
    shown.denials += 1
    rendered = shown.render()
    check("it names the mode and ceiling", "workspace-write" in rendered and "danger-full-access" in rendered)
    check("it lists what the mode permits", "permits:" in rendered)
    check("it shows the grant", "grant danger-full-access" in rendered)
    check("it counts the denials", "1 denial" in rendered, rendered)

    print()
    print("=== 17. policy_from_context respects the existing container flag ===")
    class Ctx:
        sandbox_enabled = False
        workspace = "/work"

    host = S.policy_from_context(Ctx())
    check("a plain run may escalate", host.allow_escalation)
    check("its ceiling is the maximum", host.ceiling == S.DANGER_FULL_ACCESS)

    class Boxed:
        sandbox_enabled = True
        workspace = "/work"

    container = S.policy_from_context(Boxed())
    check("with the container on, escalation is unavailable", not container.allow_escalation,
          "the container IS the boundary; widening past it would defeat enabling it")
    check("and the ceiling is the container's own", container.ceiling == S.WORKSPACE_WRITE)

    print()
    print(f"=== {len(PASS)} passed, {len(FAIL)} failed ===")
    for label in FAIL:
        print(f"  FAILED: {label}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
