"""A failing command must stop the turn, and a question must be answered without an investigation.

Both tests here encode a failure a user actually experienced, and both were wrong before this file
existed. Recording the measurements in the tests rather than only in the fix is deliberate: the numbers
are the argument, and a future change that loosens them should have to argue with the number.

THE MEASURED FAILURE

    user : "how to pip install myriaxon?"
    agent: ran a directory listing, then read pyproject.toml, TWICE
           the first read BLOCKED FOR 120 SECONDS and failed
    loop detector: reported nothing

    call 1: verdict = None
    call 2: verdict = None
    ...
    call 10: Detection(kind='repeat', severity='warning', message='Consider whether the approach is working.')

Three separate things were wrong, and each has a test below:

  1. ten identical *results* was the only bar, so two identical *failures* were invisible. Ten repeats of a
     successful poll is wasteful; two repeats of a failure is two minutes of a user's life and the same
     mistake twice.
  2. a terminal failure marker PREFIXED the outcome text instead of replacing it, so two timeouts worded
     differently hashed differently and the breaker could not recognise the same failure twice -- the
     breaker was in place and silently inert.
  3. no rule told the agent to answer a question directly. "inspect what you need" is an instruction to
     investigate, and it never said when not to.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from myriaxon import loop_detection as ld  # noqa: E402


@pytest.fixture
def detector():
    fresh = ld.LoopDetector(run_id="test")
    ld.set_detector(fresh)
    yield fresh
    ld.set_detector(None)


class TestAFailureStopsTheTurn:
    """Cause 1: the threshold was ten repeats, which no real failure ever reaches."""

    def test_the_threshold_is_two_not_ten(self):
        assert ld.FAILURE_BREAKER_THRESHOLD == 2, (
            "the whole point is that two identical failures is already one too many"
        )
        assert ld.FAILURE_BREAKER_THRESHOLD < ld.WARNING_THRESHOLD, (
            "a failure must be treated as more urgent than a repeat"
        )

    def test_the_exact_failure_the_user_hit_now_stops(self, detector):
        """The reproduction, verbatim: same command, same failure, second attempt."""
        command = {"command": 'Get-Content "C:\\...\\pyproject.toml"'}
        failure = "configuration error: project.license must be valid exactly by one definition"

        assert ld.record("run_bash", command, failure, failed=True) is None, "one failure is not a loop"
        verdict = ld.record("run_bash", command, failure, failed=True)

        assert verdict is not None, "the second identical failure must stop the turn"
        assert verdict.kind == "repeated-failure"
        assert verdict.severity == "critical", (
            "advice is the wrong register for a broken command -- 'consider whether the approach is "
            "working' is what the old code said at call ten"
        )
        assert "will fail again" in verdict.message

    def test_a_success_after_a_failure_does_not_trip_it(self, detector):
        """Retrying with a fix is exactly what should happen, so it must not be punished."""
        command = {"command": "ls"}
        ld.record("run_bash", command, "permission denied", failed=True)
        assert ld.record("run_bash", command, "file.txt", failed=False) is None

    def test_a_different_failure_is_progress_not_a_loop(self, detector):
        """The error changed, so the model learned something. Interrupting that would be its own mistake."""
        command = {"command": "ls"}
        ld.record("run_bash", command, "permission denied", failed=True)
        assert ld.record("run_bash", command, "no such file or directory", failed=True) is None

    def test_different_commands_each_failing_once_is_not_a_loop(self, detector):
        assert ld.record("run_bash", {"command": "a"}, "permission denied", failed=True) is None
        assert ld.record("run_bash", {"command": "b"}, "permission denied", failed=True) is None

    def test_repeated_successes_are_still_only_a_warning(self, detector):
        """A poll returning unchanged data is wasteful, not broken. The old bar stays for that case."""
        command = {"command": "git status"}
        verdicts = [ld.record("run_bash", command, "clean", failed=False) for _ in range(10)]
        assert verdicts[0] is None
        assert verdicts[-1] is not None
        assert verdicts[-1].severity == "warning", "a poll should be advised, not stopped"


class TestTimeoutsAreRecognisedAsFailures:
    """Cause 2: the marker prefixed the text, so two timeouts hashed differently. The breaker was inert."""

    def test_two_differently_worded_timeouts_are_the_same_failure(self, detector):
        command = {"command": "Get-Content pyproject.toml"}
        first = "[timed out after 120s and was killed]"
        second = "[timed out after 120s and was killed]\nThis command did not finish. Before retrying..."

        assert ld.record("run_bash", command, first, failed=True) is None
        verdict = ld.record("run_bash", command, second, failed=True)

        assert verdict is not None, (
            "two timeouts of the same command are the same failure regardless of the guidance appended "
            "to the second one"
        )
        assert verdict.kind == "repeated-failure"

    def test_a_timeout_is_a_terminal_marker(self):
        assert any("timed out" in marker for marker in ld._TERMINAL_FAILURE_MARKERS)

    def test_two_different_terminal_causes_stay_distinct(self):
        """Replacing the text with the marker must not collapse genuinely different failures."""
        a = ld.normalise_outcome("permission denied")
        b = ld.normalise_outcome("no such file or directory")
        assert a != b

    def test_the_same_terminal_cause_digests_identically_whatever_the_wording(self):
        """The bug: the marker PREFIXED the text, so trailing prose changed the identity."""
        a = ld.normalise_outcome("permission denied while opening file X")
        b = ld.normalise_outcome("PERMISSION DENIED")
        assert a == b


class TestTheAnswerFirstRuleExists:
    """Cause 3: no rule said when NOT to investigate."""

    class FakeAgent:
        """Only the attributes the prompt builder reads. The same minimal shape other tests use."""

        profile = None
        tools = []
        project_instructions = ""
        skills = []
        memory = None
        persona_identity = ""

    def test_the_system_prompt_forbids_investigating_an_answerable_question(self):
        from myriaxon.system_prompt import build_system_prompt

        prompt = build_system_prompt(self.FakeAgent())
        assert "Answer the question that was asked" in prompt
        assert "do not open files" in prompt

    def test_the_rule_is_essential_so_it_survives_a_terse_profile(self):
        """A rule a small profile can drop is a rule that disappears exactly when it is needed."""
        source = (REPO / "myriaxon" / "system_prompt.py").read_text(encoding="utf-8")
        start = source.index('"answer-first"')
        section = source[start:start + 900]
        assert "essential=True" in section
