"""Drive `/dots chat` and `/dots off` through the real command handler.

The routing is covered by `tests/test_dot_chat_mode.py` with stand-ins. This runs the actual
`_run_dots_command` against the real registry, so the verb parsing, the messages and the switch all
behave as they will in a terminal.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from myriaxon import cli  # noqa: E402
from myriaxon import dots as dots_module  # noqa: E402
from myriaxon.tool_context import ToolContext  # noqa: E402

failures = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global failures
    if not ok:
        failures += 1
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}{('  -- ' + detail) if detail and not ok else ''}")


def main() -> int:
    registry = dots_module.get_registry()
    registry.reload()
    names = sorted(d.name for d in registry.all())
    print(f"dots present: {names}\n")
    if not names:
        print("no dots to drive")
        return 0

    name = "analyst" if "analyst" in names else names[0]
    dot = registry.get(name)
    if not dot.enabled:
        dot.enabled = True
        registry.persist(dot)

    console = mock.Mock()
    console.print = lambda *a, **k: printed.append(" ".join(str(x) for x in a))
    printed: list[str] = []

    agent = mock.Mock()
    agent.tool_context = ToolContext.__new__(ToolContext)
    agent.tool_context.active_dot = ""
    agent.tool_context.console = console

    def out() -> str:
        return printed[-1] if printed else ""

    print("1. /dots chat <name> enters the conversation")
    cli._run_dots_command(console, ["chat", name], agent)
    print(f"     said: {out()[:96]}")
    check("the dot became active", agent.tool_context.active_dot == name)
    check("it says how to get back", "/dots off" in " ".join(printed))

    print("\n2. the prompt shows who answers")
    active = getattr(agent.tool_context, "active_dot", "")
    prompt_text = f"{active}> " if active else "> "
    print(f"     prompt would read: {prompt_text!r}")
    check("the prompt is named after the dot", prompt_text.startswith(name))

    print("\n3. /dots off returns to Myriaxon")
    printed.clear()
    cli._run_dots_command(console, ["off"], agent)
    print(f"     said: {out()[:96]}")
    check("the dot is cleared", agent.tool_context.active_dot == "")
    check("it says we are back", "Myriaxon" in out())

    print("\n4. a disabled dot is refused with the fix named")
    printed.clear()
    agent.tool_context.active_dot = ""      # explicit: step 3 must not be assumed to have worked
    dot.enabled = False
    registry.persist(dot)
    cli._run_dots_command(console, ["chat", name], agent)
    print(f"     said: {out()[:96]}")
    check("it did not become active", agent.tool_context.active_dot == "")
    check("the message names the remedy", f"/dots enable {name}" in " ".join(printed))
    dot.enabled = True
    registry.persist(dot)
    check("(re-enabled for the remaining steps)", registry.get(name).enabled is True)

    print("\n5. /dots chat with no name leaves the conversation")
    cli._run_dots_command(console, ["chat", name], agent)
    check("active again", agent.tool_context.active_dot == name)
    cli._run_dots_command(console, ["chat"], agent)
    check("bare /dots chat exits", agent.tool_context.active_dot == "")

    print("\n6. an unknown name lists what exists")
    printed.clear()
    cli._run_dots_command(console, ["chat", "nonexistent-dot"], agent)
    print(f"     said: {out()[:110]}")
    check("it refused", agent.tool_context.active_dot == "")
    check("it lists real names", any(n in " ".join(printed) for n in names))

    print("\n7. the unknown-subcommand message names chat and off")
    printed.clear()
    cli._run_dots_command(console, ["bogus"], agent)
    joined = " ".join(printed)
    check("names /dots chat", "/dots chat" in joined)
    check("names /dots off", "/dots off" in joined)

    print("\n8. /dots enable now points at chat")
    printed.clear()
    cli._run_dots_command(console, ["enable", name], agent)
    joined = " ".join(printed)
    print(f"     said: {joined[:140]}")
    check("the hint mentions chat", f"/dots chat {name}" in joined)
    check("it still mentions the one-shot form", f"/dots run {name}" in joined)

    print()
    print("dot chat mode verified" if not failures else f"{failures} check(s) FAILED")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
