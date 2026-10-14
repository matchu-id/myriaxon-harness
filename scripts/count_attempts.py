"""Count and time every HTTP attempt one CLI turn makes.

    python scripts/count_attempts.py

Distinguishes the two explanations for a slow/failing turn:
  * many attempts (retries) -- a reliability problem
  * few attempts, each slow   -- a latency problem
Patching at the class level catches retries made by the client AND by the agent.
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

from myriaxon import direct_http_client as dhc  # noqa: E402

ATTEMPTS: list[dict] = []
_original = dhc.DirectOpenAI._stream_chat


def counting_stream(self, **kwargs):
    record: dict = {"start": time.perf_counter(), "status": "?", "chunks": 0}
    ATTEMPTS.append(record)
    try:
        for chunk in _original(self, **kwargs):
            record["chunks"] += 1
            yield chunk
        record["status"] = "ok"
    except Exception as exc:  # noqa: BLE001
        record["status"] = f"{type(exc).__name__}:{getattr(exc, 'status_code', '')}"
        raise
    finally:
        record["elapsed"] = time.perf_counter() - record["start"]


dhc.DirectOpenAI._stream_chat = counting_stream


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
    console = Console(quiet=True)
    config = resolve_config(console)

    start = time.perf_counter()
    agent = _build_agent(
        config, config.model, console, True, None, [], [], discover_skills(),
        discover_subagent_types(), load_hooks_config(), load_project_instructions(),
        Scheduler(), settings, mode="auto_edit",
    )
    build = time.perf_counter() - start

    start = time.perf_counter()
    try:
        agent.run_turn("create attempt_probe.txt with the word hi")
        outcome = "completed"
    except Exception as exc:  # noqa: BLE001
        outcome = f"{type(exc).__name__}: {str(exc)[:80]}"
    turn = time.perf_counter() - start

    print(f"build agent : {build:6.2f}s")
    print(f"run_turn    : {turn:6.2f}s  ({outcome})")
    print(f"http attempts: {len(ATTEMPTS)}")
    for i, record in enumerate(ATTEMPTS, 1):
        print(f"  attempt {i}: {record.get('elapsed', 0):6.2f}s  chunks={record['chunks']:<4} {record['status']}")
    total_http = sum(r.get("elapsed", 0) for r in ATTEMPTS)
    print(f"\ntotal time in HTTP: {total_http:6.2f}s of a {turn:6.2f}s turn")
    print(f"non-HTTP turn time: {turn - total_http:6.2f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
