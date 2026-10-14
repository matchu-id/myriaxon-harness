"""Does a warm-up request actually reduce the first real turn? Measure, do not assume.

    python scripts/measure_warmup.py
"""
import statistics
import sys
import time
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
warnings.filterwarnings("ignore")

PROJECT = os.environ.get("GOOGLE_CLOUD_PROJECT", "").strip()

if not PROJECT:

    raise SystemExit(

        "Set GOOGLE_CLOUD_PROJECT (or MYRIAXON_VERTEX_PROJECT) before running this script."

    )
MODEL = "gemini-3.8-flash"


def one_run(warmup: bool, delay: float = 0.0) -> tuple[float, float, str]:
    import google.genai as genai

    from myriaxon.gemini_agent import GeminiAgent
    from rich.console import Console

    client = genai.Client(vertexai=True, project=PROJECT, location="global")
    agent = GeminiAgent(client, MODEL, Console(quiet=True), True, warmup=warmup)
    if delay:
        time.sleep(delay)
    start = time.perf_counter()
    agent.run_turn("Reply with exactly: pong")
    return time.perf_counter() - start, agent.request_count, agent.last_assistant_text()[:20]


def main() -> int:
    print(f"model={MODEL}\n")

    print("=== no warm-up (baseline) ===")
    for index in range(2):
        elapsed, requests, text = one_run(warmup=False)
        print(f"  run {index + 1}: first-turn {elapsed:6.2f}s  requests={requests}  {text!r}")

    print("\n=== with warm-up, real turn fired immediately ===")
    for index in range(2):
        elapsed, requests, text = one_run(warmup=True)
        print(f"  run {index + 1}: first-turn {elapsed:6.2f}s  requests={requests}  {text!r}")

    print("\n=== with warm-up, then a 5s pause (simulating the user thinking) ===")
    for index in range(1):
        elapsed, requests, text = one_run(warmup=True, delay=5.0)
        print(f"  run {index + 1}: first-turn {elapsed:6.2f}s  requests={requests}  {text!r}")

    print("\n=== second turn in the SAME process (proves the backend stays warm) ===")
    import google.genai as genai

    from myriaxon.gemini_agent import GeminiAgent
    from rich.console import Console

    client = genai.Client(vertexai=True, project=PROJECT, location="global")
    agent = GeminiAgent(client, MODEL, Console(quiet=True), True, warmup=False)
    start = time.perf_counter()
    agent.run_turn("Reply with exactly: pong")
    first = time.perf_counter() - start
    start = time.perf_counter()
    agent.run_turn("Reply with exactly: ping")
    second = time.perf_counter() - start
    print(f"  turn 1: {first:6.2f}s   turn 2: {second:6.2f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
