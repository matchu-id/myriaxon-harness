"""Ask the real sub-agent constructor what system prompt it would send.

`test_dot_persona_spawn.py` uses a recording stand-in, which proves the persona is *assigned*. This
uses the real `_build_agent` through `agent_factory`, so it proves the persona survives the
constructor the CLI actually uses -- and prints the prompt, so the answer is a fact rather than an
inference from reading code.

This drives the real `_spawn_subagent`, with only `run_turn` replaced so no request goes out.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tools"))

from myriaxon import tool_registry  # noqa: E402
from myriaxon.cli import _build_agent  # noqa: E402
from myriaxon.config import Config  # noqa: E402
from myriaxon.hooks import load_hooks_config  # noqa: E402
from myriaxon.project_instructions import load_project_instructions  # noqa: E402
from myriaxon.scheduler import Scheduler  # noqa: E402
from myriaxon.settings import load_settings  # noqa: E402
from myriaxon.skills import discover_skills  # noqa: E402
from myriaxon.subagents import discover_subagent_types  # noqa: E402
from myriaxon.theme import get_console  # noqa: E402
from myriaxon.tool_context import ToolContext  # noqa: E402

CAPTURED: dict = {}


def main() -> int:
    config = Config(
        provider="openai_compat", api_key="test-key",
        base_url="http://127.0.0.1:8799", model="myriaxon-test-model",
    )

    # A real agent builds its own `agent_factory` (a `functools.partial` over the provider's Agent
    # class), and that is what the real spawn path calls -- so this uses THAT rather than a
    # reimplementation. The first version hand-rolled a factory over `_build_agent`, which does not
    # accept `tools=`, so the check failed on its own scaffolding instead of the thing under test.
    parent = _build_agent(
        config, config.model, get_console(quiet=True), True, None, [], [],
        discover_skills(), discover_subagent_types(), load_hooks_config(),
        load_project_instructions(), Scheduler(), load_settings(),
        mode="auto_edit", agent_mode="single",
    )
    if parent is None:
        print("  could not build a parent agent")
        return 1

    ctx = parent.tool_context
    real_factory = ctx.agent_factory

    def factory(tools=None):
        """The real factory, with only `run_turn` replaced so no request goes out."""
        agent = real_factory(tools=tools)
        CAPTURED["agent"] = agent

        def stop(prompt: str) -> None:
            CAPTURED["prompt"] = prompt

        agent.run_turn = stop  # type: ignore[method-assign]
        return agent

    ctx.agent_factory = factory
    # A fresh id counter so the sub-agent registers cleanly.
    ctx.subagent_id_counter = 0

    persona = 'You are "analyst", a persistent Myriaxon agent (a "dot"). You are a data analyst.'
    text, is_error = tool_registry._spawn_subagent(
        "Message from your owner:\nwho are you?", None, ctx, quiet=True, persona=persona)

    agent = CAPTURED.get("agent")
    if agent is None:
        print(f"  the factory was never called (result={text!r}, is_error={is_error})")
        return 1

    from myriaxon.system_prompt import build_system_prompt

    system = build_system_prompt(agent)
    first = system.splitlines()[0] if system else ""

    print(f"  prompt sent to the sub-agent: {CAPTURED.get('prompt','')[:64]!r}")
    print(f"  persona on the agent        : {agent.persona_identity[:64]!r}")
    print(f"  first line of the system    : {first[:96]!r}")
    print()

    failures = 0

    def check(label: str, ok: bool, detail: str = "") -> None:
        nonlocal failures
        if not ok:
            failures += 1
        print(f"  {'ok  ' if ok else 'FAIL'}  {label}{('  -- ' + detail) if detail and not ok else ''}")

    check("the real constructor kept the persona", bool(agent.persona_identity))
    check("the identity line is the dot", "analyst" in first, first[:80])
    check("the generic identity is gone", "terminal-based coding agent" not in system)
    check("the dot is told its workspace", "working directory" in system.lower())
    check("the dot keeps its tools", len(agent.tools or []) > 0, str(len(agent.tools or [])))
    check("the briefing is not in the message",
          "persistent Myriaxon agent" not in CAPTURED.get("prompt", ""))

    print()
    print("identity through the real constructor verified" if not failures
          else f"{failures} check(s) FAILED")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
