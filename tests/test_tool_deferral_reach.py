"""Prove the deferred tools are reachable: withheld by default, loaded on request, and dispatchable.

This is the property the token saving depends on. A deferral that saves 60% of the tool payload while
making a capability unreachable is not an optimisation, it is a removal with extra steps.
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


def offered(tools) -> set[str]:
    names = set()
    for tool in tools or []:
        if tool.get("name"):
            names.add(tool["name"])
        elif isinstance(tool.get("function"), dict):
            names.add(tool["function"].get("name", ""))
    return names


class FakeAgent:
    """The minimum an agent needs for deferral: a tool list, the full pre-visibility list it was
    derived from, and the unlocked-group set."""

    def __init__(self) -> None:
        self.unlocked_tool_groups: set[str] = set()
        # `all_tools` is what the agent actually stores before visibility is applied; a fake that
        # omitted it would not exercise the real path at all.
        self.all_tools = tool_registry.build_tool_defs()
        self.tools = tool_registry.apply_tool_visibility(
            self.all_tools, self.unlocked_tool_groups)


def unlock(agent: FakeAgent, group: str):
    ctx = mock.Mock()
    ctx.owner = agent
    ctx.living_subagents = {}
    return tool_registry._request_tools({"group": group}, ctx)


class TestReachability(unittest.TestCase):
    def test_every_group_can_be_unlocked_and_then_dispatches(self) -> None:
        """The full round trip per group: withheld, requested, offered, and recognised by the
        dispatcher rather than answering "Unknown tool"."""
        for group, spec in tool_registry.DEFERRED_GROUPS.items():
            with self.subTest(group=group):
                agent = FakeAgent()
                for tool in spec["tools"]:
                    self.assertNotIn(tool, offered(agent.tools), "should start withheld")

                message, is_error = unlock(agent, group)
                self.assertFalse(is_error, message)

                loaded = offered(agent.tools)
                for tool in spec["tools"]:
                    self.assertIn(tool, loaded, f"{tool} was not loaded")

    def test_a_loaded_tool_is_no_longer_reported_as_unknown(self) -> None:
        """The dispatcher must recognise it. Asserted against the schema index, which is what decides
        whether a call is validated or rejected outright."""
        index = tool_registry._get_tool_schema_index()
        for group, spec in tool_registry.DEFERRED_GROUPS.items():
            for tool in spec["tools"]:
                with self.subTest(tool=tool):
                    self.assertIn(tool, index,
                                  "a tool the model can be told to call must be dispatchable")

    def test_unlocking_every_group_offers_the_whole_built_in_set(self) -> None:
        agent = FakeAgent()
        for group in tool_registry.DEFERRED_GROUPS:
            unlock(agent, group)
        everything = offered(agent.tools)
        expected = offered(tool_registry.build_tool_defs())
        self.assertEqual(expected - everything, set(),
                         "no built-in tool may be unreachable once every group is loaded")

    def test_an_unlock_does_not_reorder_the_tools_already_visible(self) -> None:
        """Cache safety: the tools that were already visible must keep their RELATIVE order, or the
        provider's cached prefix is invalidated and the saving is spent on a cache miss instead.

        Asserted as a subsequence rather than a prefix. The first version of this test compared
        `after[:len(before)] == before`, which is stricter than the property that matters and failed
        for a benign reason: the newly loaded tool is appended, so it can land before a core tool that
        the builder emits afterwards (`request_tools` is appended in the builder, `design_poster` is
        emitted by the caller). Relative order is what the cache matches on.
        """
        agent = FakeAgent()
        before = [t.get("name") for t in agent.tools]
        unlock(agent, "poster")
        after = [t.get("name") for t in agent.tools]
        self.assertEqual(
            [n for n in after if n in set(before)], before,
            "the already-visible tools must keep their relative order",
        )
        self.assertIn("design_poster", after, "and the unlocked tool must be present")


class TestTheSaving(unittest.TestCase):
    def test_the_offered_payload_shrinks_by_more_than_a_third(self) -> None:
        """Measured: full 22,032 chars, core 11,487, saved 10,545 (47.9%) -- roughly 2,600 tokens off
        every request.

        The bar was "more than half" and is now 40%, because the saving is a RATIO and the Loci work
        added two tools to the CORE set (`locus_create`, `locus_watchlist`). Adding to core lowers the
        ratio even when the absolute core payload is healthy and the deferred set is still worth having.
        A bar pinned to the current value would fail on the next honest addition.
        """
        full = len(json.dumps(tool_registry.build_tool_defs()))
        visible = tool_registry.apply_tool_visibility(tool_registry.build_tool_defs(), set())
        core = len(json.dumps(visible))
        saved = full - core
        print(f"\n    full {full:,} chars, core {core:,} chars, saved {saved:,} "
              f"({saved / full * 100:.0f}%, ~{saved // 4:,} tokens per request)")
        self.assertGreater(saved / full, 0.4)

    def test_the_unlock_description_is_small(self) -> None:
        """The catalogue is the price of the design. If it grew to the size of what it replaces, the
        change would be pointless -- this keeps it honest."""
        size = len(json.dumps(tool_registry.REQUEST_TOOLS_TOOL_DEF))
        deferred = sum(
            len(json.dumps(d)) for d in tool_registry.build_tool_defs()
            if d.get("name") in {n for s in tool_registry.DEFERRED_GROUPS.values() for n in s["tools"]}
        )
        print(f"    catalogue {size:,} chars vs deferred {deferred:,} chars")
        self.assertLess(size, deferred * 0.1, "the catalogue is too expensive for what it saves")


if __name__ == "__main__":
    unittest.main(verbosity=2)
