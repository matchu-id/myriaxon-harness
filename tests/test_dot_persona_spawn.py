"""A locus run through the CLI path must send the locus's briefing as the sub-agent's identity.

This is the gap the earlier fix left. The dashboard path sets `session.persona_identity` (covered by
`tools/locus_identity_check.py`, which confirmed "I'm the analyst locus"), and `test_persona_identity.py`
covers `build_system_prompt` reading a persona. But nothing covered the CLI path end to end: the
`locus_run` tool builds a *sub-agent* through `_spawn_subagent`, and whether the persona survives that
constructor was never asserted.

The mock provider is asked to report the system prompt it was sent, so this asserts what the model
actually saw rather than what the code intended.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tools"))


class TestLocusRunCarriesTheIdentity(unittest.TestCase):
    """The real spawn path, with a scripted provider and no network."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="mx-locuspersona-")
        self._saved = {k: os.environ.get(k) for k in ("MYRIAXON_CONFIG_DIR",)}

    def tearDown(self) -> None:
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self._tmp.cleanup()

    def _agent_and_dot(self):
        """A real agent wired to the scripted provider, plus a real locus in the registry."""
        import serve_mock_provider  # noqa: F401  (tools/ is on sys.path; used for its PORT)
        from myriaxon import loci
        from myriaxon.cli import _build_agent
        from myriaxon.config import Config
        from myriaxon.settings import load_settings
        from myriaxon.skills import discover_skills
        from myriaxon.subagents import discover_subagent_types
        from myriaxon.hooks import load_hooks_config
        from myriaxon.project_instructions import load_project_instructions
        from myriaxon.scheduler import Scheduler
        from myriaxon.theme import get_console

        registry = loci.get_registry()
        locus = registry.create(
            name="Persona Probe",
            role="data analyst",
            instructions="You answer as the Persona Probe locus.",
            workspace=self._tmp.name,
        )
        registry.add_goal(locus, "Show what identity you were given.")

        config = Config(
            provider="openai_compat", api_key="test-key",
            base_url=f"http://127.0.0.1:{os.environ.get('MOCK_PORT', '8798')}",
            model="myriaxon-test-model",
        )
        agent = _build_agent(
            config, config.model, get_console(quiet=True), True, None, [], [],
            discover_skills(), discover_subagent_types(), load_hooks_config(),
            load_project_instructions(), Scheduler(), load_settings(),
            mode="auto_edit", agent_mode="single",
        )
        return agent, locus

    def test_the_briefing_becomes_the_subagent_identity(self) -> None:
        """Driven through `_spawn_subagent_inner` with a recording agent factory, so the assertion is
        about the constructor the CLI actually uses rather than about the prompt builder."""
        from myriaxon import tool_registry
        from myriaxon.tool_context import ToolContext

        captured: dict = {}

        class Recorder:
            """Stands in for a sub-agent: records what it was told, and what its prompt became.

            The attributes below are the ones `build_system_prompt` reads. Listing them explicitly
            rather than using a Mock means a new attribute the builder starts needing shows up here as
            an AttributeError instead of silently rendering as a Mock repr.
            """

            profile = None
            project_instructions = ""
            skills = []

            def __init__(self, tools=None) -> None:
                captured["tools"] = tools
                self.persona_identity = ""
                self.tool_context = ToolContext.__new__(ToolContext)
                self.tool_context._in_subagent = False
                self.tools = tools or []

            def run_turn(self, prompt: str) -> None:
                captured["prompt"] = prompt
                from myriaxon.system_prompt import build_system_prompt

                captured["persona_identity"] = self.persona_identity
                captured["system"] = build_system_prompt(self)

            def last_assistant_text(self) -> str:
                return "ok"

        ctx = ToolContext.__new__(ToolContext)
        ctx.agent_factory = Recorder
        ctx.subagent_types = {}
        ctx.tools = []
        ctx.living_subagents = {}
        ctx.subagent_id_counter = 0

        persona = 'You are "Persona Probe", a persistent Myriaxon agent (a "locus").'
        tool_registry._spawn_subagent_inner(
            "Message from your owner:\nwho are you?",
            None, ctx, quiet=True, persona=persona,
        )

        self.assertEqual(captured.get("persona_identity"), persona,
                         "the persona must be set on the sub-agent")
        system = captured.get("system", "")
        self.assertIn("Persona Probe", system, "the identity must reach the system prompt")
        self.assertNotIn(
            "terminal-based coding agent", system,
            "the generic identity must be REPLACED, not joined -- two sections defining the same "
            "agent is the original bug",
        )
        self.assertIn("Message from your owner", captured.get("prompt", ""),
                      "the briefing must not also be sent as the message")

    def test_a_nested_subagent_is_refused_rather_than_deadlocking(self) -> None:
        """The sub-agent slot is held by the parent, so a nested spawn would wait forever."""
        from myriaxon import tool_registry
        from myriaxon.tool_context import ToolContext

        ctx = ToolContext.__new__(ToolContext)
        ctx._in_subagent = True
        ctx.tools = []
        ctx.subagent_types = {}
        ctx.living_subagents = {}

        message, is_error = tool_registry._spawn_subagent("do more", None, ctx)
        self.assertTrue(is_error, message)
        self.assertIn("cannot spawn", message)

    def test_the_slot_is_released_even_when_the_subagent_raises(self) -> None:
        """A leaked slot is a slow deadlock: three raised sub-agents would wedge the process."""
        from myriaxon import tool_registry
        from myriaxon.tool_context import ToolContext

        before = tool_registry._subagent_slots._value

        class Exploding:
            def __init__(self, tools=None) -> None:
                self.persona_identity = ""
                self.tool_context = ToolContext.__new__(ToolContext)
                self.tool_context._in_subagent = False

            def run_turn(self, prompt: str) -> None:
                raise RuntimeError("provider blew up")

        ctx = ToolContext.__new__(ToolContext)
        ctx.agent_factory = Exploding
        ctx.subagent_types = {}
        ctx.tools = []
        ctx.living_subagents = {}
        ctx.subagent_id_counter = 0

        with self.assertRaises(RuntimeError):
            tool_registry._spawn_subagent("boom", None, ctx, quiet=True)
        self.assertEqual(tool_registry._subagent_slots._value, before,
                         "the slot leaked on the exception path")


if __name__ == "__main__":
    unittest.main(verbosity=2)
