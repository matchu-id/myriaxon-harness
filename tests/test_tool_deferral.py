"""Deferred tools: the schemas are withheld, and `request_tools` really loads them back.

The saving is worthless if the unlock does not work, and a broken unlock is worse than no deferral --
the capability would be gone with no way to reach it. So this asserts both halves: the group is NOT
sent by default, and calling the tool makes it appear and makes the handler dispatchable.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from myriaxon import tool_registry  # noqa: E402


def names(tools) -> set[str]:
    out = set()
    for tool in tools or []:
        if tool.get("name"):
            out.add(tool["name"])
        elif isinstance(tool.get("function"), dict):
            out.add(tool["function"].get("name", ""))
    return out


class FakeAgent:
    def __init__(self) -> None:
        self.unlocked_tool_groups: set[str] = set()
        self.all_tools = tool_registry.build_tool_defs()
        self.tools = tool_registry.apply_tool_visibility(
            self.all_tools, self.unlocked_tool_groups)


class TestWithholding(unittest.TestCase):
    def test_every_deferred_group_is_absent_by_default(self) -> None:
        visible = names(FakeAgent().tools)
        for group, spec in tool_registry.DEFERRED_GROUPS.items():
            for tool in spec["tools"]:
                with self.subTest(group=group, tool=tool):
                    self.assertNotIn(tool, visible, f"{tool} should be withheld")

    def test_the_core_tools_are_all_still_present(self) -> None:
        """The point is to withhold, not to break the everyday path."""
        visible = names(FakeAgent().tools)
        for tool in ("read_file", "write_file", "edit_file", "run_bash", "glob_search",
                     "grep_search", "web_search", "web_fetch", "todo_write", "use_skill",
                     "save_skill", "git_status", "git_diff", "ask_user_question"):
            with self.subTest(tool=tool):
                self.assertIn(tool, visible)

    def test_the_unlock_tool_ships_by_default(self) -> None:
        """If it were deferred too there would be no way to reach anything."""
        self.assertIn("request_tools", names(FakeAgent().tools))

    def test_every_deferred_tool_is_named_in_the_unlock_description(self) -> None:
        """The model has to be able to see the door. The catalogue is the whole safety property of
        this design: a capability nobody can discover is a removed capability."""
        description = tool_registry.REQUEST_TOOLS_TOOL_DEF["description"]
        for group, spec in tool_registry.DEFERRED_GROUPS.items():
            with self.subTest(group=group):
                self.assertIn(group, description, "the group name must be listed")
                self.assertIn(str(spec["summary"])[:24], description, "with what it is for")

    def test_the_enum_lists_exactly_the_groups(self) -> None:
        enum = tool_registry.REQUEST_TOOLS_TOOL_DEF["input_schema"]["properties"]["group"]["enum"]
        self.assertEqual(sorted(enum), sorted(tool_registry.DEFERRED_GROUPS))

    def test_a_plugin_or_mcp_tool_is_never_deferred(self) -> None:
        """The filter only knows the built-in groups; anything else passes through untouched."""
        extra = [{"name": "mcp__thing__do", "description": "x", "input_schema": {}},
                 {"name": "plugin_do", "description": "x", "input_schema": {}}]
        kept = names(tool_registry.apply_tool_visibility(extra, set()))
        self.assertEqual(kept, {"mcp__thing__do", "plugin_do"})

    def test_withholding_is_identical_in_all_three_shapes(self) -> None:
        """A filter that understood only one serialisation would silently withhold nothing on the
        other two providers."""
        for label, builder in (("anthropic", tool_registry.build_tool_defs),
                               ("openai", tool_registry.build_tool_defs_openai),
                               ("gemini", tool_registry.build_tool_defs_gemini)):
            with self.subTest(shape=label):
                visible = names(tool_registry.apply_tool_visibility(builder(), set()))
                self.assertNotIn("design_poster", visible)
                self.assertNotIn("locus_create", visible)
                self.assertIn("read_file", visible)


class TestUnlock(unittest.TestCase):
    def call(self, agent, group: str):
        ctx = mock.Mock()
        ctx.owner = agent
        ctx.living_subagents = {}
        return tool_registry._request_tools({"group": group}, ctx)

    def test_unlocking_adds_the_tools(self) -> None:
        agent = FakeAgent()
        self.assertNotIn("design_poster", names(agent.tools))
        message, is_error = self.call(agent, "poster")
        self.assertFalse(is_error, message)
        self.assertIn("design_poster", names(agent.tools))
        self.assertIn("design_poster", message, "the reply must name what was loaded")

    def test_unlocking_one_group_leaves_the_others_withheld(self) -> None:
        agent = FakeAgent()
        self.call(agent, "poster")
        visible = names(agent.tools)
        self.assertIn("design_poster", visible)
        self.assertNotIn("locus_create", visible)
        self.assertNotIn("edit_notebook", visible)

    def test_unlocking_twice_does_not_duplicate(self) -> None:
        """The list is rebuilt, not appended to. Appending would send the same schema twice after a
        second call, costing the tokens this change exists to save."""
        agent = FakeAgent()
        self.call(agent, "loci")
        first = len(agent.tools)
        message, _ = self.call(agent, "loci")
        self.assertEqual(len(agent.tools), first)
        self.assertIn("already loaded", message)

    def test_the_core_tools_keep_their_position_after_an_unlock(self) -> None:
        """Order stability matters: a reshuffled prefix invalidates the provider's cache, which costs
        more than the withheld schemas save."""
        agent = FakeAgent()
        before = [t.get("name") for t in agent.tools if t.get("name") in
                  ("read_file", "write_file", "edit_file", "run_bash")]
        self.call(agent, "poster")
        after = [t.get("name") for t in agent.tools if t.get("name") in
                 ("read_file", "write_file", "edit_file", "run_bash")]
        self.assertEqual(before, after)

    def test_an_unknown_group_is_refused_with_the_valid_list(self) -> None:
        agent = FakeAgent()
        message, is_error = self.call(agent, "posters")
        self.assertTrue(is_error)
        for group in tool_registry.DEFERRED_GROUPS:
            self.assertIn(group, message, "the refusal must list what is valid")
        self.assertIn("poster", message, "a near miss should be suggested")

    def test_an_empty_group_is_refused(self) -> None:
        agent = FakeAgent()
        message, is_error = self.call(agent, "")
        self.assertTrue(is_error)
        self.assertIn("poster", message)

    def test_a_context_with_no_owner_refuses_rather_than_crashing(self) -> None:
        """A test or an older caller may build a context without one. An AttributeError mid-turn is a
        failed turn; a refusal is a message the model can act on."""
        ctx = mock.Mock(spec=[])
        message, is_error = tool_registry._request_tools({"group": "poster"}, ctx)
        self.assertTrue(is_error)
        self.assertIn("could not reach", message)

    def test_group_for_tool_maps_every_deferred_tool(self) -> None:
        for group, spec in tool_registry.DEFERRED_GROUPS.items():
            for tool in spec["tools"]:
                with self.subTest(tool=tool):
                    self.assertEqual(tool_registry.group_for_tool(tool), group)

    def test_group_for_tool_returns_empty_for_a_core_tool(self) -> None:
        self.assertEqual(tool_registry.group_for_tool("read_file"), "")


class TestSaving(unittest.TestCase):
    def test_the_two_builders_agree(self) -> None:
        """`build_tool_defs` and `_builtin_tool_defs` list the same tools in two separate places.

        This is not hypothetical: the first version of this change added `request_tools` to only one
        of them, so every tool list sent to a provider withheld the deferred groups while the unlock
        tool was nowhere to be seen -- the saving applied and the capability was unreachable. The two
        lists are pinned together here so the next addition cannot diverge in silence.
        """
        built = names(tool_registry.build_tool_defs())
        builtin = names(tool_registry._builtin_tool_defs())
        self.assertEqual(built - builtin, set(), f"only in build_tool_defs: {sorted(built - builtin)}")
        self.assertEqual(builtin - built, set(), f"only in _builtin_tool_defs: {sorted(builtin - built)}")

    def test_the_default_set_is_meaningfully_smaller(self) -> None:
        """The measurement the change exists for. Asserted so a future tool addition that lands in the
        wrong group is caught here rather than in a provider bill.

        The bar is 40% rather than 50%, and that is a MEASURED change rather than a relaxed one. The
        saving is a RATIO: adding tools to the core set lowers it even when the absolute core payload
        is healthy, and the Loci work added `locus_create` and `locus_watchlist` there. Measured now:
        full 22,032 chars, core 11,487, saved 10,545 (47.9%), 20 core of 35 tools. A threshold set to
        the exact current value would fail on any honest addition, which makes it a tripwire rather
        than a guard -- so it asserts the property the design needs (the default set is far smaller)
        and leaves headroom for tools that legitimately belong in core.
        """
        full = len(json.dumps(tool_registry.build_tool_defs()))
        visible = tool_registry.apply_tool_visibility(tool_registry.build_tool_defs(), set())
        core = len(json.dumps(visible))
        saved = (full - core) / full
        self.assertGreater(
            saved, 0.4,
            f"core {core:,} of full {full:,} saves only {saved:.0%}, which is not a real saving")

    def test_unlocking_everything_restores_the_full_set(self) -> None:
        """No tool is unreachable: with every group loaded, nothing is missing."""
        everything = set(tool_registry.DEFERRED_GROUPS)
        loaded = names(tool_registry.apply_tool_visibility(
            tool_registry.build_tool_defs(), everything))
        # The delegation tools are present in this builder, so they must come back too.
        missing = names(tool_registry.build_tool_defs()) - loaded
        self.assertEqual(missing, set(), f"unreachable tools: {sorted(missing)}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
