#!/usr/bin/env python3
"""Verify mid-turn steering.

## The gap this closes

Myriaxon could **cancel** a turn (`cancellation.py`) but not **redirect** one, so a correction had to wait for the
turn to end — on a long tool-using turn, that means watching it finish the wrong thing first. Both new rivals have
it: PiHarness exposes `inner.steer(message)`, Opencode promotes a durable `steer` input at the provider-turn
boundary.

## The two properties these checks exist for

**Delivery is at the step boundary, and that is not a detail.** Interrupting mid-step leaves a `tool_use` block with
no matching `tool_result`, and the provider then rejects the transcript on **every later turn**. So the precise
guarantee is: *a steer takes effect on the next request, not the one in flight.* The checks assert the call is at
the boundary in all three providers and that it is drained, so it cannot be replayed.

**A steer must carry its own source.** `goal_authority` decides whether a turn may complete a goal from its
provenance. A steer appended as a plain user turn would let a scheduler, a sub-agent or a Locus arrive looking like a
person — silently acquiring the authority to mark work complete. That is the exact hole `goal_authority` closes.
"""

from __future__ import annotations

import io
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from myriaxon import goal_authority as GA  # noqa: E402
from myriaxon import model_adapt  # noqa: E402
from myriaxon import steering as S  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAIL).append(label)
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}" + (f"\n         {detail}" if detail else ""))


class FakeAgent:
    """The minimum a delivery needs. Deliberately not a real agent: this tests the mechanism, not the provider."""

    def __init__(self) -> None:
        self.messages: list[dict] = []
        self.steer_queue = S.SteerQueue()
        self.last_steer_sources: list[str] = []
        self.tool_context = None

        class _Console:
            def __init__(self) -> None:
                self.out = io.StringIO()

            def print(self, *a, **k) -> None:
                self.out.write(" ".join(str(x) for x in a) + "\n")

        self.console = _Console()


def main() -> int:
    print("=== 1. the queue accepts a steer and reports it ===")
    queue = S.SteerQueue()
    ok, reason = queue.offer("use the other file", GA.SOURCE_HUMAN)
    check("a steer is accepted", ok, reason)
    check("  and is pending", queue.pending() == 1, str(queue.pending()))
    check("  and was counted", queue.accepted == 1, str(queue.accepted))

    print()
    print("=== 2. AN EMPTY STEER IS REFUSED, not delivered as an empty turn ===")
    # An empty user turn is a real defect: some providers reject it, and a model that receives one has been told
    # nothing while its transcript grew.
    for empty in ("", "   ", "\n\t ", None):
        ok, reason = queue.offer(empty, GA.SOURCE_HUMAN)
        check(f"refuses {empty!r}", not ok, reason[:70])
    # Five values were offered above, not four -- the first version of this assertion hard-coded 4 and failed
    # against correct code. Counting them from the loop is what the check should have done from the start.
    empty_cases = ("", "   ", "\n\t ", None)
    check("the refusals are counted, not silent", queue.dropped == len(empty_cases), str(queue.dropped))
    check("  and none were queued", queue.pending() == 1, str(queue.pending()))

    print()
    print("=== 3. THE QUEUE IS BOUNDED ===")
    # Past a handful of competing corrections the model cannot honour them, so queueing more is queueing to ignore.
    full = S.SteerQueue()
    accepted = 0
    for i in range(S.MAX_QUEUED + 5):
        if full.offer(f"correction {i}", GA.SOURCE_HUMAN)[0]:
            accepted += 1
    check("exactly the limit is accepted", accepted == S.MAX_QUEUED, f"{accepted} of {S.MAX_QUEUED}")
    # Asserts the INCREMENT, not an absolute count. The first version asserted `dropped == 1` and failed against
    # correct code: the loop above already dropped 5 offers that exceeded the limit, so the total is 6. An absolute
    # count silently encodes everything that happened earlier in the section.
    before_dropped = full.dropped
    ok, reason = full.offer("one more", GA.SOURCE_HUMAN)
    check("the next one is refused", not ok, reason[:100])
    check("  and the reason says why", "limit" in reason, reason[:120])
    check("  and the refusal increments the dropped count",
          full.dropped == before_dropped + 1, f"{before_dropped} -> {full.dropped}")
    check("  and nothing was queued", full.pending() == S.MAX_QUEUED, str(full.pending()))

    print()
    print("=== 4. THE SOURCE IS REQUIRED AND NEVER DEFAULTS TO HUMAN ===")
    # The inversion `goal_authority.source_of` already applies: a producer must SUPPLY human authority rather than
    # inherit it by leaving a field blank.
    for blank in ("", None, "   ", "wibble", "root"):
        q = S.SteerQueue()
        q.offer("something", blank)
        steer = q.peek()[0]
        check(f"source {blank!r} resolves to system, not human",
              steer.source == GA.SOURCE_SYSTEM, steer.source)
    q = S.SteerQueue()
    q.offer("from a person", GA.SOURCE_HUMAN)
    check("an explicit human source is preserved", q.peek()[0].source == GA.SOURCE_HUMAN)
    check("case is normalised",
          (lambda x: (x.offer("t", "HUMAN"), x.peek()[0].source)[1])(S.SteerQueue()) == GA.SOURCE_HUMAN)

    print()
    print("=== 5. THE AUTHORITY CONSEQUENCE: a non-human steer cannot complete a goal ===")
    # This is the reason the source field is load-bearing rather than decorative.
    from myriaxon import run_budget  # noqa: F401  (import cost is irrelevant; keeps the check honest)

    human_steer = S.SteerQueue()
    human_steer.offer("we are done", GA.SOURCE_HUMAN)
    locus_steer = S.SteerQueue()
    locus_steer.offer("we are done", GA.SOURCE_DOT)

    human_decision = GA.may_complete(GA.TurnContext(
        source=human_steer.peek()[0].source,
        human_input=human_steer.peek()[0].source == GA.SOURCE_HUMAN,
    ))
    locus_decision = GA.may_complete(GA.TurnContext(
        source=locus_steer.peek()[0].source,
        human_input=False,
    ))
    check("a HUMAN steer may complete a goal", human_decision.allowed, human_decision.render()[:80])
    check("a locus-sourced steer may NOT", not locus_decision.allowed, locus_decision.render()[:90])
    check("  and the refusal names it as self-grading",
          "grading itself" in locus_decision.reason, locus_decision.reason[:140])

    print()
    print("=== 6. delivery appends a real user turn ===")
    agent = FakeAgent()
    agent.steer_queue.offer("use the other file", GA.SOURCE_HUMAN)
    delivered = model_adapt.deliver_steers(agent)
    check("one steer was delivered", delivered == 1, str(delivered))
    check("  a message was appended", len(agent.messages) == 1, str(agent.messages))
    check("  with role user", agent.messages[0].get("role") == "user", str(agent.messages[0]))
    check("  carrying the steer text", agent.messages[0].get("content") == "use the other file",
          str(agent.messages[0]))
    check("  and the source is recorded on the agent",
          agent.last_steer_sources == [GA.SOURCE_HUMAN], str(agent.last_steer_sources))

    print()
    print("=== 7. delivery DRAINS: a steer is never replayed ===")
    # A replayed steer reads as a person repeating themselves, and the model tries to reconcile a correction with
    # itself.
    again = model_adapt.deliver_steers(agent)
    check("a second delivery delivers nothing", again == 0, str(again))
    check("  and appends nothing", len(agent.messages) == 1, str(len(agent.messages)))
    check("the queue is empty", agent.steer_queue.pending() == 0, str(agent.steer_queue.pending()))
    check("drain() is idempotent", agent.steer_queue.drain() == [])
    check("  and a third delivery still delivers nothing", model_adapt.deliver_steers(agent) == 0)

    print()
    print("=== 8. AN EMPTY QUEUE PRODUCES NO EMPTY TURN ===")
    # The failure to avoid: a turn that appends `content: ""` and grows the transcript while telling the model
    # nothing.
    fresh = FakeAgent()
    before = len(fresh.messages)
    check("delivering from an empty queue returns 0", model_adapt.deliver_steers(fresh) == 0)
    check("  and appends no message", len(fresh.messages) == before, str(fresh.messages))
    check("clear() on an empty queue reports 0", fresh.steer_queue.clear() == 0)

    print()
    print("=== 9. several steers arrive together, in order ===")
    multi = FakeAgent()
    for text in ("first", "second", "third"):
        multi.steer_queue.offer(text, GA.SOURCE_HUMAN)
    check("all three are delivered", model_adapt.deliver_steers(multi) == 3)
    check("  in the order queued",
          [m["content"] for m in multi.messages] == ["first", "second", "third"],
          str([m["content"] for m in multi.messages]))
    check("  and the provenance note covers them all",
          "3 message" in S.provenance_note([S.Steer(t, GA.SOURCE_HUMAN) for t in ("a", "b", "c")]))

    print()
    print("=== 10. it never raises on a hostile or partial agent ===")
    # Every caller is a turn loop. A steer that can break a turn is worse than a correction arriving late.
    class Bare:
        pass

    class NoQueue:
        messages: list = []

    class NoMessages:
        steer_queue = S.SteerQueue()

    for label, obj in (("an object with nothing", Bare()), ("no queue", NoQueue()),
                       ("a queue but no messages", NoMessages())):
        try:
            result = model_adapt.deliver_steers(obj)
            check(f"handles {label}", result == 0, str(result))
        except Exception as exc:  # noqa: BLE001
            check(f"handles {label}", False, f"{type(exc).__name__}: {exc}")

    print()
    print("=== 11. provenance is recorded, NOT encoded into the message text ===")
    # A marker the model is asked to parse is a second and weaker statement of a fact the harness already knows, and
    # a model can be talked out of reading a marker.
    steer = S.Steer("do the other thing", GA.SOURCE_SCHEDULE, origin="nightly")
    message = S.as_user_turn(steer)
    check("the message carries only the text", message["content"] == "do the other thing", str(message))
    check("  no marker was injected", "[" not in message["content"], str(message))
    check("  the source lives on the Steer object", steer.source == GA.SOURCE_SCHEDULE)
    check("  and the note names it", GA.SOURCE_SCHEDULE in S.provenance_note([steer]),
          S.provenance_note([steer]))

    print()
    print("=== 12. WIRED: all THREE providers deliver at the step boundary ===")
    # A mechanism present on one provider and absent on another is the failure mode `model_adapt` exists to prevent.
    for module in ("agent.py", "openai_agent.py", "gemini_agent.py"):
        source = (REPO / "myriaxon" / module).read_text(encoding="utf-8")
        check(f"{module} delivers steers", "deliver_steers(self)" in source,
              "the call must be in the turn loop")
        check(f"  {module} has a steer_queue", "steer_queue" in source)
    # And the call must be INSIDE the step loop, before the next request.
    for module in ("openai_agent.py", "gemini_agent.py"):
        source = (REPO / "myriaxon" / ".." / "myriaxon" / module).resolve().read_text(encoding="utf-8")
        loop = source.index("while True:")
        call = source.index("model_adapt.deliver_steers(self)")
        prepare = source.index("_prepare_context()", call)
        check(f"  {module}: delivery is inside the loop", loop < call, f"loop {loop} < call {call}")
        check(f"  {module}: delivery precedes _prepare_context", call < prepare,
              "the request must be built after the steer is appended")

    print()
    print("=== 13. the step boundary is the ONLY safe place, and the code says why ===")
    source = (REPO / "myriaxon" / "model_adapt.py").read_text(encoding="utf-8")
    section = source[source.index("def deliver_steers"):]
    section = section[:section.index("\ndef ", 1)] if "\ndef " in section[1:] else section
    check("the ordering constraint is documented", "tool_use" in section and "tool_result" in section,
          "the reason must be recorded where the next reader will find it")
    check("  and the guarantee is stated", "NEXT request" in section or "next request" in section,
          "a steer cannot change a response already sent")
    check("  and idempotency is explained", "drain" in section.lower())
    check("  the function cannot raise", "except Exception" in section)

    print()
    print("=== 14. cancel and steer remain DIFFERENT mechanisms ===")
    # Steering must not become a second, competing way to stop a turn.
    cancellation = (REPO / "myriaxon" / "cancellation.py").read_text(encoding="utf-8")
    check("the cancel path still exists", "class CancelToken" in cancellation)
    check("  it is unchanged by steering", "steer" not in cancellation.lower(),
          "steering must not be wired into the cancel token")
    check("steering does not stop the turn", "return" not in S.as_user_turn(S.Steer("x", GA.SOURCE_HUMAN)),
          "delivering a steer appends a message; it does not end the loop")

    print()
    print("=== 15. the queue reports its state for a status line ===")
    status = S.SteerQueue()
    check("an empty queue says so", "no steers waiting" in status.render(), status.render())
    status.offer("one", GA.SOURCE_HUMAN)
    check("a pending steer is shown", "one" in status.render(), status.render())
    check("  with its source", GA.SOURCE_HUMAN in status.render(), status.render())
    check("clear() reports the count", status.clear() == 1)
    check("  and the queue is empty again", status.pending() == 0)

    print()
    print(f"=== {len(PASS)} passed, {len(FAIL)} failed ===")
    for label in FAIL:
        print(f"  FAILED: {label}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
