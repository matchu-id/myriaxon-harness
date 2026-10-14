"""Talking to a locus continuously, which `/loci enable` looked like it did and did not.

The report was: `/loci enable analyst`, then typing "hi" and "who are you", and Myriaxon answering.
That was correct behaviour for the code as written -- `enable` only sets a flag, and `/loci run`
spawns a *disposable* sub-agent per message, so there was no conversation to continue and everything
typed in between went to the main agent. The dashboard had a persistent per-locus conversation; the
terminal had no way to enter one at all.

`/loci chat <name>` now does that, and this covers the routing: who receives a message, what the
prompt says, and how it ends.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from myriaxon import cli  # noqa: E402
from myriaxon.tool_context import ToolContext  # noqa: E402


class FakeLocus:
    def __init__(self, name: str, *, enabled: bool = True, role: str = "generalist",
                 goals=None) -> None:
        self.name = name
        self.role = role
        self.enabled = enabled
        self.goals = goals or []


class FakeAgent:
    def __init__(self) -> None:
        self.tool_context = ToolContext.__new__(ToolContext)
        self.tool_context.active_dot = ""
        self.tool_context.console = mock.Mock()


class TestActiveLocusRouting(unittest.TestCase):
    def setUp(self) -> None:
        self.agent = FakeAgent()
        self.console = mock.Mock()

    def test_no_active_dot_means_the_message_is_not_routed(self) -> None:
        self.assertFalse(cli._send_to_active_dot(self.agent, "hi"),
                         "with nothing active the caller must fall through to Myriaxon")

    def test_setting_a_dot_makes_it_active(self) -> None:
        cli._set_active_dot(self.console, self.agent, FakeLocus("analyst"))
        self.assertEqual(self.agent.tool_context.active_dot, "analyst")

    def test_clearing_returns_to_myriaxon(self) -> None:
        cli._set_active_dot(self.console, self.agent, FakeLocus("analyst"))
        cli._set_active_dot(self.console, self.agent, None)
        self.assertEqual(self.agent.tool_context.active_dot, "")

    def test_a_disabled_dot_is_refused_at_the_switch(self) -> None:
        """`enable` is what makes a locus runnable, so entering a conversation with a disabled one
        would refuse on the very next message. Saying so here is where the fix is one command away."""
        cli._set_active_dot(self.console, self.agent, FakeLocus("analyst", enabled=False))
        self.assertEqual(self.agent.tool_context.active_dot, "",
                         "a disabled locus must not become the active speaker")
        printed = " ".join(str(c) for c in self.console.print.call_args_list)
        self.assertIn("disabled", printed)

    def test_the_switch_says_how_to_get_back(self) -> None:
        """Discoverability: without this the only escape is a command nobody was told about."""
        cli._set_active_dot(self.console, self.agent, FakeLocus("analyst"))
        printed = " ".join(str(c) for c in self.console.print.call_args_list)
        self.assertIn("/loci off", printed)

    def test_a_routed_message_goes_through_execute_locus_run(self) -> None:
        """The same entry point `/loci run` and the `locus_run` tool use, so the preflight, lease,
        retry policy and identity are identical rather than re-implemented."""
        cli._set_active_dot(self.console, self.agent, FakeLocus("analyst"))

        with mock.patch("myriaxon.loci.get_registry") as registry:
            registry.return_value.get.return_value = FakeLocus("analyst")
            with mock.patch("myriaxon.tool_registry.execute_locus_run",
                            return_value=("the locus's answer", False)) as run:
                routed = cli._send_to_active_dot(self.agent, "who are you?")

        self.assertTrue(routed)
        run.assert_called_once()
        args, _ = run.call_args
        self.assertEqual(args[0]["locus"], "analyst")
        self.assertEqual(args[0]["message"], "who are you?")

    def test_a_vanished_dot_clears_the_switch_rather_than_failing_every_message(self) -> None:
        """`/loci rm` in another terminal would otherwise make the REPL answer an error forever."""
        cli._set_active_dot(self.console, self.agent, FakeLocus("analyst"))
        with mock.patch("myriaxon.loci.get_registry") as registry:
            registry.return_value.get.return_value = None
            routed = cli._send_to_active_dot(self.agent, "hi")
        self.assertTrue(routed, "the message is still consumed, not sent to Myriaxon")
        self.assertEqual(self.agent.tool_context.active_dot, "")

    def test_a_failing_run_still_consumes_the_message(self) -> None:
        """Falling through to Myriaxon on a locus failure would answer as the wrong agent -- the exact
        confusion this feature exists to remove."""
        cli._set_active_dot(self.console, self.agent, FakeLocus("analyst"))
        with mock.patch("myriaxon.loci.get_registry") as registry:
            registry.return_value.get.return_value = FakeLocus("analyst")
            with mock.patch("myriaxon.tool_registry.execute_locus_run",
                            return_value=("not ready to run", True)):
                routed = cli._send_to_active_dot(self.agent, "hi")
        self.assertTrue(routed)
        self.assertEqual(self.agent.tool_context.active_dot, "analyst",
                         "a failed run must not silently hand the conversation back")


class TestTheSwitchIsDiscoverable(unittest.TestCase):
    def test_the_help_text_names_chat_and_off(self) -> None:
        source = (REPO / "myriaxon" / "cli.py").read_text(encoding="utf-8")
        help_block = source[source.index("  /loci [list]"):source.index("  /ground")]
        self.assertIn("/loci chat", help_block)
        self.assertIn("/loci off", help_block)

    def test_the_enable_hint_points_at_chat(self) -> None:
        """`/loci enable analyst` answering "message it with /loci run" is the misleading half: run
        is one-shot, so a user following it gets no conversation."""
        source = (REPO / "myriaxon" / "cli.py").read_text(encoding="utf-8")
        marker = source.index("# The hint used to say only")
        window = source[marker:marker + 900]
        self.assertIn("/loci chat", window)

    def test_completion_knows_the_command(self) -> None:
        from myriaxon.completion import BUILTIN_COMMANDS

        self.assertIn("loci", BUILTIN_COMMANDS)
        self.assertIn("chat", BUILTIN_COMMANDS["loci"])

    def test_the_unknown_subcommand_message_lists_them(self) -> None:
        source = (REPO / "myriaxon" / "cli.py").read_text(encoding="utf-8")
        marker = source.index("Unknown /loci subcommand")
        window = source[marker:marker + 400]
        self.assertIn("/loci chat", window)
        self.assertIn("/loci off", window)

    def test_every_theme_can_style_the_dot_prompt(self) -> None:
        """The prompt shows who will answer, so every theme needs the style or the distinction
        disappears exactly where it is hardest to notice.

        `THEMES` maps a name to a `rich.theme.Theme`, whose styles are on `.styles` -- not the theme
        itself, which is why the first version of this test raised TypeError instead of asserting.
        """
        from myriaxon import theme

        for name, table in theme.THEMES.items():
            styles = getattr(table, "styles", None)
            if styles is None:
                self.skipTest("this build spells the theme table differently")
            with self.subTest(theme=name):
                self.assertIn("prompt.locus", styles,
                              f"the {name} theme cannot distinguish a locus's prompt")
                self.assertIn("prompt", styles)


class TestVerbDisambiguation(unittest.TestCase):
    def test_off_alone_leaves_and_off_with_a_name_keeps_its_old_meaning(self) -> None:
        """`/loci off <name>` was already a shorthand for `/loci proactive <name> off` before
        `/loci off` meant "leave the conversation". They are told apart by arity, and the proactive
        handler is placed second so the bare form wins -- the first version put the chat handler after
        it and the advertised way out printed a proactive usage warning instead."""
        source = (REPO / "myriaxon" / "cli.py").read_text(encoding="utf-8")
        chat_at = source.index('if verb == "chat" or (verb == "off" and len(args) < 2):')
        proactive_at = source.index('if verb in ("on", "off", "proactive"):')
        self.assertLess(chat_at, proactive_at,
                        "the bare-/loci-off handler must be reached before the proactive one")

    def test_the_bare_form_requires_no_second_argument(self) -> None:
        """`/loci off` with nothing after it must not fall into a `len(args) < 2` usage error -- that
        is what made the documented escape route unusable."""
        source = (REPO / "myriaxon" / "cli.py").read_text(encoding="utf-8")
        line = [l for l in source.splitlines() if 'if verb == "chat" or' in l][0]
        self.assertIn("len(args) < 2", line)

    def test_the_help_distinguishes_the_two_forms(self) -> None:
        source = (REPO / "myriaxon" / "cli.py").read_text(encoding="utf-8")
        block = source[source.index("  /loci [list]"):source.index("  /ground")]
        self.assertIn("/loci off                Back to Myriaxon.", block)


if __name__ == "__main__":
    unittest.main(verbosity=2)
