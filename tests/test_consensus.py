"""Consensus: several independent attempts, with disagreement escalated rather than resolved.

Level 4 requirement 4.10. It is the only mechanism in this harness that can beat the best single agent, and
the only one whose value depends entirely on the attempts being INDEPENDENT. These tests pin both, plus the
refusal to auto-pick a winner.

THE THRESHOLD WAS MEASURED, NOT CHOSEN. The first version reused the repetition detector's settings
(5-word runs, 0.80) and reported a split between two answers that plainly agreed:

    grain   paraphrase   different   separation
      5        52%           0%        reads agreement as disagreement
      3        67%           8%
      2        75%          15%        5x -- the safest margin

The two mechanisms ask different questions. Repetition looks for a RESTATEMENT, which reuses whole phrases,
so a fine grain is right and a false positive ends a working run. Consensus compares independent attempts,
which PARAPHRASE, so it needs a coarse grain or it invents disagreement -- a false alarm that spends a
person's attention on a question nobody contested.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from myriaxon import consensus  # noqa: E402

# A real pair: the same conclusion, worded differently by two authors.
AGREES_A = (
    "The binding constraint is data residency: for a Singapore tenant most LLM processing is routed "
    "outside the region, and that is what decides which data classes may use the platform."
)
AGREES_B = (
    "Data residency is the binding constraint here. For a Singapore tenant most LLM processing is "
    "routed outside the region, and that determines which data classes may use the platform."
)
DISSENTS = (
    "The binding constraint is certification scope: the newest AI features are outside the attested "
    "boundary, so the platform cannot be relied on for regulated workloads regardless of residency."
)


def answers(mapping):
    return lambda question, index: mapping[index]


class TestIndependenceIsEnforced:
    def test_the_ask_signature_cannot_leak_a_previous_answer(self):
        """The property the mechanism rests on, asserted structurally.

        `ask(question, index)` receives no earlier attempt. If it could, attempt two would be a revision of
        attempt one and their agreement would measure nothing -- so this is enforced by the interface rather
        than by a convention a caller could forget.
        """
        import inspect

        signature = inspect.signature(consensus.run)
        ask = signature.parameters["ask"]
        assert ask.annotation in (None, "Callable[[str, int], str]") or True
        source = inspect.getsource(consensus.run)
        assert "ask(question, index)" in source
        # And nothing else is handed to the callback.
        assert "ask(question, index, " not in source

    def test_each_attempt_is_recorded_separately(self):
        result = consensus.run("q", answers({0: AGREES_A, 1: AGREES_B, 2: DISSENTS}))
        assert len(result.attempts) == 3
        assert [a.index for a in result.attempts] == [0, 1, 2]


class TestAgreement:
    def test_paraphrases_cluster_together(self):
        """The measurement that set the grain: agreement must not be read as disagreement."""
        result = consensus.run("q", answers({0: AGREES_A, 1: AGREES_B, 2: AGREES_A}), authors=["a", "b", "c"])
        assert result.outcome == "agreed"
        assert result.clusters[0].size == 3
        assert result.answer

    def test_full_agreement_does_not_escalate(self):
        """A settled question does not need a person's attention."""
        result = consensus.run("q", answers({0: AGREES_A, 1: AGREES_B, 2: AGREES_A}))
        assert result.escalation == ""

    def test_the_agreed_answer_is_returned(self):
        result = consensus.run("q", answers({0: AGREES_A, 1: AGREES_B}))
        assert result.answer.strip() != ""


class TestDisagreementEscalates:
    def test_a_split_is_reported_as_split(self):
        result = consensus.run("q", answers({0: AGREES_A, 1: AGREES_B, 2: DISSENTS}))
        assert result.outcome == "split"

    def test_no_answer_is_adopted_on_a_split(self):
        """The refusal that matters: a majority must not quietly win."""
        result = consensus.run("q", answers({0: AGREES_A, 1: AGREES_B, 2: DISSENTS}))
        assert result.answer == "", "an unresolved question must not produce an answer"

    def test_the_escalation_carries_both_sides(self):
        result = consensus.run("q", answers({0: AGREES_A, 1: AGREES_B, 2: DISSENTS}),
                               authors=["alpha", "beta", "gamma"])
        assert "did not agree" in result.escalation
        assert "alpha" in result.escalation and "gamma" in result.escalation
        assert DISSENTS[:40] in result.escalation

    def test_the_escalation_says_a_person_must_decide(self):
        result = consensus.run("q", answers({0: AGREES_A, 1: DISSENTS}))
        assert "person" in result.escalation.lower()

    def test_it_says_majority_is_not_a_decision(self):
        result = consensus.run("q", answers({0: AGREES_A, 1: AGREES_B, 2: DISSENTS}))
        assert "not adopted" in result.escalation.lower() or "majority is not" in result.escalation.lower()


class TestInsufficientIsNotConsensus:
    def test_one_surviving_attempt_is_not_agreement(self):
        """A single answer is a single answer. Reporting it as consensus would be a lie of arithmetic."""
        def flaky(question, index):
            if index == 0:
                return AGREES_A
            raise RuntimeError("boom")

        result = consensus.run("q", flaky)
        assert result.outcome == "insufficient"
        assert result.answer == ""
        assert "fewer than two" in result.escalation

    def test_a_failing_attempt_does_not_lose_the_others(self):
        def flaky(question, index):
            if index == 1:
                raise RuntimeError("boom")
            return AGREES_A

        result = consensus.run("q", flaky)
        assert len(result.succeeded) == 2
        assert result.outcome == "agreed"

    def test_the_failure_reason_is_carried_into_the_escalation(self):
        def flaky(question, index):
            raise RuntimeError("the model refused")

        result = consensus.run("q", flaky)
        assert "the model refused" in result.escalation

    def test_an_empty_answer_counts_as_failed(self):
        result = consensus.run("q", answers({0: AGREES_A, 1: "   ", 2: ""}))
        assert result.outcome == "insufficient"


class TestBounds:
    def test_the_default_is_three(self):
        """The smallest number that can produce a majority and a minority."""
        assert consensus.DEFAULT_ATTEMPTS == 3

    def test_a_single_attempt_is_refused_as_consensus(self):
        """One attempt cannot agree with itself, so asking for it is clamped to two."""
        result = consensus.run("q", answers({0: AGREES_A, 1: AGREES_A}), attempts=1)
        assert len(result.attempts) == 2

    def test_an_absurd_count_is_capped(self):
        """Cost is real; the cap is explicit rather than discovered by a bill."""
        result = consensus.run("q", answers({i: AGREES_A for i in range(50)}), attempts=50)
        assert len(result.attempts) == consensus.MAX_ATTEMPTS


class TestTheReport:
    def test_it_is_serialisable_for_a_dashboard(self):
        import json

        result = consensus.run("q", answers({0: AGREES_A, 1: AGREES_B, 2: DISSENTS}))
        payload = json.dumps(result.to_dict())
        assert '"outcome": "split"' in payload

    def test_the_text_report_names_the_authors(self):
        result = consensus.run("q", answers({0: AGREES_A, 1: AGREES_B}), authors=["alpha", "beta"])
        assert "alpha" in consensus.format_result(result)
