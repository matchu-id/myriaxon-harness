"""Compare the CLI's turn path against a hand-built agent, in the same process.

    python scripts/diag_slow_turn.py

The phase table says: HTTP request 3s, but the turn 24s. The agent-level profile says 5s for the
same work. Something differs between the two paths, and this isolates it by timing the SAME agent
through both.
"""
import os
import sys
import time
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
warnings.filterwarnings("ignore")

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from myriaxon.dotenv_support import load_dotenv_into_environ  # noqa: E402

load_dotenv_into_environ()


def main() -> int:
    from rich.console import Console

    from myriaxon.cli import _build_agent
    from myriaxon.config import resolve_config
    from myriaxon.hooks import load_hooks_config
    from myriaxon.project_instructions import load_project_instructions
    from myriaxon.scheduler import Scheduler
    from myriaxon.settings import load_settings
    from myriaxon.skills import discover_skills
    from myriaxon.subagents import discover_subagent_types

    settings = load_settings()

    for label, quiet in (("quiet console (like my script)", True), ("non-quiet console (like the CLI)", False)):
        console = Console(quiet=quiet)
        config = resolve_config(console)
        agent = _build_agent(
            config, config.model, console, True, None, [], [], discover_skills(),
            discover_subagent_types(), load_hooks_config(), load_project_instructions(),
            Scheduler(), settings, mode="auto_edit",
        )

        # Time the pieces inside the turn.
        marks = {}

        original_stream = agent._stream_response

        def timed_stream():
            start = time.perf_counter()
            try:
                return original_stream()
            finally:
                marks["_stream_response"] = time.perf_counter() - start

        agent._stream_response = timed_stream

        original_prepare = agent._prepare_context

        def timed_prepare():
            start = time.perf_counter()
            try:
                return original_prepare()
            finally:
                marks["_prepare_context"] = marks.get("_prepare_context", 0.0) + (time.perf_counter() - start)

        agent._prepare_context = timed_prepare

        start = time.perf_counter()
        agent.run_turn("say pong")
        turn = time.perf_counter() - start

        print(f"=== {label} ===")
        print(f"  run_turn total     : {turn:6.2f}s")
        for name, value in sorted(marks.items(), key=lambda kv: kv[1], reverse=True):
            print(f"  {name:<19}: {value:6.2f}s")
        print(f"  requests           : {agent.usage_totals.request_count}")
        print()
        # Reuse the same config; a fresh resolve_config per loop is fine and cheap.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
