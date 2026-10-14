"""How fast is Gemini on Vertex through this harness?

    python scripts/timing_gemini.py

Measures the numbers that decide whether the CLI feels fast:
  * cold start (first request to a fresh model instance)
  * warm requests (the cost of every subsequent prompt)
  * time-to-first-token vs total, since perceived speed is TTFT
  * the harness's own overhead on top of the model
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

PROJECT = os.environ.get("GOOGLE_CLOUD_PROJECT", "").strip()

if not PROJECT:

    raise SystemExit(

        "Set GOOGLE_CLOUD_PROJECT (or MYRIAXON_VERTEX_PROJECT) before running this script."

    )
LOCATION = "global"


def main() -> int:
    from rich.console import Console

    from myriaxon.config import resolve_config

    console = Console(quiet=True)
    config = resolve_config(console)
    model = config.model
    print(f"model={model} location={config.vertex_location} project={config.vertex_project}\n")

    process_start = time.perf_counter()

    import google.genai as genai

    import_done = time.perf_counter()
    print(f"  import google.genai          {import_done - process_start:6.2f}s")

    client = genai.Client(vertexai=True, project=config.vertex_project, location=config.vertex_location)
    client_done = time.perf_counter()
    print(f"  construct client             {client_done - import_done:6.2f}s")

    print("\n=== requests (streaming) ===")
    samples: list[tuple[float, float]] = []
    for index in range(6):
        start = time.perf_counter()
        ttft = None
        chars = 0
        try:
            for chunk in client.models.generate_content_stream(
                model=model, contents="Count from 1 to 20, comma separated."
            ):
                if ttft is None:
                    ttft = time.perf_counter() - start
                chars += len(chunk.text or "")
        except Exception as exc:  # noqa: BLE001
            print(f"  request {index + 1}: FAILED {type(exc).__name__}: {str(exc)[:90]}")
            continue
        total = time.perf_counter() - start
        samples.append((ttft or total, total))
        label = "COLD" if index == 0 else "warm"
        print(f"  request {index + 1} [{label}]: ttft={ttft:5.2f}s  total={total:5.2f}s  ({chars} chars)")

    if len(samples) > 1:
        warm_ttft = [s[0] for s in samples[1:]]
        warm_total = [s[1] for s in samples[1:]]
        print("\n=== summary ===")
        print(f"  cold start (request 1)       {samples[0][1]:6.2f}s")
        print(f"  warm ttft  median            {statistics.median(warm_ttft):6.2f}s")
        print(f"  warm total median            {statistics.median(warm_total):6.2f}s")
        print(f"  warm best                    {min(warm_total):6.2f}s")

    print("\n=== what this means for the CLI ===")
    print("  one-shot (-p): pays import + cold start every time")
    print("  REPL:          pays it once, then warm-per-prompt")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
