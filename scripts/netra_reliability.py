"""Which Netra model can actually serve the agent's full request, reliably?

    python scripts/netra_reliability.py

The agent's request (full system prompt + 24 tool schemas + max_tokens=4096) intermittently gets a
502 from Netra, which breaks real runs. This measures both available models against that exact shape
so the default is chosen on evidence rather than on which model happens to be listed first.
"""
import os
import statistics
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

MODELS = ["deepseek/deepseek-v4.1-flash", "deepseek/deepseek-v4-flash-0731"]
SYSTEM = "You are Myriaxon, a terminal-based coding agent with tools." * 40
USER = "Create a file named probe.txt containing the word hi"


def main() -> int:
    from myriaxon import tool_registry
    from myriaxon.direct_http_client import DirectOpenAI

    tools = tool_registry.build_tool_defs_openai()
    client = DirectOpenAI(api_key=os.environ["NETRA_API_KEY"], base_url="https://api.netraruntime.com/v1")

    print(f"tools={len(tools)}  system={len(SYSTEM)} chars  max_tokens=4096\n")
    for model in MODELS:
        ok_times, failures = [], []
        for _ in range(6):
            start = time.perf_counter()
            try:
                chunks = 0
                for _chunk in client.chat.completions.create(
                    model=model,
                    messages=[{"role": "system", "content": SYSTEM}, {"role": "user", "content": USER}],
                    tools=tools,
                    max_tokens=4096,
                    stream=True,
                ):
                    chunks += 1
                ok_times.append(time.perf_counter() - start)
            except Exception as exc:  # noqa: BLE001
                failures.append(getattr(exc, "status_code", type(exc).__name__))
        total = len(ok_times) + len(failures)
        detail = f"median={statistics.median(ok_times):.2f}s" if ok_times else "no successful request"
        print(f"  {model:<34} {len(ok_times)}/{total} ok  {detail}  failures={failures}")

    client.close()
    print("\nA model with failures here is not safe as the default for agent work, regardless of speed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
