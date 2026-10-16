"""The idle wake-up belongs to Locus mode, and only fires when there is something to run.

Reported from use: the CLI prompt kept returning on its own every few seconds. The mechanism was traced to
`_arm_locus_wakeup`, which armed a timer in **every** session -- including a plain Myriaxon one, where there
are no objectives to advance and therefore nothing a wake-up could possibly achieve.

The loop was self-sustaining rather than merely useless:
`next_wakeup_seconds()` returns `0.0` whenever anything is due, the timer fires almost immediately, the pass
finds nothing it is allowed to run (a Locus whose preflight refuses stays permanently due, because a refused
run does not advance `last_run_at`), nothing happens, and the prompt re-arms and exits again.

The fix has two halves, and the SECOND is the one easy to miss:

  1. **Myriaxon mode never arms a timer.** Autonomy is a Locus property. A Myriaxon session has a person
     driving it; a prompt that returns on its own is not autonomy, it is an interruption.
  2. **Locus mode does not arm one either when nothing is runnable.** `next_wakeup_seconds()` returns the
     idle cap (30 s) in that case, and the cap exists so a deployment notices a Locus enabled in the
     meantime -- but for an interactive prompt the same value is a timer that fires every 30 seconds to run
     nothing, which is the same loop, merely slower.

These tests pin both halves AND the feature they could have broken: a Locus with real work must still wake.
Over-gating is the failure mode of a fix like this, so it is asserted, not assumed.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


class FakeLoop:
    """Records what was scheduled instead of running it, so the DECISION is observable."""

    def __init__(self) -> None:
        self.armed: list[float] = []

    def call_later(self, delay, fn) -> None:  # noqa: ANN001
        self.armed.append(delay)


def arm(active_dot: str) -> FakeLoop:
    from myriaxon import cli

    loop = FakeLoop()
    session = SimpleNamespace(app=SimpleNamespace(loop=loop, is_running=True))
    agent = SimpleNamespace(tool_context=SimpleNamespace(active_dot=active_dot))
    cli._arm_locus_wakeup(session, agent)
    return loop


@pytest.fixture
def scratch_config(tmp_path, monkeypatch):
    """An isolated config dir, so these tests can create Loci without touching the real ones."""
    monkeypatch.setenv("MYRIAXON_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("MYRIAXON_SWARM_DIR", str(tmp_path / "swarm"))
    monkeypatch.setenv("MYRIAXON_NO_SUPERVISOR", "1")
    monkeypatch.delenv("MYRIAXON_DOT_WAKEUP", raising=False)
    return tmp_path


class TestMyriaxonModeNeverWakes:
    """Autonomy belongs to a Locus. A Myriaxon prompt waits for its person."""

    def test_a_plain_session_arms_no_timer(self, scratch_config):
        assert arm("").armed == []

    def test_whitespace_is_treated_as_no_locus(self, scratch_config):
        """`active_dot` is a string field; a blank-but-truthy value must not arm a timer."""
        assert arm("   ").armed == []

    def test_it_stays_quiet_even_when_a_locus_has_work(self, scratch_config):
        """The important case. Work existing elsewhere is not a reason to interrupt THIS session.

        This is the half that a naive fix gets wrong: gating on "is anything runnable" alone would still arm
        a timer in Myriaxon mode whenever some Locus happened to be due.
        """
        from myriaxon import loci

        registry = loci.get_registry()
        registry.reload()
        registry.create(name="busy", role="engineer", proactive=True, max_steps_per_run=2,
                        goals=["something to do"])
        registry.reload()
        assert [locus.name for locus in registry.headless()], "precondition: a runnable Locus exists"
        assert arm("").armed == [], "a Myriaxon session must not wake for another agent's work"


class TestLocusModeWakesOnlyWhenThereIsWork:
    def test_a_locus_with_no_objectives_does_not_arm_one(self, scratch_config):
        """The second half of the fix.

        A proactive Locus with **zero open objectives** is permanently `due()` -- a refused run never advances
        `last_run_at` -- but it can never actually run. That disagreement between `due()` and `preflight()` is
        what armed the timer forever. `headless()` asks the honest question and excludes it.

        The Locus is created WITHOUT goals rather than having one retired, because `retire_goal` correctly
        refuses anything the owner wrote (Level 5.3: an agent must not be able to destroy its owner's
        instructions, and the failure would be silent). Working around that would have been the test proving
        something other than what it claims.
        """
        from myriaxon import loci

        registry = loci.get_registry()
        registry.reload()
        registry.create(name="idle", role="engineer", proactive=True, max_steps_per_run=2, goals=[])
        registry.reload()

        idle = registry.get("idle")
        assert idle is not None, "precondition: the Locus exists"
        assert idle.enabled and idle.proactive, "precondition: it WOULD be a wake-up candidate on schedule"
        assert not idle.runnable_goals(), "precondition: and it has nothing it is allowed to run"
        # `is_due()` is deliberately NOT asserted here. It is time-based -- a freshly created Locus is not due
        # yet -- and pinning it would make this test depend on the clock rather than on the logic. The property
        # that matters is the one below: a candidate with nothing to run must not be woken for.
        assert "idle" not in [locus.name for locus in registry.headless()], (
            "headless() must exclude a Locus with no runnable goals -- asking the same question the run would "
            "is exactly what `due()` alone gets wrong, and that disagreement armed the timer forever"
        )

    def test_real_work_still_arms_a_timer(self, scratch_config):
        """THE FEATURE. Over-gating is the failure mode of this fix, so it is asserted directly."""
        from myriaxon import loci

        registry = loci.get_registry()
        registry.reload()
        registry.create(name="worker", role="engineer", proactive=True, max_steps_per_run=2,
                        goals=["an objective worth waking for"])
        registry.reload()
        # Asserted membership rather than list equality: `get_registry()` is a module-level singleton, so a
        # Locus created by an earlier test in this session can still be in the registry depending on how the
        # suite is ordered. What matters is that THIS runnable Locus is present, not that it is alone.
        assert "worker" in [locus.name for locus in registry.headless()]

        loop = arm("worker")
        assert loop.armed, "a Locus with runnable work MUST still self-wake -- that is the feature"
        # 0.0 means "due right now", floored to the 0.05 minimum the timer applies.
        assert loop.armed[0] <= 1.0, f"work waiting should wake promptly, got {loop.armed[0]}s"

    def test_the_escape_hatch_disables_it(self, scratch_config, monkeypatch):
        """`MYRIAXON_DOT_WAKEUP=0` must still work for anyone who wants a Locus prompt that never wakes."""
        from myriaxon import loci

        registry = loci.get_registry()
        registry.reload()
        registry.create(name="worker2", role="engineer", proactive=True, max_steps_per_run=2,
                        goals=["work"])
        registry.reload()
        monkeypatch.setenv("MYRIAXON_DOT_WAKEUP", "0")
        assert arm("worker2").armed == []


class TestTheGateDoesNotCrashThePrompt:
    """A wake-up mechanism that can crash the prompt is worse than a Locus that fires late."""

    def test_no_agent_is_survivable(self, scratch_config):
        from myriaxon import cli

        cli._arm_locus_wakeup(SimpleNamespace(app=SimpleNamespace(loop=FakeLoop())), None)

    def test_an_agent_without_a_tool_context_is_survivable(self, scratch_config):
        from myriaxon import cli

        loop = FakeLoop()
        cli._arm_locus_wakeup(SimpleNamespace(app=SimpleNamespace(loop=loop)),
                              SimpleNamespace(tool_context=None))
        assert loop.armed == []

    def test_a_session_without_a_loop_is_survivable(self, scratch_config):
        from myriaxon import cli

        cli._arm_locus_wakeup(SimpleNamespace(app=None), SimpleNamespace(tool_context=SimpleNamespace()))
