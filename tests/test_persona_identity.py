"""The identity section must be replaced by a locus's persona, not appended to it."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from myriaxon import system_prompt  # noqa: E402


class FakeAgent:
    """Only the attributes `_sections` reads. Deliberately minimal."""

    profile = None
    tools = []
    project_instructions = ""
    skills = []
    memory = None
    persona_identity = ""


class TestIdentity(unittest.TestCase):
    def identity_line(self, **attrs) -> str:
        agent = FakeAgent()
        for key, value in attrs.items():
            setattr(agent, key, value)
        return system_prompt.build_system_prompt(agent).splitlines()[0]

    def test_a_normal_agent_keeps_the_generic_identity(self) -> None:
        line = self.identity_line()
        self.assertIn("Myriaxon", line)
        self.assertIn("coding agent", line)

    def test_a_dot_persona_replaces_the_identity_entirely(self) -> None:
        """The defect: the briefing was sent as a user message while the system prompt went on
        asserting the generic identity with `essential=True`. The model believed the system prompt --
        correctly -- so `/loci run analyst "are you analyst?"` answered "I'm a coding agent"."""
        persona = 'You are "analyst", a persistent Myriaxon agent (a "locus").'
        line = self.identity_line(persona_identity=persona)
        self.assertEqual(line, persona)
        self.assertNotIn(
            "terminal-based coding agent", line,
            "two sections claiming to define the same agent is the bug, not the fix",
        )

    def test_the_persona_is_the_only_identity_claim_in_the_prompt(self) -> None:
        persona = 'You are "analyst", a data analyst locus.'
        agent = FakeAgent()
        agent.persona_identity = persona
        prompt = system_prompt.build_system_prompt(agent)
        self.assertEqual(prompt.count("terminal-based coding agent"), 0)

    def test_the_working_directory_is_still_stated_for_a_dot(self) -> None:
        """A locus's identity is prose its owner wrote and cannot be relied on to mention the cwd."""
        agent = FakeAgent()
        agent.persona_identity = 'You are "analyst", a locus.'
        prompt = system_prompt.build_system_prompt(agent)
        self.assertIn("Current working directory", prompt)

    def test_a_blank_persona_falls_back_rather_than_producing_an_empty_identity(self) -> None:
        for blank in ("", "   ", None):
            with self.subTest(value=repr(blank)):
                line = self.identity_line(persona_identity=blank)
                self.assertIn("Myriaxon", line)


if __name__ == "__main__":
    unittest.main(verbosity=2)
