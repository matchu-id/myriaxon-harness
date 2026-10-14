"""How the interactive prompt treats what the user types: unknown commands, and cancelling a picker.

Two behaviours that were wrong until the takeover and are easy to break again:

* `/hepl` (a typo) was sent to the model as chat text, spending a turn on it. A first word that is `/` plus a plain name is a
  command, and an unknown one says so. A path (`/etc/hosts what is this?`) is still a message.
* Pressing Escape in a picker (`/mode`, `/theme`, ...) did not cancel: `select_option` fell through to a second, typed prompt that
  offered the first option as its default, and closing the input there ended the session with "Unexpected error".
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


class TestUnknownSlashCommand(unittest.TestCase):
    def setUp(self) -> None:
        from myriaxon.cli import is_unknown_slash_command

        self.check = is_unknown_slash_command

    def test_a_mistyped_command_is_unknown(self) -> None:
        for text in ("/hepl", "/h", "/mode-x", "/model_list", "/hepl with some arguments", "  /exitt  "):
            self.assertTrue(self.check(text), text)

    def test_paths_and_ordinary_messages_are_not(self) -> None:
        for text in ("/etc/hosts what is this?", "/Users/me/file.txt", "/tmp/x", "hello", "what does /help do", "",
                     "   ", "/", "//comment", "/v1.2", "/dir\\file", "/a:b", "/名前/x"):
            self.assertFalse(self.check(text), text)


class _Console:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def print(self, *args, **kwargs) -> None:
        self.lines.append(" ".join(str(a) for a in args))


class _Dialog:
    """Stands in for prompt_toolkit's radiolist_dialog: `.run()` returns whatever the test says the user did."""

    def __init__(self, result) -> None:
        self.result = result

    def __call__(self, **kwargs) -> "_Dialog":
        return self

    def run(self):
        return self.result


class TestCancellablePicker(unittest.TestCase):
    OPTIONS = [("a", "A"), ("b", "B")]

    def _select(self, dialog_result, *, cancellable: bool, typed=None, typed_raises=None):
        from myriaxon import config

        def ask(*args, **kwargs):
            if typed_raises is not None:
                raise typed_raises
            return typed

        with mock.patch("prompt_toolkit.shortcuts.radiolist_dialog", _Dialog(dialog_result)), \
                mock.patch.object(config.Prompt, "ask", side_effect=ask) as prompt:
            chosen = config.select_option(_Console(), "Pick", self.OPTIONS, default="a", cancellable=cancellable)
        return chosen, prompt.call_count

    def test_a_choice_in_the_menu_is_returned(self) -> None:
        self.assertEqual(self._select("b", cancellable=True), ("b", 0))
        self.assertEqual(self._select("b", cancellable=False), ("b", 0))

    def test_escape_cancels_when_cancellable(self) -> None:
        chosen, typed_prompts = self._select(None, cancellable=True)
        self.assertIsNone(chosen)
        self.assertEqual(typed_prompts, 0, "cancelling must not show a second, typed prompt")

    def test_escape_still_falls_back_to_typing_in_the_setup_wizard(self) -> None:
        # The wizard needs an answer, so it keeps asking.
        self.assertEqual(self._select(None, cancellable=False, typed="b"), ("b", 1))

    def test_closing_the_input_at_the_typed_prompt_cancels_instead_of_crashing(self) -> None:
        # No usable menu (no terminal): the typed prompt is used, and Ctrl-C / end of input means "never mind".
        from myriaxon import config

        for error in (EOFError(), KeyboardInterrupt()):
            with mock.patch("prompt_toolkit.shortcuts.radiolist_dialog", side_effect=RuntimeError("no console")), \
                    mock.patch.object(config.Prompt, "ask", side_effect=error):
                chosen = config.select_option(_Console(), "Pick", self.OPTIONS, cancellable=True)
            self.assertIsNone(chosen, type(error).__name__)

    def test_the_wizard_does_not_swallow_end_of_input(self) -> None:
        from myriaxon import config

        with mock.patch("prompt_toolkit.shortcuts.radiolist_dialog", side_effect=RuntimeError("no console")), \
                mock.patch.object(config.Prompt, "ask", side_effect=EOFError()):
            with self.assertRaises(EOFError):
                config.select_option(_Console(), "Pick", self.OPTIONS)


class TestPickHelper(unittest.TestCase):
    def test_a_cancelled_pick_says_so_and_returns_none(self) -> None:
        from myriaxon import cli

        console = _Console()
        with mock.patch.object(cli, "select_option", return_value=None) as picker:
            self.assertIsNone(cli._pick(console, "Select a mode", [("a", "a")], default="a"))
        self.assertTrue(any("Cancelled" in line for line in console.lines), console.lines)
        self.assertTrue(picker.call_args.kwargs.get("cancellable"), "the REPL pickers must be cancellable")

    def test_a_real_choice_is_returned_without_comment(self) -> None:
        from myriaxon import cli

        console = _Console()
        with mock.patch.object(cli, "select_option", return_value="plan"):
            self.assertEqual(cli._pick(console, "Select a mode", [("plan", "plan")]), "plan")
        self.assertEqual(console.lines, [])


if __name__ == "__main__":
    unittest.main()
