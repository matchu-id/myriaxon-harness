"""Where does a Netra-backed Myriaxon run actually spend its time?

    python scripts/timing_netra.py

Separates the three costs that get conflated when someone says "the harness is slow":
  * interpreter + SDK import      (fixed per process)
  * agent construction            (client + model lookup)
  * model round trips             (the provider's, and the only one that scales with work)
"""
import json
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

MODEL = os.environ.get("MYRIAXON_MODEL", "deepseek/deepseek-v4.1-flash")
BASE_URL = "https://api.netraruntime.com/v1"


def main() -> int:
    process_start = time.perf_counter()
    marks = []

    def mark(label):
        marks.append((label, time.perf_counter() - process_start))

    mark("interpreter up")

    import openai  # noqa: F401

    mark("import openai SDK")

    api = openai.OpenAI(base_url=BASE_URL, api_key=os.environ["NETRA_API_KEY"], max_retries=0, timeout=180.0)
    mark("client constructed")

    # The agent's own tool schema set, to size the request realistically.
    from myriaxon import tool_registry

    tools = tool_registry.build_tool_defs_openai()
    schemas = json.dumps(tools)
    mark(f"tool schemas built ({len(tools)} tools, {len(schemas):,} chars)")

    system = "You are a coding agent. " * 40
    print(f"model={MODEL}\n")

    print("=== model round trips (median of 3) ===")
    for label, extra, tools_arg in (
        ("plain, no tools, no reasoning", {}, None),
        ("with 22 tool schemas", {}, tools),
        ("with tools + reasoning.effort=low", {"extra_body": {"reasoning": {"effort": "low"}}}, tools),
        ("with tools + reasoning.effort=high", {"extra_body": {"reasoning": {"effort": "high"}}}, tools),
    ):
        samples = []
        for _ in range(3):
            start = time.perf_counter()
            try:
                api.chat.completions.create(
                    model=MODEL,
                    messages=[{"role": "system", "content": system}, {"role": "user", "content": "Reply with exactly: pong"}],
                    max_tokens=300,
                    tools=tools_arg,
                    **extra,
                )
                samples.append(time.perf_counter() - start)
            except Exception as exc:  # noqa: BLE001
                print(f"  {label:<38} FAILED {type(exc).__name__}: {str(exc)[:80]}")
                samples = []
                break
        if samples:
            print(f"  {label:<38} min={min(samples):6.2f}s  median={statistics.median(samples):6.2f}s")

    print("\n=== startup phases (seconds since process start) ===")
    previous = 0.0
    for label, value in marks:
        print(f"  {label:<46} {value:7.2f}  (+{value - previous:6.2f})")
        previous = value
    print(f"  {'TOTAL before any model call':<46} {previous:7.2f}")

    print("\n=== interpretation ===")
    print(f"  the SDK import ({marks[1][1]:.1f}s here) is paid once per PROCESS.")
    print("  two ways to stop paying it per command:")
    print("    1. use the REPL (python -m myriaxon) -- one process, many prompts")
    print("    2. keep a warm process (the deferrals already made --help 1.7s)")
    print("  a different LANGUAGE would not remove this cost: it is the SDK's own module count,")
    print("  dominated by openai.types.* which Myriaxon never uses.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
