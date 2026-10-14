"""Persistent agents (loci): identity, objectives, prompting, persistence and resilience.

Offline and hermetic -- every test builds its own registry in a temporary directory, so nothing
here touches ``~/.myriaxon`` or needs an API key.

The behaviours asserted are the ones a person would notice if they broke: that a locus remembers
its objectives across a restart, that a proactive locus wakes for open work and stays quiet
otherwise, that a blocked objective is not retried in a loop, that an active objective is not
starved by a newly added one, and that a corrupt registry degrades instead of taking the harness
down with it.
"""

from __future__ import annotations

import json
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from myriaxon import loci


class LocusTestCase(unittest.TestCase):
    """Base: a private registry per test.

    The registry is injected by patching the accessor, **not** by assigning the module global.
    Assigning `loci._registry` leaks: the panel tests build a real dashboard in the same process,
    the dashboard resolves its registry through `get_registry()`, and it would then be handed a
    scratch registry whose loci the API never created. That is a test-isolation bug that presents
    as a product bug -- the panel looked broken while the code was fine.
    """

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory(prefix="mx-loci-test-")
        self.path = Path(self._tmp.name) / "loci.json"
        self.registry = loci.LocusRegistry(self.path)
        self._saved_get = loci.get_registry
        loci.get_registry = lambda: self.registry

    def tearDown(self) -> None:
        loci.get_registry = self._saved_get
        self._tmp.cleanup()

    def reopened(self) -> loci.LocusRegistry:
        return loci.LocusRegistry(self.path)


class TestCreateAndPersist(LocusTestCase):
    def test_create_records_identity(self) -> None:
        locus = self.registry.create(
            "Sentinel",
            role="dependency watcher",
            instructions="Never upgrade a major version without a note.",
            goals=["Report outdated direct dependencies"],
            proactive=True,
            interval_seconds=60,
        )
        self.assertTrue(locus.id)
        self.assertEqual(locus.name, "Sentinel")
        self.assertEqual(locus.role, "dependency watcher")
        self.assertEqual(len(locus.goals), 1)
        self.assertTrue(any(a.kind == "created" for a in locus.activity))

    def test_everything_survives_a_restart(self) -> None:
        self.registry.create(
            "Sentinel",
            role="watcher",
            instructions="Be terse.",
            goals=["watch the thing"],
            proactive=True,
        )
        again = self.reopened().get("Sentinel")
        assert again is not None
        self.assertEqual(again.role, "watcher")
        self.assertEqual(again.instructions, "Be terse.")
        self.assertEqual([g.title for g in again.goals], ["watch the thing"])
        self.assertTrue(again.proactive)

    def test_lookup_is_by_id_name_and_case(self) -> None:
        locus = self.registry.create("Sentinel")
        self.assertIs(self.registry.get("sentinel"), locus)
        self.assertIs(self.registry.get(locus.id), locus)
        self.assertIsNone(self.registry.get("nothing-like-this"))

    def test_duplicate_names_are_refused(self) -> None:
        self.registry.create("Sentinel")
        with self.assertRaises(ValueError):
            self.registry.create("sentinel")  # refused case-insensitively too

    def test_delete(self) -> None:
        self.registry.create("Sentinel")
        self.assertTrue(self.registry.delete("Sentinel"))
        self.assertFalse(self.registry.delete("Sentinel"))
        self.assertIsNone(self.reopened().get("Sentinel"))


class TestGoals(LocusTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.locus = self.registry.create("Sentinel", goals=["First"])

    def test_lookup_by_id_title_and_fragment(self) -> None:
        goal = self.locus.add_goal("Write the weekly summary")
        self.assertIs(self.locus.find_goal(goal.id), goal)
        self.assertIs(self.locus.find_goal("Write the weekly summary"), goal)
        self.assertIs(self.locus.find_goal("weekly"), goal)
        self.assertIsNone(self.locus.find_goal("zzz"))

    def test_status_transitions_and_validation(self) -> None:
        goal = self.locus.add_goal("Ship it")
        self.assertIsNotNone(self.locus.set_goal_status(goal.id, "active"))
        self.assertEqual(self.locus.find_goal(goal.id).status, "active")
        self.assertIsNone(self.locus.set_goal_status(goal.id, "banana"))
        self.assertEqual(self.locus.find_goal(goal.id).status, "active", "a bad status must not change anything")

    def test_notes_are_kept(self) -> None:
        goal = self.locus.add_goal("Ship it")
        self.locus.set_goal_status(goal.id, "blocked", "needs the API key")
        self.assertEqual(self.locus.find_goal(goal.id).notes[-1], "needs the API key")

    def test_active_goal_is_never_starved_by_a_new_one(self) -> None:
        """Regression: selecting the first goal that was active *or* pending meant adding an
        objective stopped progress on work already in flight."""
        first = self.locus.goals[0]
        self.locus.set_goal_status(first.id, "active")
        self.locus.add_goal("Brand new objective")
        self.assertEqual(self.locus.next_goal().id, first.id)

    def test_pending_goal_is_selected_when_nothing_is_active(self) -> None:
        self.assertEqual(self.locus.next_goal().id, self.locus.goals[0].id)

    def test_blocked_and_done_goals_are_not_selected(self) -> None:
        goal = self.locus.goals[0]
        self.locus.set_goal_status(goal.id, "blocked")
        self.assertIsNone(self.locus.next_goal())
        self.assertEqual(len(self.locus.open_goals()), 1, "blocked is still open -- it needs a person")

        self.locus.set_goal_status(goal.id, "done")
        self.assertIsNone(self.locus.next_goal())
        self.assertEqual(self.locus.open_goals(), [])

    def test_goal_changes_survive_a_restart(self) -> None:
        # Mutating a Locus obtained from the registry is in-memory until it is persisted -- that is
        # the deliberate contract, because a goal is usually updated by the locus's own turn and a
        # write on every field change would be both noisy and racy.
        goal = self.locus.add_goal("Ship it")
        self.locus.set_goal_status(goal.id, "done", "shipped in 1.2.0")
        self.registry.persist(self.locus)

        again = self.reopened().get("Sentinel")
        assert again is not None
        restored = again.find_goal(goal.id)
        assert restored is not None, "the added goal was lost across a restart"
        self.assertEqual(restored.status, "done")
        self.assertIn("shipped in 1.2.0", restored.notes)

    def test_goal_order_and_count_survive_a_restart(self) -> None:
        self.locus.add_goal("Second")
        self.locus.add_goal("Third")
        self.registry.persist(self.locus)
        again = self.reopened().get("Sentinel")
        assert again is not None
        self.assertEqual([g.title for g in again.goals], ["First", "Second", "Third"])

    def test_the_reliability_fields_survive_a_restart(self) -> None:
        """Everything the run layer records is JSON on disk, so a field that is not serialized is
        a field that is silently lost -- including the reason a goal last failed, which is the one
        piece of state a person needs to decide what to do about a stuck objective."""
        goal = self.locus.goals[0]
        goal.last_error = "provider timed out"
        goal.attempts = 2
        self.locus.last_error = "provider timed out"
        self.locus.last_goal_id = goal.id
        self.locus.max_steps_per_run = 3
        self.registry.persist(self.locus)

        raw = json.loads(self.path.read_text(encoding="utf-8"))["loci"][0]
        self.assertEqual(raw["goals"][0]["last_error"], "provider timed out")
        self.assertEqual(raw["last_error"], "provider timed out")
        self.assertEqual(raw["max_steps_per_run"], 3)

        again = self.reopened().get("Sentinel")
        assert again is not None
        self.assertEqual(again.goals[0].last_error, "provider timed out")
        self.assertEqual(again.goals[0].attempts, 2)
        self.assertEqual(again.last_error, "provider timed out")
        self.assertEqual(again.max_steps_per_run, 3)


class TestProactiveGate(LocusTestCase):
    def test_new_dot_is_due_immediately(self) -> None:
        locus = self.registry.create("W", goals=["g"], proactive=True, interval_seconds=30)
        self.assertTrue(locus.is_due())
        self.assertEqual(locus.seconds_until_due(), 0)

    def test_respects_the_interval(self) -> None:
        locus = self.registry.create("W", goals=["g"], proactive=True, interval_seconds=30)
        locus.last_run_at = time.time()
        self.assertFalse(locus.is_due(now=time.time() + 1))
        self.assertTrue(locus.is_due(now=time.time() + 31))
        self.assertEqual(locus.seconds_until_due(now=time.time() + 10), 20)

    def test_disabled_or_non_proactive_or_idle_is_never_due(self) -> None:
        locus = self.registry.create("W", goals=["g"], proactive=True)
        locus.proactive = False
        self.assertFalse(locus.is_due(now=time.time() + 10_000))
        locus.proactive = True
        locus.enabled = False
        self.assertFalse(locus.is_due(now=time.time() + 10_000))
        locus.enabled = True
        locus.set_goal_status(locus.goals[0].id, "done")
        self.assertFalse(locus.is_due(now=time.time() + 10_000))

    def test_next_due_finds_the_soonest(self) -> None:
        soon = self.registry.create("Soon", goals=["g"], proactive=True, interval_seconds=30)
        later = self.registry.create("Later", goals=["g"], proactive=True, interval_seconds=900)
        soon.last_run_at = time.time()
        later.last_run_at = time.time()
        result = self.registry.next_due()
        assert result is not None
        self.assertEqual(result[0].name, "Soon")
        self.assertLessEqual(result[1], 30)


class TestPrompting(LocusTestCase):
    def test_briefing_carries_the_identity(self) -> None:
        locus = self.registry.create(
            "Sentinel",
            role="dependency watcher",
            instructions="Never upgrade a major version without a note.",
            goals=["Report outdated direct dependencies"],
        )
        brief = locus.briefing()
        self.assertIn("Sentinel", brief)
        self.assertIn("dependency watcher", brief)
        self.assertIn("major version", brief)
        self.assertIn("Report outdated direct dependencies", brief)
        self.assertIn(str(locus.workspace_path()), brief)

    def test_briefing_includes_recent_work_so_it_is_not_repeated(self) -> None:
        locus = self.registry.create("S", goals=["g"])
        locus.log("turn", "checked six packages, all current")
        self.assertIn("checked six packages", locus.briefing())

    def test_briefing_without_objectives_stands_the_dot_down(self) -> None:
        """A locus with nothing to do must be told to stop, not left to invent work."""
        locus = self.registry.create("Idle")
        self.assertIn("Do not invent work", locus.briefing())
        self.assertEqual(locus.proactive_prompt(), "")

    def test_proactive_prompt_targets_one_goal_and_marks_it_active(self) -> None:
        locus = self.registry.create("W", goals=["do the thing"], proactive=True)
        prompt = locus.proactive_prompt()
        self.assertIn("do the thing", prompt)
        self.assertIn("locus_goal_update", prompt, "it must be told how to report completion")
        goal = locus.goals[0]
        self.assertEqual(goal.status, "active")
        self.assertEqual(goal.attempts, 1)

    def test_proactive_prompt_invites_reflection_without_demanding_a_lesson(self) -> None:
        """Same wording as the sub-agent path: ask once, and say plainly that finding nothing
        generalizable is a normal outcome -- a prompt that demands a lesson gets an invented one."""
        locus = self.registry.create("W2", goals=["do the thing"], proactive=True)
        prompt = locus.proactive_prompt()
        self.assertIn("save_skill", prompt)
        self.assertIn("normal, expected outcome", prompt)

    def test_a_run_that_never_completed_anything_is_offered_no_reflection(self) -> None:
        locus = self.registry.create("W3", goals=["do the thing"], proactive=True)
        locus.proactive_prompt()
        self.assertEqual(locus.reflection_prompt(), "", "nothing was finished, so nothing was learned")


class TestToolHandlers(LocusTestCase):
    def test_list_and_create(self) -> None:
        text, failed = loci.execute_locus_list({})
        self.assertFalse(failed)
        self.assertIn("persistent agent", text, "the empty state should explain the concept")

        text, failed = loci.execute_locus_create({"name": "Fresh", "goals": ["one thing"]})
        self.assertFalse(failed)
        self.assertIn("Fresh", text)
        self.assertIn("Fresh", loci.execute_locus_list({})[0])

    def test_create_reports_a_duplicate_as_an_error_not_a_crash(self) -> None:
        loci.execute_locus_create({"name": "Fresh"})
        text, failed = loci.execute_locus_create({"name": "Fresh"})
        self.assertTrue(failed)
        self.assertIn("already exists", text)

    def test_goal_update_adds_and_completes(self) -> None:
        loci.execute_locus_create({"name": "Fresh"})
        text, failed = loci.execute_locus_goal_update({"locus": "Fresh", "add": "another thing"})
        self.assertFalse(failed)
        self.assertIn("another thing", text)

        text, failed = loci.execute_locus_goal_update(
            {"locus": "Fresh", "goal": "another", "status": "done", "note": "finished"}
        )
        self.assertFalse(failed)
        self.assertIn("done", text)

    def test_goal_update_rejects_unknown_targets(self) -> None:
        self.assertTrue(loci.execute_locus_goal_update({"locus": "Ghost", "add": "x"})[1])
        loci.execute_locus_create({"name": "Fresh"})
        self.assertTrue(
            loci.execute_locus_goal_update({"locus": "Fresh", "goal": "nope", "status": "done"})[1]
        )
        self.assertTrue(loci.execute_locus_goal_update({"locus": "Fresh"})[1], "needs goal+status or add")

    def test_run_rejects_an_unknown_or_disabled_dot_without_an_agent(self) -> None:
        from myriaxon import tool_registry

        class _Ctx:
            pass

        text, failed = tool_registry.execute_locus_run({"locus": "Ghost", "message": "hi"}, _Ctx())
        self.assertTrue(failed)
        self.assertIn("locus_list", text)

        loci.execute_locus_create({"name": "Off"})
        self.registry.get("Off").enabled = False
        self.registry.save()
        text, failed = tool_registry.execute_locus_run({"locus": "Off", "message": "hi"}, _Ctx())
        self.assertTrue(failed)
        self.assertIn("disabled", text)


class TestResilience(LocusTestCase):
    def test_corrupt_registry_does_not_raise_and_is_preserved(self) -> None:
        self.path.write_text("{ this is not json", encoding="utf-8")
        registry = loci.LocusRegistry(self.path)
        self.assertEqual(registry.all(), [])
        self.assertTrue(
            self.path.with_suffix(".json.corrupt").exists(),
            "the unreadable file must be kept so the user can recover it",
        )

    def test_one_bad_entry_does_not_lose_the_others(self) -> None:
        self.path.write_text(
            json.dumps({"version": 1, "loci": [{"id": "aa", "name": "Good"}, {"garbage": True}]}),
            encoding="utf-8",
        )
        self.assertEqual([d.name for d in loci.LocusRegistry(self.path).all()], ["Good"])

    def test_a_second_registry_sees_the_first_ones_writes(self) -> None:
        """Two long-lived processes share one loci.json -- the terminal and the dashboard are
        routinely open together. A registry that never re-read would revert the other's work."""
        first = loci.LocusRegistry(self.path)
        second = loci.LocusRegistry(self.path)   # loaded while the file was empty

        first.create("FromFirst", goals=["a"])
        second.reload()
        self.assertIsNotNone(second.get("FromFirst"), "reload did not observe the other's write")

        # A mutation made through the stale instance must not discard it.
        second.create("FromSecond")
        again = self.reopened()
        self.assertEqual(sorted(d.name for d in again.all()), ["FromFirst", "FromSecond"])

    def test_persisting_one_dot_does_not_revert_another(self) -> None:
        """Regression: saving the whole set as this process last saw it reverted a locus that
        another process had completed in the meantime."""
        first = loci.LocusRegistry(self.path)
        second = loci.LocusRegistry(self.path)
        first.create("A", goals=["one"])
        first.create("B", goals=["two"])

        # The stale instance holds neither locus, then mutates one it can see.
        second.reload()
        locus_b = second.get("B")
        assert locus_b is not None
        locus_b.set_goal_status(locus_b.goals[0].id, "done", "finished elsewhere")
        second.persist(locus_b)

        final = self.reopened()
        self.assertEqual(sorted(d.name for d in final.all()), ["A", "B"], "locus A was reverted")
        restored_b = final.get("B")
        assert restored_b is not None
        self.assertEqual(restored_b.goals[0].status, "done")

    def test_format_registry_explains_an_empty_registry(self) -> None:
        self.assertIn("persistent agent", loci.format_registry([]))
        self.registry.create("S", goals=["g"])
        listing = loci.format_registry(self.registry.all())
        self.assertIn("S", listing)
        self.assertIn("1 open objective", listing)

    def test_a_goal_that_used_up_its_attempts_becomes_a_question_not_a_loop(self) -> None:
        """Regression guard for the worst failure mode a proactive locus has: no symptom at all.
        The goal stays open (a person still owes an answer) but is never handed to the model again."""
        locus = self.registry.create("Stuck", goals=["impossible"], proactive=True, interval_seconds=30)
        locus.goals[0].attempts = loci.MAX_GOAL_ATTEMPTS
        self.assertEqual(locus.proactive_prompt(), "")
        self.assertEqual(locus.goals[0].status, loci.GOAL_BLOCKED)
        self.assertTrue(any(a.kind == "error" for a in locus.activity))
        self.assertFalse(locus.is_due(now=time.time() + 10_000), "it will not be woken for this goal")

        self.registry.persist(locus)
        again = self.reopened().get("Stuck")
        assert again is not None
        self.assertEqual(again.goals[0].status, loci.GOAL_BLOCKED)
        self.assertIn("attempt", again.goals[0].notes[-1])


class TestToolRegistration(unittest.TestCase):
    """The locus tools must exist for every provider shape, not just the default one.

    Regression: the registry enumerates its built-ins in five separate places, so a new tool
    added to one of them was silently absent from the other four -- present for Anthropic and
    missing for OpenAI-compatible and Gemini.
    """

    def test_every_builder_exposes_the_dot_tools(self) -> None:
        from myriaxon import tool_registry

        expected = {"locus_list", "locus_create", "locus_goal_update", "locus_run"}

        anthropic = {d["name"] for d in tool_registry.build_tool_defs()}
        self.assertTrue(expected <= anthropic, sorted(expected - anthropic))

        openai = {d["function"]["name"] for d in tool_registry.build_tool_defs_openai() if "function" in d}
        self.assertTrue(expected <= openai, sorted(expected - openai))

        gemini = {d["name"] for d in tool_registry.build_tool_defs_gemini() if "name" in d}
        self.assertTrue(expected <= gemini, sorted(expected - gemini))

        builtin = {d["name"] for d in tool_registry._builtin_tool_defs()}
        self.assertTrue(expected <= builtin, sorted(expected - builtin))

    def test_the_dot_tools_are_in_the_schema_index(self) -> None:
        """A tool missing from the index is dispatched without any argument validation."""
        from myriaxon import tool_registry

        index = tool_registry._get_tool_schema_index()
        for name in ("locus_list", "locus_create", "locus_goal_update", "locus_run"):
            with self.subTest(tool=name):
                self.assertIn(name, index)

    def test_dot_is_a_reserved_slash_command(self) -> None:
        from myriaxon.constants import RESERVED_COMMAND_NAMES

        self.assertIn("loci", RESERVED_COMMAND_NAMES)


if __name__ == "__main__":
    unittest.main(verbosity=2)
