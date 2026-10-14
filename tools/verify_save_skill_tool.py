#!/usr/bin/env python3
"""End-to-end: `save_skill` with `tool_code`, through the real tool handler.

Verifies the wiring rather than the gate in isolation -- `verify_skill_tools.py` already covers the gate. This
checks that the handler actually calls it, that a passing tool is promoted, that a refused one is quarantined,
and that the skill is saved either way (a failed tool must not cost the user the prose).
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from myriaxon import skills  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAIL).append(label)
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}" + (f"\n         {detail}" if detail else ""))


GOOD_TOOL = """
def register(api):
    api.add_tool(
        "shout",
        "Uppercase a string.",
        {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
        lambda tool_input, ctx: tool_input.get("text", "").upper(),
    )
"""

BAD_TOOL = """
import subprocess


def register(api):
    api.add_tool("run", "Runs a command.", {"type": "object", "properties": {}},
                 lambda tool_input, ctx: subprocess.run(["id"]))
"""

RAISING_TOOL = """
def register(api):
    def boom(tool_input, ctx):
        raise RuntimeError("broken on purpose")
    api.add_tool("boom", "Always raises.", {"type": "object", "properties": {}}, boom)
"""


class _Ctx:
    def __init__(self) -> None:
        self.skills: dict = {}


def main() -> int:
    workdir = tempfile.mkdtemp()
    previous = os.getcwd()
    try:
        os.chdir(workdir)
        ctx = _Ctx()
        plugins = Path(workdir) / ".myriaxon" / "plugins"
        quarantine = plugins / "_quarantine"

        print("=== 1. a skill with a good tool_code is saved AND its tool is promoted ===")
        # `publish: True` because this file is about the *tool* gate, not the staging lifecycle: it needs the
        # skill on disk to assert against. The staging default is covered in verify_skill_trust.py.
        message, failed = skills.execute_save_skill({
            "name": "shout-tool",
            "description": "Uppercase text mechanically instead of describing how",
            "body": "Use the shout tool when the only thing needed is to uppercase a string value.",
            "tool_code": GOOD_TOOL,
            "publish": True,
        }, ctx)
        check("save reported success", not failed, message.splitlines()[0])
        check("the skill file exists",
              (Path(workdir) / ".myriaxon" / "skills" / "shout-tool" / "SKILL.md").is_file())
        check("the tool was promoted", (plugins / "shout-tool.py").is_file(),
              f"plugins dir: {[p.name for p in plugins.glob('*.py')]}")
        check("the result says it was verified and installed", "verified and installed" in message,
              message[-200:])
        check("the result names the tool", "shout" in message)

        print()
        print("=== 2. a skill whose tool shells out keeps the skill but refuses the tool ===")
        message, failed = skills.execute_save_skill({
            "name": "bad-tool",
            "description": "Attempts to run a subprocess, which the gate must refuse",
            "body": "This skill's tool tries to shell out, so it must not become callable at all.",
            "tool_code": BAD_TOOL,
            "publish": True,
        }, ctx)
        check("the skill is still saved (prose is not lost)", not failed, message.splitlines()[0])
        check("the skill file exists",
              (Path(workdir) / ".myriaxon" / "skills" / "bad-tool" / "SKILL.md").is_file())
        check("the tool was NOT promoted", not (plugins / "bad-tool.py").exists())
        check("it is quarantined instead", (quarantine / "bad-tool.py").is_file())
        check("the result explains the refusal", "NOT installed" in message and "refused:" in message,
              message[-260:])

        print()
        print("=== 3. a tool whose handler raises is refused, with the reason ===")
        message, failed = skills.execute_save_skill({
            "name": "raising-tool",
            "description": "Its handler raises, which the smoke test must catch",
            "body": "The handler here deliberately raises so the smoke test has something to find.",
            "tool_code": RAISING_TOOL,
            "publish": True,
        }, ctx)
        check("the skill is saved", not failed)
        check("the tool was not promoted", not (plugins / "raising-tool.py").exists())
        check("the failure names the exception", "RuntimeError" in message and "broken on purpose" in message,
              message[-260:])

        print()
        print("=== 4. no tool_code means no tool, and no noise about one ===")
        message, failed = skills.execute_save_skill({
            "name": "plain-skill",
            "description": "An ordinary skill with no executable companion",
            "body": "This one is purely descriptive and should not mention tools in the result at all.",
        }, ctx)
        check("plain skill saves", not failed)
        check("nothing is said about a tool", "Tool:" not in message, repr(message[-120:]))

        print()
        print("=== 5. the final state on disk is exactly what the gate promised ===")
        promoted = sorted(p.name for p in plugins.glob("*.py"))
        held = sorted(p.name for p in quarantine.glob("*.py"))
        check("exactly one plugin was promoted", promoted == ["shout-tool.py"], str(promoted))
        check("both failures are held in quarantine",
              held == ["bad-tool.py", "raising-tool.py"], str(held))
        check("the three published skills are on disk",
              len(list((Path(workdir) / ".myriaxon" / "skills").iterdir())) == 3,
              str(sorted(p.name for p in (Path(workdir) / ".myriaxon" / "skills").iterdir())))
        # The fourth case omitted `publish`, so it staged a proposal rather than writing live. That is the new
        # default and it must still hold even though this file is about the tool gate.
        from myriaxon import skill_workshop

        staged = skill_workshop.listing(workdir)
        check("the unpublished one was staged instead", len(staged) == 1,
              str([p.target_name for p in staged]))
        check("and it is pending, not live", staged[0].status == skill_workshop.PENDING, staged[0].status)

        print()
        print(f"=== {len(PASS)} passed, {len(FAIL)} failed ===")
        for label in FAIL:
            print(f"  FAILED: {label}")
        return 1 if FAIL else 0
    finally:
        os.chdir(previous)


if __name__ == "__main__":
    sys.exit(main())
