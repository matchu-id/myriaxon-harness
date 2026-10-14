"""Where does a Gemini turn spend its time, cold vs warm, and in the harness vs the model?

    python scripts/profile_gemini_turn.py

Gemini goes through the google-genai SDK rather than the direct HTTP client, so the split has to be
measured rather than inferred. Reports, per turn:
  * wall time for the whole turn
  * wall time for each model request (via the SDK call)
  * time in the harness's own context preparation
"""
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

import os  # noqa: E402


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

    workspace = Path(__file__).resolve().parent.parent / "bench-out" / "gemprofile"
    workspace.mkdir(parents=True, exist_ok=True)
    os.chdir(workspace)

    started = time.perf_counter()
    config = resolve_config(Console(quiet=True))
    print(f"resolve_config            {time.perf_counter() - started:6.2f}s")

    started = time.perf_counter()
    agent = _build_agent(
        config, config.model, Console(quiet=True), True, None, [], [], discover_skills(),
        discover_subagent_types(), load_hooks_config(), load_project_instructions(), Scheduler(),
        load_settings(), mode="auto_edit",
    )
    print(f"build agent               {time.perf_counter() - started:6.2f}s")

    # Instrument the SDK call itself, whichever client object the agent holds.
    stats = {"count": 0, "time": 0.0}
    client = getattr(agent, "client", None)
    models = getattr(client, "models", None)
    if models is not None and hasattr(models, "generate_content_stream"):
        original = models.generate_content_stream

        def timed(*args, **kwargs):
            stats["count"] += 1
            call_start = time.perf_counter()
            try:
                for chunk in original(*args, **kwargs):
                    yield chunk
            finally:
                stats["time"] += time.perf_counter() - call_start

        models.generate_content_stream = timed
    else:
        print("  (could not instrument the SDK call; totals only)")

    for index in range(1, 5):
        prompt = "say pong" if index == 1 else f"reply with the single word: pong{index}"
        before_calls, before_time = stats["count"], stats["time"]
        started = time.perf_counter()
        agent.run_turn(prompt)
        turn = time.perf_counter() - started
        calls = stats["count"] - before_calls
        model_time = stats["time"] - before_time
        label = "COLD" if index == 1 else "warm"
        print(
            f"turn {index} [{label}]  wall={turn:6.2f}s  model={model_time:6.2f}s ({calls} req)"
            f"  harness={turn - model_time:6.2f}s"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
