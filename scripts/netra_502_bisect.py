"""Isolate what makes Netra return 502 for the agent's request.

    python scripts/netra_502_bisect.py

Varies one dimension at a time from the known-good bare request toward the agent's real request, so
the trigger is identified rather than assumed. A 502 from a gateway in ~0.4s means the provider is
rejecting or failing to route the request, not that the model took too long.
"""
import json
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

MODEL = "deepseek/deepseek-v4.1-flash"


def attempt(client, label, *, tools=None, system="", user="say pong", max_tokens=200, runs=2, extra=None):
    results = []
    for _ in range(runs):
        start = time.perf_counter()
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": user})
        kwargs = {"model": MODEL, "messages": messages, "max_tokens": max_tokens, "stream": True}
        if tools:
            kwargs["tools"] = tools
        if extra:
            kwargs.update(extra)
        try:
            chunks = sum(1 for _ in client.chat.completions.create(**kwargs))
            results.append(f"OK({chunks})")
        except Exception as exc:  # noqa: BLE001
            results.append(str(getattr(exc, "status_code", type(exc).__name__)))
    ok = sum(1 for r in results if r.startswith("OK"))
    mark = "PASS" if ok == runs else "FAIL"
    print(f"  [{mark}] {label:<44} {results}")
    return ok == runs


def main() -> int:
    from myriaxon import tool_registry
    from myriaxon.direct_http_client import DirectOpenAI

    client = DirectOpenAI(api_key=os.environ["NETRA_API_KEY"], base_url="https://api.netraruntime.com/v1")
    all_tools = tool_registry.build_tool_defs_openai()

    print("=== baseline (known good) ===")
    attempt(client, "bare, no tools, max_tokens=200", max_tokens=200)

    print("\n=== tools only ===")
    for n in (1, 8, 24):
        attempt(client, f"{n} tools, max_tokens=200", tools=all_tools[:n], max_tokens=200)

    print("\n=== max_tokens only, no tools ===")
    for mt in (200, 1024, 4096, 8192, 16384):
        attempt(client, f"no tools, max_tokens={mt}", max_tokens=mt)

    print("\n=== system prompt only ===")
    for length in (20, 200, 2000, 8000):
        attempt(client, f"system={length} chars, no tools", system="You are Myriaxon. " * (length // 17), max_tokens=200)

    print("\n=== combinations ===")
    attempt(client, "24 tools + max_tokens=4096", tools=all_tools, max_tokens=4096)
    attempt(client, "24 tools + system + max_tokens=4096", tools=all_tools, system="You are Myriaxon. " * 140, max_tokens=4096)
    attempt(client, "24 tools + max_tokens=4096 + reasoning low", tools=all_tools, max_tokens=4096,
            extra={"extra_body": {"reasoning": {"effort": "low"}}})

    print("\n=== payload size ===")
    for n in (24,):
        print(f"  24 tools json = {len(json.dumps(all_tools)):,} bytes")

    client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
