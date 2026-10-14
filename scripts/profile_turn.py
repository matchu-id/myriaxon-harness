"""Time every stage of one agent turn, to find harness overhead that is not the model request.

    python scripts/profile_turn.py

The HTTP client already reports its own timing (ttfb/stream/total). This measures what happens
AROUND that call -- the parts that are Myriaxon's own code and therefore fixable here.
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

from rich.console import Console  # noqa: E402

from myriaxon.direct_http_client import DirectOpenAI  # noqa: E402
from myriaxon.openai_agent import OpenAIAgent  # noqa: E402


def main() -> int:
    client = DirectOpenAI(api_key=os.environ["NETRA_API_KEY"], base_url="https://api.netraruntime.com/v1")
    agent = OpenAIAgent(client, "deepseek/deepseek-v4.1-flash", Console(quiet=True), True, provider="netra")

    # Instrument the pieces the agent calls, then run one turn and read the breakdown.
    timings: dict[str, float] = {}

    def wrap(obj, name):
        original = getattr(obj, name)

        def timed(*args, **kwargs):
            start = time.perf_counter()
            try:
                return original(*args, **kwargs)
            finally:
                timings[name] = timings.get(name, 0.0) + (time.perf_counter() - start)

        setattr(obj, name, timed)

    for name in ("_prepare_context", "_stream_response", "_stream_response_with_retry",
                 "_maybe_warn_context_usage", "_save_session", "_system_prompt"):
        if hasattr(agent, name):
            wrap(agent, name)

    from myriaxon import session_store, memory_store
    wrap(session_store, "save_session")
    wrap(memory_store, "record_session")

    started = time.perf_counter()
    agent.run_turn("say pong")
    total = time.perf_counter() - started

    print(f"total turn                {total:7.2f}s")
    print()
    for name, value in sorted(timings.items(), key=lambda kv: kv[1], reverse=True):
        print(f"  {name:<28} {value:7.2f}s")
    accounted = sum(timings.values())
    print(f"\n  {'accounted':<28} {accounted:7.2f}s")
    print(f"  {'other':<28} {total - accounted:7.2f}s")
    print(f"\n  requests made: {agent.usage_totals.request_count}")
    print(f"  reply: {agent.last_assistant_text()[:60]!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
