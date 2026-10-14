"""Is the streaming Live display the cost? Same agent, two output targets.

    python scripts/diag_live_overhead.py
    python scripts/diag_live_overhead.py > out.txt     # second mode

Reports per-chunk timings so a slow chunk loop is visible rather than inferred.
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
    console = Console()  # NOT quiet -- same as a normal CLI run
    config = resolve_config(console)
    agent = _build_agent(
        config, config.model, console, True, None, [], [], discover_skills(),
        discover_subagent_types(), load_hooks_config(), load_project_instructions(),
        Scheduler(), settings, mode="auto_edit",
    )

    print(f"stdout is a terminal: {sys.stdout.isatty()}", file=sys.stderr)

    # Count chunks and time them from inside the client.
    original_create = agent.client.chat.completions.create

    def counting_create(**kwargs):
        chunks = []
        start = time.perf_counter()
        for chunk in original_create(**kwargs):
            chunks.append(time.perf_counter() - start)
            yield chunk

    agent.client.chat.completions.create = counting_create

    start = time.perf_counter()
    agent.run_turn("say pong")
    turn = time.perf_counter() - start
    print(f"run_turn                : {turn:6.2f}s", file=sys.stderr)
    if chunks := getattr(counting_create, "collected", None):
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
