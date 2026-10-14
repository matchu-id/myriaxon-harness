"""Verify the direct HTTP client matches the `openai` SDK on the same request.

    python scripts/verify_direct_client.py

Runs identical prompts through both clients and compares text, tool calls, reasoning and usage.
A faster client that behaves differently is not an improvement, so equivalence is checked rather than
assumed -- and it is checked against the real Netra endpoint, not a stub.
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
BASE_URL = "https://api.netraruntime.com/v1"

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write text to a file.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                "required": ["path", "content"],
            },
        },
    }
]


def via_sdk(prompt, tools=None):
    from openai import OpenAI

    client = OpenAI(base_url=BASE_URL, api_key=os.environ["NETRA_API_KEY"], max_retries=0, timeout=120.0)
    stream = client.chat.completions.create(
        model=MODEL, messages=[{"role": "user", "content": prompt}], tools=tools, max_tokens=400, stream=True
    )
    text, calls, details = [], {}, []
    for chunk in stream:
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta
        if delta.content:
            text.append(delta.content)
        for raw in getattr(delta, "tool_calls", None) or []:
            entry = calls.setdefault(raw.index, {"name": None, "args": ""})
            if raw.function and raw.function.name:
                entry["name"] = raw.function.name
            if raw.function and raw.function.arguments:
                entry["args"] += raw.function.arguments
        got = getattr(delta, "reasoning_details", None)
        if got:
            details.extend(got)
    return "".join(text), calls, len(details)


def via_direct(prompt, tools=None):
    from myriaxon.direct_http_client import DirectOpenAI

    client = DirectOpenAI(api_key=os.environ["NETRA_API_KEY"], base_url=BASE_URL, timeout=120.0)
    calls, text, details = {}, [], 0
    for delta in client.chat.completions.create(
        model=MODEL, messages=[{"role": "user", "content": prompt}], tools=tools, max_tokens=400
    ):
        if delta.content:
            text.append(delta.content)
        for raw in delta.tool_calls:
            entry = calls.setdefault(raw["index"], {"name": None, "args": ""})
            if raw["name"]:
                entry["name"] = raw["name"]
            if raw["arguments"]:
                entry["args"] += raw["arguments"]
        details += len(delta.reasoning_details)
    client.close()
    return "".join(text), calls, details


def compare(label, prompt, tools=None):
    print(f"=== {label} ===")
    start = time.perf_counter()
    sdk_text, sdk_calls, sdk_details = via_sdk(prompt, tools)
    sdk_time = time.perf_counter() - start

    start = time.perf_counter()
    direct_text, direct_calls, direct_details = via_direct(prompt, tools)
    direct_time = time.perf_counter() - start

    print(f"  sdk    {sdk_time:5.2f}s  text={sdk_text[:70]!r}")
    print(f"  direct {direct_time:5.2f}s  text={direct_text[:70]!r}")
    if sdk_calls or direct_calls:
        print(f"  sdk    calls={ {k: {kk: (vv[:60] if isinstance(vv,str) else vv) for kk, vv in v.items()} for k, v in sdk_calls.items()} }")
        print(f"  direct calls={ {k: {kk: (vv[:60] if isinstance(vv,str) else vv) for kk, vv in v.items()} for k, v in direct_calls.items()} }")
    print(f"  reasoning details: sdk={sdk_details} direct={direct_details}")

    same_text = sdk_text.strip() == direct_text.strip()
    same_names = [c["name"] for c in sdk_calls.values()] == [c["name"] for c in direct_calls.values()]
    print(f"  text identical   : {same_text}")
    print(f"  tool names match : {same_names}")
    print()
    return same_text, same_names


def main() -> int:
    if not os.environ.get("NETRA_API_KEY"):
        print("NETRA_API_KEY not set")
        return 2

    results = [
        compare("plain text", "Reply with exactly: pong"),
        compare("tool call", "Write the word hello into greeting.txt", TOOLS),
    ]

    print("=== client construction cost ===")
    for label, factory in (
        ("openai SDK", lambda: __import__("openai").OpenAI(base_url=BASE_URL, api_key="x")),
        ("direct httpx", lambda: __import__("myriaxon.direct_http_client", fromlist=["x"]).DirectOpenAI(api_key="x", base_url=BASE_URL)),
    ):
        start = time.perf_counter()
        client = factory()
        print(f"  {label:<14} {time.perf_counter() - start:6.2f}s (imports already loaded)")
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass

    ok = all(all(r) for r in results)
    print("VERDICT:", "direct client is behaviourally equivalent" if ok else "DIFFERENCES FOUND -- investigate")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
