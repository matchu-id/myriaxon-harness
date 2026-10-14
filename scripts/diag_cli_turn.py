"""Run one real CLI turn and report which heavy modules got imported, and when.

    python scripts/diag_cli_turn.py

Answers the question the phase table cannot: the HTTP client says its request took ~3s, yet the
turn takes ~24s. Whatever fills that gap is either an import triggered mid-turn or code in the turn
path, and this distinguishes the two by snapshotting sys.modules before and after.
"""
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from myriaxon.dotenv_support import load_dotenv_into_environ  # noqa: E402

load_dotenv_into_environ()

HEAVY = ("openai", "anthropic", "google.genai", "httpx", "rich.markdown", "markdown_it")


def snapshot() -> set:
    return {m for m in sys.modules if m.split(".")[0] == "openai" or m.startswith(HEAVY)}


def main() -> int:
    from rich.console import Console

    from myriaxon.settings import load_settings
    from myriaxon.cli import _build_agent
    from myriaxon.config import resolve_config
    from myriaxon.skills import discover_skills
    from myriaxon.subagents import discover_subagent_types
    from myriaxon.hooks import load_hooks_config
    from myriaxon.project_instructions import load_project_instructions
    from myriaxon.scheduler import Scheduler

    console = Console(quiet=True)
    settings = load_settings()
    config = resolve_config(console)
    print(f"provider={config.provider} model={config.model}")

    t = time.perf_counter()
    agent = _build_agent(
        config, config.model, console, True, None, [], [], discover_skills(),
        discover_subagent_types(), load_hooks_config(), load_project_instructions(), Scheduler(),
        settings, mode="auto_edit",
    )
    print(f"build_agent            : {time.perf_counter() - t:6.2f}s")

    before = snapshot()
    print(f"openai loaded before turn: {'openai' in sys.modules}")

    t = time.perf_counter()
    agent.run_turn("say pong")
    turn = time.perf_counter() - t
    print(f"run_turn               : {turn:6.2f}s")

    after = snapshot()
    new = sorted(after - before)
    print(f"modules newly imported DURING the turn: {len(new)}")
    for module in new[:15]:
        print(f"  {module}")
    if "openai" in sys.modules and "openai" not in before:
        print("\n>>> the openai SDK WAS imported mid-turn -- that is the gap")
    print(f"\nrequest_count={agent.usage_totals.request_count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
