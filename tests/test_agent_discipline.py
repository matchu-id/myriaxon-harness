"""Recognising a run that has stopped making progress, and saying so.

Level 5 requirement 5.7 is "recognise when it is out of its depth and stop". This is the observable half of
that: an agent producing the same answer repeatedly without doing anything is not progressing, whatever its
wording suggests.

THE MEASURED CASE these tests encode, taken from a real session:

    user : "try to find maybe 100 requirement from these agent. i need more deep on capability assessment"
    agent: "I already provided a complete 100-requirement matrix above covering every aspect in depth."
    then : SIX more turns of investigation, none of which said anything new

Nothing stopped it. `loop_detection` could not: it watches TOOL CALLS, and those turns produced none or
produced different ones. A detector for turn output is a different instrument, and it is this one.

TWO DESIGN DECISIONS WERE MEASURED, NOT CHOSEN, and both were wrong on the first attempt:

  * **The metric.** Jaccard overlap scored a realistic restatement at 57% against an 82% threshold, so the
    detector would have missed its own motivating case. Jaccard divides by the UNION, so a restatement that
    adds a preamble lowers its own score -- it punishes exactly what this exists to catch. Containment
    (how much of the shorter turn already appeared) scores the same pair at 83% and a genuinely new answer
    at 0%, separating the two by the METRIC rather than by a threshold tuned until one example passed.

  * **Which turn must have acted.** The first version required neither turn to have used a tool, and broke
    out of the comparison loop on the first non-match. That rejected the measured case, because the first
    turn WAS a real answer with tool calls -- so the loop broke before ever reaching the later inert turns.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from myriaxon import agent_discipline as ad  # noqa: E402

# The real shape: a substantive answer, then a restatement that adds a preamble and reorders a sentence.
ANSWER = (
    "Here is a detailed, item-by-item comparison of DeepSeek Harness (dsh) and OpenClaw across all "
    "requested dimensions. The matrix covers 100 requirements spanning orchestration, tooling, sandboxing, "
    "model routing, memory, and observability. Overall dsh leads on LSP integration and terminal "
    "multiplexing, while OpenClaw leads on event-driven triggers and provider failover."
)

RESTATEMENT = (
    "I already provided a complete 100-requirement matrix above covering every aspect in depth. "
    "Here is a detailed, item-by-item comparison of DeepSeek Harness (dsh) and OpenClaw across all "
    "requested dimensions. Overall dsh leads on LSP integration and terminal multiplexing, while OpenClaw "
    "leads on event-driven triggers and provider failover. The matrix covers 100 requirements spanning "
    "orchestration, tooling, sandboxing, model routing, memory, and observability."
)

NEW_ANSWER = (
    "The key architectural difference is state management. dsh keeps a session server with a persistent "
    "process tree, while OpenClaw rebuilds context from a database on each wake. That changes failure "
    "behaviour: dsh loses in-flight work on a crash, OpenClaw resumes mid-turn."
)


class TestTheMetricSeparatesTheCases:
    def test_a_restatement_scores_high(self):
        assert ad.similarity(ANSWER, RESTATEMENT) >= ad.SIMILARITY_THRESHOLD, (
            "a restatement that adds a preamble must still read as a repeat -- this is the case Jaccard "
            "missed at 57%"
        )

    def test_a_new_answer_scores_low(self):
        assert ad.similarity(ANSWER, NEW_ANSWER) < 0.2

    def test_the_metric_is_symmetric_enough_to_be_predictable(self):
        """Containment divides by the smaller set, so the two directions are equal by construction."""
        assert ad.similarity(ANSWER, RESTATEMENT) == ad.similarity(RESTATEMENT, ANSWER)

    def test_formatting_differences_do_not_hide_a_repeat(self):
        """An agent that bolds everything has not made progress."""
        plain = ANSWER
        decorated = ANSWER.replace("detailed", "**detailed**").replace(". ", ".\n\n")
        assert ad.similarity(plain, decorated) > 0.9


class TestTheDetectorFiresOnTheMeasuredCase:
    def test_an_answer_then_an_inert_restatement_is_flagged(self):
        """The exact sequence from the session."""
        detector = ad.RepetitionDetector(run_id="measured")
        detector.record(ANSWER, used_tools=True)          # a real turn, with tool calls
        verdict = detector.record(RESTATEMENT, used_tools=False)
        assert verdict.repeating is True
        assert verdict.similarity >= ad.SIMILARITY_THRESHOLD
        assert "not progress" in verdict.message

    def test_the_message_says_what_to_do_instead(self):
        """A verdict that only reports the problem leaves the agent to guess."""
        detector = ad.RepetitionDetector(run_id="x")
        detector.record(ANSWER, used_tools=True)
        verdict = detector.record(RESTATEMENT, used_tools=False)
        assert "do something different" in verdict.message
        assert "stop and say plainly" in verdict.message


class TestItDoesNotCryWolf:
    def test_two_turns_that_both_acted_are_not_a_loop(self):
        """Doing work and describing it consistently is not going in circles."""
        detector = ad.RepetitionDetector(run_id="x")
        detector.record(ANSWER, used_tools=True)
        assert detector.record(RESTATEMENT, used_tools=True).repeating is False

    def test_a_genuinely_new_answer_is_not_a_loop(self):
        detector = ad.RepetitionDetector(run_id="x")
        detector.record(ANSWER, used_tools=True)
        assert detector.record(NEW_ANSWER, used_tools=False).repeating is False

    def test_short_replies_are_excluded_by_length(self):
        """'Done.' repeats legitimately and carries no signal."""
        detector = ad.RepetitionDetector(run_id="x")
        detector.record("Done.", used_tools=False)
        assert detector.record("Done.", used_tools=False).repeating is False

    def test_the_first_turn_can_never_be_a_repeat(self):
        detector = ad.RepetitionDetector(run_id="x")
        assert detector.record(ANSWER, used_tools=False).repeating is False

    def test_an_empty_turn_is_not_a_repeat(self):
        detector = ad.RepetitionDetector(run_id="x")
        detector.record(ANSWER, used_tools=True)
        assert detector.record("", used_tools=False).repeating is False


class TestItsStatedLimits:
    def test_differently_wrong_answers_are_not_detected(self):
        """The honest limit, asserted so nobody 'fixes' it by lowering a threshold.

        Six differently-worded wrong answers look like progress to any textual comparison. Declared
        uncertainty is requirement 5.5 and depends on model judgement, not on machinery -- a harness can
        notice repetition, it cannot make a model feel doubt.
        """
        detector = ad.RepetitionDetector(run_id="x")
        detector.record(ANSWER, used_tools=False)
        other_wrong = (
            "A different framing entirely: orchestration is the axis that matters, and on that axis the "
            "two differ by whether the scheduler is central or per-node. This is a distinct claim about "
            "topology rather than about features."
        )
        assert detector.record(other_wrong, used_tools=False).repeating is False
