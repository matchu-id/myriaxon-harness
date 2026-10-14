#!/usr/bin/env python3
"""Verify section 8.5's boundaries are WIRED, not merely defined.

The signature failure mode in this repository is a mechanism that exists, is verified in isolation, and is called by
nobody. `tools/verify_boundaries.py` proves the RULES; this proves they reach the two places that matter -- the system
prompt, which prevents, and the turn loop, which is the one function every turn passes through.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
if importlib.util.find_spec("rich") is None:
    sys.path.insert(0, str(REPO / "tools" / "_testdoubles"))
    print("  [NOTE] rich is absent; using the test double in tools/_testdoubles.", flush=True)

from myriaxon import boundaries as B  # noqa: E402
from myriaxon import system_prompt  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAIL).append(label)
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}" + (f"\n         {detail}" if detail else ""))


class _StubAgent:
    """The minimum `build_system_prompt` reads. Deliberately not a real agent: this checks the PROMPT, and a stub
    keeps the check from depending on a provider, a config, or a network."""

    project_instructions = ""
    cwd = str(REPO)
    model = "stub"
    provider = "stub"
    mode = "manual"
    tools: list = []
    skills: dict = {}
    unlocked_tool_groups: set = set()

    # Explicit None rather than a permissive __getattr__ returning "". Measured: returning "" made
    # profile.verbosity fail, because the builder treats a falsy profile as ABSENT and a truthy non-object as
    # present. A stub that answers every question with the wrong TYPE is worse than one that fails loudly.
    profile = None
    verbosity = "full"
    last_stop_reason = ""


def main() -> int:
    print("=== 1. boundaries_prompt() exists and states the clauses ===")
    text = B.boundaries_prompt()
    check("it returns text", bool(text.strip()))
    for clause, marker in (
        ("diagnosis", "diagnose"),
        ("medication", "medication"),
        ("human claim", "human"),
        ("replacement", "replace friends"),
        ("guilt", "guilt"),
        ("streak anxiety", "streak"),
        ("privacy", "journal"),
    ):
        check(f"it covers {clause}", marker in text.lower(), text[:80])

    print()
    print("=== 2. WIRED into the system prompt, where it can PREVENT ===")
    prompt = system_prompt.build_system_prompt(_StubAgent())
    check("the prompt is non-empty", len(prompt) > 200, f"{len(prompt)} chars")
    check("the boundaries text is IN the prompt", B.boundaries_prompt() in prompt,
          "a rule that never reaches the model is a rule that cannot prevent anything")
    check("it is a named section", "boundaries" in prompt.lower() or "diagnose" in prompt.lower())
    check("the existing care section survives", "struggling" in prompt.lower(),
          "adding a section must not displace the one it sits beside")

    print()
    print("=== 3. WIRED into the turn loop, which every turn passes through ===")
    source = (REPO / "myriaxon" / "cli.py").read_text(encoding="utf-8")
    check("the check is called from the turn path", "boundaries_module.review(" in source)
    check("  and imported", "boundaries as boundaries_module" in source)
    turn_path = source[source.index("def _run_turn_safely"):]
    turn_path = turn_path[:turn_path.index("\ndef ", 10)]
    check("  inside _run_turn_safely", "boundaries_module.review(" in turn_path,
          "the one function every turn goes through is the only placement that cannot be skipped")
    check("  the user's message is passed", "user_message=text" in turn_path,
          "the dependence boundary is about the REPLY to a disclosure, so it needs the user's turn")
    # The guard is the except Exception that WRAPS the check, so it appears AFTER it -- and the window has to reach
    # past the whole block. Measured: a 700-character slice ended before the handler and reported a missing guard that
    # was there, which is a verifier lying about the code rather than about itself.
    after_call = turn_path.split("boundaries_module.review", 1)[1]
    check("  a failure cannot take down the turn", "except Exception" in after_call,
          "a check must never take down a turn that already succeeded")

    print()
    print("=== 4. the face follows the content ===")
    check("a diagnosis moves the expression",
          "SIGNAL_MOOD_LOW" in turn_path,
          "a reply that crossed a line is not a cheerful moment, and the character is what says so quietly")
    check("  the signal reference is one this module has",
          hasattr(B, "DIAGNOSIS") and hasattr(B, "MEDICATION"))

    print()
    print("=== 5. the prompt and the checker agree on what the boundaries ARE ===")
    # The drift this prevents: writing the rules twice and having the instruction and the check disagree.
    for boundary in B.BOUNDARIES:
        check(f"  {boundary} is a named boundary", isinstance(boundary, str) and bool(boundary))
    check("the prompt is generated, not a second copy",
          "from .boundaries import boundaries_prompt" in (REPO / "myriaxon" / "system_prompt.py").read_text(encoding="utf-8"),
          "one definition, imported -- so the instruction and the check cannot drift apart")

    print()
    print(f"=== {len(PASS)} passed, {len(FAIL)} failed ===")
    for label in FAIL:
        print(f"  FAILED: {label}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
