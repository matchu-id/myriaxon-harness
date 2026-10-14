"""Find the fastest Netra configuration for Myriaxon's actual request shape.

    python scripts/tune_netra.py

Uses the agent's REAL prompt and tool schema set, with one tool round trip, so the numbers reflect
production rather than a bare "say pong". Tests the levers that plausibly matter:
  * model variant          (v4.1-flash vs v4-flash-0731)
  * reasoning effort       (none / low / medium / high)
  * max_tokens ceiling
  * whether reasoning is what dominates at all
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

BASE_URL = "https://api.netraruntime.com/v1"
MODELS = ["deepseek/deepseek-v4.1-flash", "deepseek/deepseek-v4-flash-0731"]

PROMPT = "Create a file named probe.txt containing the word tuned"
SYSTEM = "You are Myriaxon, a terminal-based coding agent.\nCurrent working directory: C:\\tmp\n" * 20


def make_tools():
    from myriaxon import tool_registry

    return tool_registry.build_tool_defs_openai()


def timed_request(client, model, tools, extra_body, max_tokens, prompt=PROMPT, runs=3, rounds=2):
    """Median wall time for a request, optionally including one tool-result continuation."""
    from myriaxon.direct_http_client import DirectAPIError  # noqa: F401  (documented failure mode)

    samples = []
    for _ in range(runs):
        start = time.perf_counter()
        messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}]
        try:
            for _round in range(rounds):
                calls = {}
                text = []
                for chunk in client.chat.completions.create(
                    model=model, messages=messages, tools=tools, max_tokens=max_tokens, stream=True, **extra_body
                ):
                    if chunk.usage:
                        continue
                    if not chunk.choices:
                        continue
                    delta = chunk.choices[0].delta
                    if delta.content:
                        text.append(delta.content)
                    for call in delta.tool_calls or []:
                        entry = calls.setdefault(call.index, {"id": None, "name": None, "args": ""})
                        if call.id:
                            entry["id"] = call.id
                        if call.function.name:
                            entry["name"] = call.function.name
                        if call.function.arguments:
                            entry["args"] += call.function.arguments
                if not calls:
                    break
                assistant = {
                    "role": "assistant",
                    "content": "".join(text) or None,
                    "tool_calls": [
                        {
                            "id": c["id"] or "call_0",
                            "type": "function",
                            "function": {"name": c["name"], "arguments": c["args"]},
                        }
                        for c in calls.values()
                    ],
                }
                messages.append(assistant)
                for c in calls.values():
                    messages.append(
                        {"role": "tool", "tool_call_id": c["id"] or "call_0", "content": "File written OK"}
                    )
            samples.append(time.perf_counter() - start)
        except Exception as exc:  # noqa: BLE001
            return None, f"{type(exc).__name__}: {str(exc)[:80]}"
    return statistics.median(samples), None


def main() -> int:
    from myriaxon.direct_http_client import DirectOpenAI

    client = DirectOpenAI(api_key=os.environ["NETRA_API_KEY"], base_url=BASE_URL, timeout=180.0)
    tools = make_tools()
    print(f"{len(tools)} tools, system+prompt ~{len(SYSTEM)+len(PROMPT):,} chars\n")

    variants = [
        ("no reasoning key at all", {}),
        ("reasoning.effort=none", {"extra_body": {"reasoning": {"effort": "none"}}}),
        ("reasoning.effort=low", {"extra_body": {"reasoning": {"effort": "low"}}}),
        ("reasoning.effort=medium", {"extra_body": {"reasoning": {"effort": "medium"}}}),
        ("reasoning.effort=high", {"extra_body": {"reasoning": {"effort": "high"}}}),
        ("reasoning.enabled=false", {"extra_body": {"reasoning": {"enabled": False}}}),
    ]

    for model in MODELS:
        print(f"=== {model} ===")
        for label, extra in variants:
            median, error = timed_request(client, model, tools, extra, 2048)
            if error:
                print(f"  {label:<28} FAILED {error}")
            else:
                print(f"  {label:<28} median={median:6.2f}s")

        print("  -- max_tokens sensitivity (no reasoning key) --")
        for mt in (256, 512, 1024, 4096):
            median, error = timed_request(client, model, tools, {}, mt)
            print(f"  max_tokens={mt:<6}            median={median:6.2f}s" if not error else f"  max_tokens={mt} FAILED {error}")
        print()

    client.close()
    print("=== how to apply the winner ===")
    print("  MYRIAXON_EFFORT=<level>   set the effort the agent sends")
    print("  or edit ~/.myriaxon/config.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
