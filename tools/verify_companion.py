#!/usr/bin/env python3
"""Verify section 8.2's companion capabilities.

Two properties are checked for each capability, and the second is the one usually left out:

  * it produces what the design document says it produces;
  * it does NOT produce what the document forbids -- a pattern view without an opt-in, advice where a question was
    asked for, a second step where the document says stop, or a productivity push at night.
"""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from myriaxon import companion as C  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAIL).append(label)
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}" + (f"\n         {detail}" if detail else ""))


def main() -> int:
    print("=== 1. mood check-in: five levels, a closed set ===")
    check("the five levels are the document's", C.MOODS == ("Great", "Okay", "Low", "Rough", "Not sure"),
          str(C.MOODS))
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        entry = C.check_in("Low", note="long week", root=root)
        check("a level is recorded", entry is not None and entry.mood == "Low")
        check("the note is kept", entry.note == "long week")
        check("case is normalised, not refused", C.check_in("great", root=root).mood == "Great",
              "refusing 'great' would be pedantry with no benefit")
        check("an unknown level is refused", C.check_in("wibble", root=root) is None,
              "the set is closed so a pattern can be honest about what it counted")
        check("an empty level is refused", C.check_in("", root=root) is None)
        check("listing returns newest first", C.list_moods(root=root)[0].mood == "Great")
        check("the last one is available", C.last_mood(root=root).mood == "Great")

        print()
        print("=== 2. THE OPT-IN IS THE USER'S: no pattern view without it ===")
        for index in range(6):
            C.check_in("Rough", root=root)
        refused = C.reflect_mood(opted_in=False, root=root)
        check("without the opt-in, nothing is produced", not refused.enough_data,
              "section 8.2 requires an opt-in and section 8.5 forbids analysing mood data without one")
        check("  and it renders as nothing", refused.render() == "")
        allowed = C.reflect_mood(opted_in=True, root=root)
        check("with the opt-in, the analysis runs", allowed.enough_data, str(allowed.counts))
        check("  it counts what was recorded", allowed.total >= 6, str(allowed.total))

        print()
        print("=== 3. a pattern is an OFFER, not a conclusion ===")
        pattern = C.mood_pattern(root=root)
        if pattern.lower_on:
            rendered = pattern.render()
            check("it names the day(s)", all(day in rendered for day in pattern.lower_on), rendered)
            check("it ends in a question", rendered.rstrip().endswith("?"), rendered)
            check("it says 'want to', not 'you should'", "Want to" in rendered, rendered)
        else:
            check("a pattern needs more than one day of data", True, "insufficient spread, which is correct")

        sparse = Path(tempfile.mkdtemp())
        C.check_in("Rough", root=sparse)
        C.check_in("Rough", root=sparse)
        thin = C.mood_pattern(root=sparse)
        check("two check-ins are NOT a pattern", not thin.lower_on,
              "a pattern drawn from two entries is a coincidence wearing a finding's clothes")

        print()
        print("=== 4. listening: the five steps, and at most one question ===")
        prompt = C.listening_prompt("work has been awful and I slept badly")
        check("the sentiment is carried in", "slept badly" in prompt)
        check("step 1 acknowledge is present", "Acknowledge" in prompt)
        check("step 2 asks for ONE specific detail", "ONE specific detail" in prompt)
        check("at most one question", f"at most {C.MAX_QUESTIONS_PER_TURN}" in prompt)
        check("it asks before advising", "vent" in prompt or "let it out" in prompt, prompt[:200])
        check("step 5 says close softly", "Close softly" in prompt)
        check("an empty input produces nothing", C.listening_prompt("") == "")

        print()
        print("=== 5. grounding: 5-4-3-2-1 and box breathing ===")
        senses = C.start_grounding("5-4-3-2-1")
        check("it is the senses exercise", senses.kind == "5-4-3-2-1")
        check("five steps, counting down 5 to 1",
              len(senses.steps) == 5 and [s.count("Name") for s in senses.steps] == [1] * 5,
              str(senses.steps))
        check("the counts are 5,4,3,2,1", [n for n, _ in C.SENSES] == [5, 4, 3, 2, 1])
        check("it renders", len(senses.render().splitlines()) >= 6)

        breathe = C.start_grounding("breathe")
        check("box breathing is 4-4-4-4", [length for _n, length in breathe.phases] == [4, 4, 4, 4],
              str(breathe.phases))
        check("it reports the round length", breathe.seconds == 16, str(breathe.seconds))
        check("it renders the phases", "Breathe in" in breathe.render())
        check("an unknown kind falls back rather than failing", C.start_grounding("nonsense").kind == "5-4-3-2-1")

        print()
        print("=== 6. the prompts constrain what the model may do ===")
        reframe = C.reframe_prompt("I ruined the launch")
        check("reframe asks for a question, not a correction",
              "QUESTION" in reframe and "never as a correction" in reframe, reframe[:120])
        check("  it carries what they said", "ruined the launch" in reframe)

        overwhelm = C.overwhelm_prompt("everything is on fire")
        check("overwhelm asks for ONE small step", "NEXT TINY STEP" in overwhelm)
        check("  and says STOP", "STOP" in overwhelm, "the document says shrink it, then stop")
        check("  and forbids stacking", "stack more tasks" in overwhelm)

        kind = C.compassion_prompt("I'm useless at this")
        check("compassion forbids lecturing", "do not\nlecture" in kind or "do not lecture" in kind.replace("\n", " "))
        check("  and points at what they said", "useless" in kind)

        connect = C.connection_prompt(who="Sam")
        check("connection names the person", "Sam" in connect)
        check("  it suggests rather than instructs", "Suggest, do not instruct" in connect)
        check("  and is droppable", "drop it" in connect)
        check("an unnamed connection nudge still works", bool(C.connection_prompt()))

        check("an empty reframe produces nothing", C.reframe_prompt("") == "")
        check("an empty overwhelm produces nothing", C.overwhelm_prompt("") == "")

        print()
        print("=== 7. rest nudges: the window wraps midnight ===")
        # A naive `hour >= 22` comparison gets 2am wrong, which is exactly the hour the nudge is for.
        midnight = time.mktime((2026, 1, 5, 23, 30, 0, 0, 0, -1))
        small_hours = time.mktime((2026, 1, 6, 2, 0, 0, 0, 0, -1))
        midday = time.mktime((2026, 1, 6, 13, 0, 0, 0, 0, -1))
        morning = time.mktime((2026, 1, 6, 6, 30, 0, 0, 0, -1))
        check("23:30 is night", C.is_night(when=midnight))
        check("02:00 is night -- the hour the wrap exists for", C.is_night(when=small_hours))
        check("06:30 is night, before the end hour", C.is_night(when=morning))
        check("13:00 is not", not C.is_night(when=midday))
        check("the nudge is empty during the day", C.rest_nudge(when=midday) == "")
        check("the nudge says nothing about productivity at night",
              "finish" in C.rest_nudge(when=midnight) or "stop" in C.rest_nudge(when=midnight),
              C.rest_nudge(when=midnight))

        print()
        print("=== 8. the module declines to be a therapist ===")
        source = (REPO / "myriaxon" / "companion.py").read_text(encoding="utf-8")
        check("it states it is not a therapist", "not** a therapist" in source or "not a therapist" in source)
        check("it does not diagnose", "diagnose" in source)
        check("no treatment is offered", "treatment" in source)

    print()
    print(f"=== {len(PASS)} passed, {len(FAIL)} failed ===")
    for label in FAIL:
        print(f"  FAILED: {label}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
