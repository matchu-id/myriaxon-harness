#!/usr/bin/env python3
"""Verify completion authority.

The claim under test is that a goal cannot be completed by the party that did the work. So most of these checks
assert a REFUSAL, and the ones that allow completion assert exactly which authority permitted it.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from myriaxon import goal_authority as G  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAIL).append(label)
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}" + (f"\n         {detail}" if detail else ""))


def source_of_label(value: str) -> str:
    """The source name a decision's reason will contain for a given input."""
    return G.source_of(value)


def main() -> int:
    print("=== 1. an unknown source resolves to the LEAST authoritative, never to human ===")
    # DSH documents the same inversion for an omitted source. Getting it backwards is how "unattended" quietly
    # becomes "authorised".
    for value in ("", None, "  ", "human ", "HUMAN", "wizard", "system"):
        got = G.source_of(value)
        check(f"{value!r} -> {got}", got in G.SOURCES, got)
    check("a blank source is NOT human", G.source_of("") == G.SOURCE_SYSTEM)
    check("an unknown source is NOT human", G.source_of("root") == G.SOURCE_SYSTEM)
    check("case is normalised", G.source_of("HUMAN") == G.SOURCE_HUMAN)
    check("whitespace is trimmed", G.source_of("  round  ") == G.SOURCE_SYSTEM,
          "`round` is not a source name; the goal-round source is 'goal-round'")

    print()
    print("=== 2. THE FIX: a Locus wake-up cannot complete its own goal ===")
    # This is the failure the module exists to prevent: the model marking its own work done inside the unattended
    # run that did the work.
    decision = G.may_complete(G.TurnContext.unattended(G.SOURCE_DOT))
    check("it is refused", not decision.allowed, decision.render())
    check("the reason names the source", "locus" in decision.reason, decision.reason[:120])
    check("the reason says it is the agent grading itself",
          "grading itself" in decision.reason, decision.reason[:200])
    check("the reason says who may", "human" in decision.reason and "admitted round" in decision.reason,
          decision.reason[:240])
    check("the model's claim is recorded", decision.model_claimed)
    check("no authority is attributed", decision.authority == "")

    print()
    print("=== 3. a sub-agent reporting success cannot complete the parent's goal ===")
    # The same failure mode one level down: a child returns "done" and the parent completes on its word.
    decision = G.may_complete(G.TurnContext.unattended(G.SOURCE_SUBAGENT))
    check("a sub-agent turn is refused", not decision.allowed, decision.render()[:120])
    check("and it says to report instead", "Report the objective as met" in decision.reason,
          decision.reason[:220])
    check("both self-grading sources are named", set(G.SELF_GRADING_SOURCES) == {"locus", "subagent"},
          str(G.SELF_GRADING_SOURCES))

    print()
    print("=== 3b. THE NARROWING: a programmatic caller MAY complete ===")
    # The first version refused everything that was not human, which broke the Loci panel and eight existing
    # tests. That was the signal the rule was drawn in the wrong place: the danger is not "not a human", it is
    # "the model deciding on its own behalf". A dashboard handler or a test driving the state machine is acting
    # on a decision made outside the model's turn.
    for source in (G.SOURCE_SCHEDULE, G.SOURCE_SYSTEM, ""):
        decision = G.may_complete(G.TurnContext.unattended(source))
        check(f"{source or 'no source'} is allowed", decision.allowed, decision.render()[:110])
        check(f"  with the 'caller' authority", decision.authority == "caller", decision.authority)
        check(f"  and the source is recorded", source_of_label(source) in decision.reason,
              decision.reason[:110])
    check("a self-grading source is still refused",
          not G.may_complete(G.TurnContext.unattended(G.SOURCE_DOT)).allowed)

    print()
    print("=== 5. THE FIX: a real human input in an open turn CAN complete ===")
    decision = G.may_complete(G.TurnContext.human())
    check("it is allowed", decision.allowed, decision.render())
    check("the authority is 'human'", decision.authority == "human", decision.authority)
    check("the rendering says so", "human authority" in decision.render(), decision.render())

    print()
    print("=== 6. claiming the human source without human input does NOT work ===")
    # The field is set by the input path, not by the caller. A turn that says it is human but carries no observed
    # input is refused -- otherwise the label is just a string the model can write.
    forged = G.TurnContext(source=G.SOURCE_HUMAN, human_input=False)
    decision = G.may_complete(forged)
    check("a human LABEL without input is refused", not decision.allowed, decision.render()[:120])
    check("and it is not attributed to a person", decision.authority == "")

    print()
    print("=== 7. the admitted round may complete the goal ===")
    stamp = G.RoundStamp(goal_id="g1", revision=1, round=3)
    decision = G.may_complete(
        G.TurnContext(source=G.SOURCE_ROUND, stamp=stamp), admitted=stamp
    )
    check("it is allowed", decision.allowed, decision.render())
    check("the authority is 'round'", decision.authority == "round")
    check("the rendering distinguishes it from a person",
          "model claimed it" in decision.render(), decision.render())

    print()
    print("=== 8. THE FIX: a DIFFERENT round of the same goal cannot ===")
    # Matching all three of (goalId, revision, round) is what stops this.
    admitted = G.RoundStamp(goal_id="g1", revision=1, round=3)
    for label, other in (
        ("a later round", G.RoundStamp("g1", 1, 4)),
        ("an earlier round", G.RoundStamp("g1", 1, 2)),
        ("another goal", G.RoundStamp("g2", 1, 3)),
        ("a superseded revision", G.RoundStamp("g1", 2, 3)),
    ):
        decision = G.may_complete(G.TurnContext(source=G.SOURCE_ROUND, stamp=other), admitted=admitted)
        check(f"{label} is refused", not decision.allowed, decision.render()[:110])
        check(f"  the reason names both stamps", other.render() in decision.reason
              and admitted.render() in decision.reason, decision.reason[:150])

    print()
    print("=== 9. a round turn with no stamp, or a goal with no admitted round, is refused ===")
    check("a round turn with no stamp is refused",
          not G.may_complete(G.TurnContext(source=G.SOURCE_ROUND), admitted=admitted).allowed)
    check("an admitted round with no turn stamp is refused",
          not G.may_complete(G.TurnContext(source=G.SOURCE_ROUND, stamp=stamp), admitted=None).allowed)
    check("both missing is refused",
          not G.may_complete(G.TurnContext(source=G.SOURCE_ROUND)).allowed)

    print()
    print("=== 10. the stamp is frozen, so identity cannot be edited after issue ===")
    try:
        stamp.round = 99            # type: ignore[misc]
        check("a stamp cannot be mutated", False, "mutation succeeded")
    except Exception as exc:  # noqa: BLE001
        check("a stamp cannot be mutated", True, type(exc).__name__)
    check("matching is by all three fields",
          G.RoundStamp("g", 1, 1).matches(G.RoundStamp("g", 1, 1))
          and not G.RoundStamp("g", 1, 1).matches(G.RoundStamp("g", 1, 2)))
    check("matching None is false", not G.RoundStamp("g", 1, 1).matches(None))
    check("the digest is stable", G.stamp_digest(stamp) == G.stamp_digest(G.RoundStamp("g1", 1, 3)))
    check("the digest differs per stamp", G.stamp_digest(stamp) != G.stamp_digest(G.RoundStamp("g1", 1, 4)))

    print()
    print("=== 11. a refused completion is RECORDED, not silently dropped ===")
    goal = G.GoalState(goal_id="g1", objective="ship the thing")
    refused = G.may_complete(G.TurnContext.unattended(G.SOURCE_DOT))
    changed = goal.complete(refused)
    check("the state did not change", not changed and goal.state == G.ACTIVE, goal.state)
    check("the refusal is in the history", len(goal.history) == 1, str(goal.history))
    check("it records the reason", "cannot complete" in goal.history[0]["reason"],
          goal.history[0]["reason"][:90])
    check("it records that the model claimed it", goal.history[0]["model_claimed"] is True)

    print()
    print("=== 12. an authorised completion is recorded with its authority ===")
    allowed = G.may_complete(G.TurnContext.human())
    changed = goal.complete(allowed)
    check("the state changed", changed and goal.state == G.COMPLETE, goal.state)
    check("the authority is recorded", goal.completed_by == "human", goal.completed_by)
    check("completing twice is a no-op", not goal.complete(allowed))
    check("the history has both events", len(goal.history) == 2, str(len(goal.history)))

    print()
    print("=== 13. a round-completed goal is distinguishable from a human-completed one ===")
    # This is the deliberate addition over the reference: both are permitted, but an operator reading the log
    # later can tell which happened.
    round_goal = G.GoalState(goal_id="g2", objective="run unattended")
    stamp2 = round_goal.admit_round()
    decision = G.may_complete(
        G.TurnContext(source=G.SOURCE_ROUND, stamp=stamp2), admitted=round_goal.admitted
    )
    round_goal.complete(decision)
    check("it completed", round_goal.state == G.COMPLETE)
    check("the authority is 'round', not 'human'", round_goal.completed_by == "round",
          round_goal.completed_by)
    check("the history records the round", round_goal.history[-1]["round"] == stamp2.render(),
          str(round_goal.history[-1]))

    print()
    print("=== 14. admitting a round stamps the CURRENT revision ===")
    revising = G.GoalState(goal_id="g3", objective="v1")
    first = revising.admit_round()
    check("the first round is number 1", first.round == 1 and first.revision == 1, first.render())
    revising.bump_revision()
    second = revising.admit_round()
    check("after a revision bump the round carries it", second.revision == 2, second.render())
    check("the old stamp no longer matches", not second.matches(first))
    check("the bump cleared the admitted round", revising.admitted is second)
    # The consequence: an objective edit cannot be completed by the round admitted for the old wording.
    decision = G.may_complete(G.TurnContext(source=G.SOURCE_ROUND, stamp=first),
                              admitted=revising.admitted)
    check("the pre-edit round cannot complete the revised goal", not decision.allowed,
          decision.render()[:110])

    print()
    print("=== 15. blocked rounds are capped, so a stuck goal reports instead of looping ===")
    stuck = G.GoalState(goal_id="g4", objective="stuck")
    report, message = stuck.block("waiting on a decision")
    check("the first block does not report", not report, message)
    check("the state is blocked", stuck.state == G.BLOCKED)
    check("the count is tracked", stuck.blocked_rounds == 1, str(stuck.blocked_rounds))
    stuck.block("same thing")
    report, message = stuck.block("same thing")
    check("the cap reports rather than retrying", report, message[:120])
    check("the message says repeating is not difficulty", "not difficulty" in message, message[:180])

    print()
    print("=== 16. progress clears the blocked streak ===")
    # A goal that recovers should not be penalised for an old stall.
    stuck.unblock()
    check("the count resets", stuck.blocked_rounds == 0, str(stuck.blocked_rounds))
    check("the state returns to active", stuck.state == G.ACTIVE, stuck.state)
    report, _ = stuck.block("once more")
    check("a fresh block starts the count again", not report)
    check("the unblock is in the history",
          any(h["event"] == "unblocked" for h in stuck.history), str(stuck.history[-3:]))

    print()
    print("=== 17. the source vocabulary is closed ===")
    check("six sources", len(G.SOURCES) == 6, str(G.SOURCES))
    check("only human and round are authoritative",
          set(G.AUTHORITATIVE_SOURCES) == {G.SOURCE_HUMAN, G.SOURCE_ROUND},
          str(G.AUTHORITATIVE_SOURCES))
    check("the states are closed",
          set(G.STATES) == {"active", "paused", "blocked", "complete"}, str(G.STATES))
    check("the block cap is positive", G.BLOCKED_ROUND_LIMIT >= 1, str(G.BLOCKED_ROUND_LIMIT))

    print()
    print("=== 18. an unattended helper is explicit about its lack of authority ===")
    # `human()` and `unattended()` are the two constructors, so a call site has to pick one and say which it is.
    check("human() sets both fields", G.TurnContext.human().human_input is True
          and G.TurnContext.human().source == G.SOURCE_HUMAN)
    check("unattended() clears human input",
          G.TurnContext.unattended(G.SOURCE_DOT).human_input is False)
    check("unattended() can carry a round stamp",
          G.TurnContext.unattended(G.SOURCE_ROUND, G.RoundStamp("g", 1, 1)).stamp is not None)

    print()
    print(f"=== {len(PASS)} passed, {len(FAIL)} failed ===")
    for label in FAIL:
        print(f"  FAILED: {label}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
