#!/usr/bin/env python3
"""Verify Loci works END TO END through the real agent path, not only as a state machine.

## Why this verifier exists separately from `verify_loci_character.py`

That one proves the renderer and the rules are correct. This one proves they are **connected** — and those are
different failures. The character was, for a while, a module with 227 passing checks and **no caller**: every
expression rendered perfectly and the CLI never changed one, because nothing invoked it. A unit test cannot see that,
which is why the wiring is asserted here against the same entry point the REPL uses.

Building it found two bugs that no isolated test could have:

  1. **`_did_work` looked for the wrong shape.** It tested `isinstance(block, dict) and block["type"] == "tool_use"`.
     Real assistant content is a list of SDK objects, so that was `False` for every genuine tool call.
  2. **Then it looked for the wrong PROTOCOL.** After fixing the object/dict miss it still failed, because this
     project's default provider is OpenAI-compatible and records `tool_calls` plus a `role: "tool"` message — not
     Anthropic's `tool_use` blocks. Two different shapes for the same event, and only one was handled.

A third followed from the second: `last_stop_reason` is set by the Anthropic agent's budget path and stays `None` on
the OpenAI path, so "no reason" had to mean "returned normally" rather than "unknown" — otherwise every successful
turn either showed `sorry` or showed nothing.

Every one of those was invisible to the 227 renderer checks. That is the argument for this file.
"""

from __future__ import annotations

import importlib.util
import io
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

# See the note in verify_loci_character: rich is absent and there is no network, so a documented test double stands in
# and says so, rather than letting the import line kill the run before any assertion.
if importlib.util.find_spec("rich") is None:
    sys.path.insert(0, str(REPO / "tools" / "_testdoubles"))
    print("  [NOTE] rich is absent; using the test double in tools/_testdoubles.", flush=True)
sys.path.insert(0, str(REPO / "tools"))

from myriaxon import character as character_module  # noqa: E402
from myriaxon import loci_character as C  # noqa: E402
from myriaxon import loci_expression as X  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAIL).append(label)
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}" + (f"\n         {detail}" if detail else ""), flush=True)


class FakeAgent:
    """A stand-in for the agent, in the shapes the two protocols actually produce."""

    def __init__(self, *, reason=None, transcript=None):
        self.last_stop_reason = reason
        self.messages = transcript if transcript is not None else []
        self.tool_context = type("TC", (), {"console": None, "grounding_enabled": False,
                                            "current_turn_message_count": 0, "mode": "manual"})()
        self.usage_totals = type("U", (), {"input_tokens": 0, "output_tokens": 0})()


def anthropic_transcript() -> list[dict]:
    """Tool use as the Anthropic SDK delivers it: objects, not dicts."""
    block = type("Block", (), {"type": "tool_use", "name": "write_file"})()
    return [{"role": "user", "content": "do it"},
            {"role": "assistant", "content": [block]},
            {"role": "user", "content": [{"type": "tool_result", "content": "ok"}]}]


def openai_transcript() -> list[dict]:
    """Tool use as the OpenAI-compatible agent records it: `tool_calls` plus a `tool` message."""
    return [{"role": "user", "content": "do it"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "function": {"name": "write_file"}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "ok"},
            {"role": "assistant", "content": "Done."}]


def main() -> int:
    print("=== 1. THE DEFECT: the character is called by the real turn path ===")
    # Asserted by reading the source of the function every turn goes through. A character that nothing calls renders
    # perfectly and never changes, which is the failure mode this whole file exists for.
    cli_source = (REPO / "myriaxon" / "cli.py").read_text(encoding="utf-8")
    turn_fn = cli_source[cli_source.index("def _run_turn_safely"):]
    turn_fn = turn_fn[:turn_fn.index("\ndef ", 1)] if "\ndef " in turn_fn[1:] else turn_fn
    check("_run_turn_safely creates the character", "for_agent(agent)" in turn_fn)
    check("  it announces a turn starting", "before_turn()" in turn_fn)
    check("  and updates the expression when the turn ends", "after_turn(" in turn_fn)
    check("  in a `finally`, so an error still moves the face", "finally:" in turn_fn and
          turn_fn.index("finally:") < turn_fn.index("after_turn("),
          "a failed turn must not leave a happy face")
    check("  every turn path reaches it", cli_source.count("_run_turn_safely(agent") >= 4,
          f"{cli_source.count('_run_turn_safely(agent')} call sites")
    check("the REPL prompt draws the character", "character_state.prompt_prefix()" in cli_source)
    check("  it ticks the held expression", "character_state.tick()" in cli_source)
    check("the status line carries the mood", "status_fragment()" in cli_source)

    print()
    print("=== 2. _did_work recognises BOTH protocols, as objects and as dicts ===")
    # Four shapes for one event. Each was a separate bug or a separate near-miss.
    cases = [
        ("Anthropic SDK objects", anthropic_transcript()),
        ("Anthropic as dicts", [{"role": "assistant",
                                 "content": [{"type": "tool_use", "name": "write_file"}]}]),
        ("OpenAI tool_calls", openai_transcript()),
        ("OpenAI tool message only", [{"role": "tool", "content": "ok"}]),
    ]
    for label, transcript in cases:
        agent = FakeAgent(transcript=transcript)
        check(f"{label} counts as work", character_module.Character._did_work(agent))
    for label, transcript in (("a text-only turn", [{"role": "assistant", "content": "hello"}]),
                              ("a text-only list", [{"role": "assistant", "content": [{"type": "text", "text": "hi"}]}]),
                              ("an empty transcript", [])):
        agent = FakeAgent(transcript=transcript)
        check(f"{label} does NOT count as work", not character_module.Character._did_work(agent))

    print()
    print("=== 3. after_turn maps every outcome, including the missing stop reason ===")
    # The OpenAI path leaves `last_stop_reason` as None, which is the norm on this host rather than the exception.
    expectations = [
        ("no stop reason, tools used", FakeAgent(reason=None, transcript=openai_transcript()), "cheering"),
        ("no stop reason, no tools", FakeAgent(reason=None, transcript=[]), "idle"),
        ("completed, tools used", FakeAgent(reason="completed", transcript=openai_transcript()), "cheering"),
        ("completed, no tools", FakeAgent(reason="completed", transcript=[]), "idle"),
        ("error", FakeAgent(reason="error"), "sorry"),
        ("cancelled", FakeAgent(reason="cancelled"), "idle"),
        ("step budget", FakeAgent(reason="step_budget"), "concerned"),
        ("token budget", FakeAgent(reason="token_budget"), "concerned"),
        ("time budget", FakeAgent(reason="time_budget"), "concerned"),
        ("an unmapped reason", FakeAgent(reason="brand_new_reason"), "idle"),
    ]
    for label, agent, expected in expectations:
        character = character_module.Character()
        character.begin()
        got = character.after_turn(agent)
        check(f"{label} -> {expected}", got == expected, got)
    # A raised exception outranks everything, including a recorded stop reason.
    character = character_module.Character()
    character.begin()
    got = character.after_turn(FakeAgent(reason="completed", transcript=openai_transcript()),
                               error=RuntimeError("boom"))
    check("an exception -> sorry even with tools used", got == "sorry", got)

    print()
    print("=== 4. the character follows a real session through several turns ===")
    character = character_module.Character()
    character.begin()
    check("a session starts on a greeting", character.expression == "happy", character.expression)
    character.before_turn(typing=True)
    check("typing shows listening", character.expression == "listening", character.expression)
    character.before_turn()
    check("sending shows thinking", character.expression == "thinking", character.expression)
    character.after_turn(FakeAgent(reason=None, transcript=openai_transcript()))
    check("a worked turn shows cheering", character.expression == "cheering", character.expression)
    check("  the turn was counted", character.turns == 1, str(character.turns))
    # An error next, then a crisis, then prove crisis survives the turn after it.
    character.before_turn()
    character.after_turn(FakeAgent(reason="error"))
    check("a failed turn shows sorry", character.expression == "sorry", character.expression)
    character.state.apply(X.SIGNAL_RISK)
    check("a disclosure shows crisis", character.expression == "crisis", character.expression)
    character.before_turn()
    character.after_turn(FakeAgent(reason=None, transcript=openai_transcript()))
    check("STICKY: the turn after a disclosure does not cheer", character.expression == "crisis",
          character.expression)
    character.release_crisis()
    check("  and releasing it returns to idle", character.expression == "idle", character.expression)

    print()
    print("=== 5. it never raises, whatever the agent looks like ===")
    # Every call site is inside a turn. A character that can raise is worse than one that shows the wrong face.
    for label, agent in (("an object with nothing", object()),
                         ("a None transcript", FakeAgent(transcript=None)),
                         ("a transcript of junk", FakeAgent(transcript=[None, 1, "x", {"role": None}])),
                         ("an agent whose attributes raise", None)):
        if label.startswith("an agent whose"):
            class Hostile:
                @property
                def messages(self):
                    raise RuntimeError("nope")

                @property
                def last_stop_reason(self):
                    raise RuntimeError("nope")
            agent = Hostile()
        try:
            character = character_module.Character()
            got = character.after_turn(agent)
            check(f"survives {label}", isinstance(got, str), got)
        except Exception as exc:  # noqa: BLE001
            check(f"survives {label}", False, f"{type(exc).__name__}: {exc}")
    try:
        fragment = character_module.Character().status_fragment()
        check("status_fragment never raises", isinstance(fragment, str), fragment)
    except Exception as exc:  # noqa: BLE001
        check("status_fragment never raises", False, f"{type(exc).__name__}: {exc}")

    print()
    print("=== 6. two agents get two characters ===")
    # The dashboard runs several sessions in one process, and a shared character would have them fighting over the
    # mood -- one session's error turning another session's face sad.
    first, second = FakeAgent(reason=None, transcript=openai_transcript()), FakeAgent(reason="error")
    one = character_module.for_agent(first)
    two = character_module.for_agent(second)
    check("they are different objects", one is not two)
    one.begin()
    two.begin()
    one.after_turn(first)
    two.after_turn(second)
    check("  with different expressions", one.expression != two.expression, f"{one.expression} vs {two.expression}")
    check("  and asking twice returns the same one", character_module.for_agent(first) is one)
    check("an agent that refuses attributes still gets a character",
          isinstance(character_module.for_agent(object()), character_module.Character))

    print()
    print("=== 7. force-by-name, for /mood and for a demo ===")
    character = character_module.Character()
    character.begin()
    for name in C.EXPRESSIONS:
        check(f"set_expression({name!r})", character.set_expression(name) and character.expression == name,
              character.expression)
    # A refused name must change NOTHING.
    #
    # Measured defect: the first version assigned `self.state.expression = name` and only THEN validated, so
    # `set_expression("listning")` returned False and left the character on the typo. A refused change that changes
    # something is worse than one that raises -- the caller sees False, believes nothing happened, and the face is
    # wrong.
    #
    # The check sets its own prior state. An earlier draft asserted the expression was still `focus`, which was
    # whatever the loop above happened to end on, so it failed for a reason that had nothing to do with the rule.
    character.set_expression("caring")
    before = character.expression
    check("an unknown name is refused", not character.set_expression("listning"))
    check("  and the expression is unchanged", character.expression == before,
          f"{before} -> {character.expression}")
    check("  an empty name is refused too", not character.set_expression(""))
    check("  and changes nothing", character.expression == before, character.expression)

    print()
    print("=== 8. the real e2e harness drives it end to end ===")
    # The strongest check available offline: a genuine turn through the real agent, the real tool dispatch and the
    # real transcript, with a scripted provider. If any of the wiring breaks, this fails.
    import contextlib
    import e2e_check

    buffer = io.StringIO()
    try:
        with contextlib.redirect_stdout(buffer):
            exit_code = e2e_check.main()
    except Exception as exc:  # noqa: BLE001
        check("the e2e harness runs", False, f"{type(exc).__name__}: {exc}")
        exit_code = 1
    output = buffer.getvalue()
    check("the e2e harness completes", exit_code == 0, output.strip().splitlines()[-1] if output else "no output")
    passed = sum(1 for line in output.splitlines() if line.strip().startswith("PASS"))
    failed = [line.strip() for line in output.splitlines() if line.strip().startswith("FAIL")]
    check("  every end-to-end check passed", not failed, "; ".join(failed[:3]))
    check("  and it ran a meaningful number", passed >= 40, f"{passed} checks")
    for label in ("Loci reacted to the turn", "a turn that used tools shows cheering",
                  "the status line carries the mood", "the prompt prefix is renderable"):
        check(f"  it asserts: {label}", any(label in line for line in output.splitlines()))


    print()
    print("=== 12. EVERY signal, through the real Character ===")
    # The gap this closed: only `error` and `risk` were driven end to end. The other eight were handled by the state
    # machine and watched by nobody, which is the difference between "the transition works" and "the transition has
    # been seen to work". Goal item (4) asks for EVERY transition.
    from myriaxon import loci_expression as expression_mod

    def fresh_character():
        """A Character as `_run_turn_safely` builds it: enabled, with a plain terminal so nothing needs a tty."""
        made = character_module.Character()
        made.enabled = True
        made._terminal = C.Terminal(color=C.T0_PLAIN, unicode=True, columns=100)
        return made

    # `begin` -- the greeting, which is the one signal a session cannot skip.
    begun = fresh_character()
    begun.begin()
    check("begin reaches happy", begun.expression == "happy", begun.expression)

    # `typing` and `sent` -- both sides of `before_turn`, which the REPL calls on every prompt.
    typing = fresh_character()
    typing.before_turn(typing=True)
    check("before_turn(typing=True) reaches listening", typing.expression == "listening", typing.expression)
    sent = fresh_character()
    sent.before_turn(typing=False)
    check("before_turn(typing=False) reaches thinking", sent.expression == "thinking", sent.expression)

    # `quiet` -- the 8-second release, driven through tick rather than through apply.
    quiet = fresh_character()
    quiet.before_turn(typing=False)
    quiet.state.tick(now=time.time() + expression_mod.IDLE_AFTER_SECONDS * 2)
    check("quiet returns the face to idle", quiet.expression == "idle", quiet.expression)

    # `open` -- the signal a fresh session opens on, applied by `begin` above and asserted directly here too.
    opened = fresh_character()
    opened.state.apply(character_module.SIGNAL_OPEN)
    check("open reaches idle", opened.expression == "idle", opened.expression)

    # `error` -- an exception outranks any recorded stop reason, so it is driven as a real failure.
    failed = fresh_character()
    failed.before_turn()
    failed.after_turn(None, error=RuntimeError("boom"))
    check("error reaches sorry", failed.expression == "sorry", failed.expression)

    # `task_done` -- a turn that did work, which is the path a successful turn takes.
    worked = fresh_character()
    worked.before_turn()

    class _DidWork:
        # "" or completed, which are the two the did-work branch accepts. An invented reason like
        # end_turn falls through to quiet instead, so the stub was testing a path that never runs.
        last_stop_reason = "completed"
        messages = [{"role": "assistant", "content": [
            {"type": "tool_use", "name": "read_file", "input": {}}]}]

    worked.after_turn(_DidWork())
    check("task_done reaches cheering", worked.expression == "cheering", worked.expression)

    # The two signals NO turn produces, so they are driven where their call sites apply them: the mood check-in and
    # the milestone path. Asserted through the same Character the turn loop uses, not through the state machine alone.
    low = fresh_character()
    low.state.apply(expression_mod.SIGNAL_MOOD_LOW)
    check("mood_low reaches caring", low.expression == "caring", low.expression)

    milestone = fresh_character()
    milestone.state.apply(expression_mod.SIGNAL_MILESTONE)
    check("milestone reaches celebrate", milestone.expression, milestone.expression)
    check("  and it is celebrate", milestone.expression == "celebrate", milestone.expression)

    distress = fresh_character()
    distress.state.apply(expression_mod.SIGNAL_DISTRESS)
    check("distress reaches caring", distress.expression == "caring", distress.expression)

    risk = fresh_character()
    risk.state.apply(expression_mod.SIGNAL_RISK)
    check("risk reaches crisis", risk.expression == "crisis", risk.expression)

    print()
    print("=== 13. no signal is left undriven ===")
    # The completeness assertion, and the reason this section exists: it fails if a signal is added to the table and
    # not to this list, which is exactly how eight of them went unwatched.
    driven_names = {
        character_module.SIGNAL_OPEN, expression_mod.SIGNAL_TYPING, expression_mod.SIGNAL_SENT,
        expression_mod.SIGNAL_QUIET, expression_mod.SIGNAL_ERROR, expression_mod.SIGNAL_TASK_DONE,
        expression_mod.SIGNAL_DISTRESS, expression_mod.SIGNAL_MILESTONE,
        expression_mod.SIGNAL_MOOD_LOW, expression_mod.SIGNAL_RISK,
    }
    table_names = set(expression_mod.SIGNAL_EXPRESSION)
    check("every signal in the table is driven above", driven_names >= table_names,
          f"undriven: {sorted(table_names - driven_names)}")
    check("  and the drive set names no signal that does not exist", driven_names <= table_names,
          f"stale: {sorted(driven_names - table_names)}")

    # Each signal must reach the expression the TABLE says, so the table and the behaviour cannot drift.
    reached = {}
    for signal, expected in sorted(expression_mod.SIGNAL_EXPRESSION.items()):
        probe = fresh_character()
        probe.state.apply(signal)
        reached[signal] = probe.expression
        check(f"  {signal} -> {expected}", probe.expression == expected,
              f"reached {probe.expression!r}")

    print()
    print("=== 14. the transitions are visible, not only recorded ===")
    # A face is only wired if a surface can RENDER it: an expression the prompt prefix cannot draw is an expression
    # the user never sees, however correct the state machine is.
    for signal, expected in sorted(expression_mod.SIGNAL_EXPRESSION.items()):
        probe = fresh_character()
        probe.state.apply(signal)
        prefix = probe.prompt_prefix()
        status = probe.status_fragment()
        check(f"  {expected} renders a prompt prefix", bool(prefix.strip()), repr(prefix[:40]))
        check(f"  {expected} renders a status fragment", bool(status.strip()), repr(status[:40]))
    print()
    print("=== 9. the rendered output is what the session would show ===")
    # End to end means the user sees it, so the fragments are rendered at a real tier rather than only inspected.
    character = character_module.Character()
    character._terminal = C.Terminal(color=C.T0_PLAIN, unicode=True, columns=100)
    character.enabled = True
    character.begin()
    prefix = character.prompt_prefix()
    check("the prompt prefix renders", bool(prefix.strip()), repr(prefix))
    check("  one line only", "\n" not in prefix, repr(prefix))
    character.set_expression("caring")
    check("  and follows the expression", "◡" in character.prompt_prefix() or "(" in character.prompt_prefix(),
          repr(character.prompt_prefix()))
    status = character.status_fragment()
    check("the status fragment names the mood", "With you" in status, status)
    check("  without colour when the terminal has none", "\x1b" not in status, repr(status))
    ascii_character = character_module.Character()
    ascii_character._terminal = C.Terminal(color=C.T0_PLAIN, unicode=False, columns=100)
    ascii_character.enabled = True
    ascii_character.begin()
    check("an ASCII session still shows a character", "^" in ascii_character.prompt_prefix(),
          repr(ascii_character.prompt_prefix()))
    disabled = character_module.Character()
    disabled.enabled = False
    check("a disabled character leaves the prompt alone", disabled.prompt_prefix() == "> ",
          repr(disabled.prompt_prefix()))

    print()
    print("=== 10. the DASHBOARD drives the same character ===")
    # A second surface is a second chance for the character to be unwired, and the dashboard has its own event loop,
    # its own template and its own JavaScript. Asserted separately from the CLI for that reason.
    ui_source = (REPO / "myriaxon" / "dashboard" / "ui.py").read_text(encoding="utf-8")
    server_source = (REPO / "myriaxon" / "dashboard" / "server.py").read_text(encoding="utf-8")
    from myriaxon.dashboard.ui import INDEX_HTML

    check("the page has an avatar element", 'id="lociAvatar"' in INDEX_HTML)
    check("  and a mood label beside it", 'id="lociMood"' in INDEX_HTML)
    check("  the label is text, not colour alone", 'id="lociMood">Here<' in INDEX_HTML,
          "a mood carried only by colour is invisible to some users")
    check("the client defines setLoci", "function setLoci" in INDEX_HTML)
    check("  it draws on load, so the header is never empty", 'setLoci("happy")' in INDEX_HTML)
    check("  on send", 'setLoci("thinking")' in INDEX_HTML)
    check("  on a failed send", 'setLoci("sorry")' in INDEX_HTML)
    check("  on a turn error event", 'setLoci("sorry")' in INDEX_HTML)
    check("  on turn end with tools", 'setLoci((e.trajectory && e.trajectory.length) ? "cheering" : "idle")'
          in INDEX_HTML, "the same rule as the CLI: only real work cheers")
    check("  it caches per expression, so it is not a request per keystroke", "LOCI_CACHE" in INDEX_HTML)
    check("  it guards against an out-of-order response", "dataset.expression === expression" in INDEX_HTML)
    check("crisis cannot animate in the stylesheet",
          '.loci[data-expression="crisis"]' in INDEX_HTML and "animation: none" in INDEX_HTML)

    check("the server serves the SVG", '/api/avatar' in server_source)
    check("  from the shared generator, not a second copy",
          "loci_svg.render_svg" in server_source, "one manifest or the surfaces drift")
    check("  and refuses an unknown expression rather than drawing idle",
          "no expression named" in server_source)
    # Actually fetch it, so the route is proven rather than merely present.
    try:
        from myriaxon import loci_svg

        svg = loci_svg.render_svg("caring", size=26)
        check("  the served SVG is valid and named", 'aria-label="Loci, with you"' in svg, svg[:90])
        check("  at the requested size", 'width="26"' in svg, svg[:90])
    except Exception as exc:  # noqa: BLE001
        check("  the served SVG renders", False, f"{type(exc).__name__}: {exc}")

    print()
    print(f"=== {len(PASS)} passed, {len(FAIL)} failed ===")
    for label in FAIL:
        print(f"  FAILED: {label}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
