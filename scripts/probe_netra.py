"""Measure the Netra (netraruntime) endpoint: latency, streaming, reasoning, and tool calling.

    python scripts/probe_netra.py

Tool calling is tested explicitly because it is the one capability the harness cannot work without:
a model that answers questions but never calls a tool makes Myriaxon useless regardless of speed.
"""
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from myriaxon.dotenv_support import load_dotenv_into_environ  # noqa: E402

load_dotenv_into_environ()

BASE_URL = "https://api.netraruntime.com/v1"
MODEL = "deepseek/deepseek-v4.1-flash"

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


def client():
    import os

    from openai import OpenAI

    return OpenAI(
        base_url=BASE_URL,
        api_key=os.environ["NETRA_API_KEY"],
        max_retries=0,
        timeout=180.0,
    )


def timed(label, fn, runs=3):
    samples = []
    for _ in range(runs):
        start = time.perf_counter()
        try:
            fn()
            samples.append(time.perf_counter() - start)
        except Exception as exc:  # noqa: BLE001
            print(f"  {label:<34} FAILED {type(exc).__name__}: {str(exc)[:110]}")
            return None
    print(f"  {label:<34} min={min(samples):6.2f}s  median={statistics.median(samples):6.2f}s  max={max(samples):6.2f}s")
    return statistics.median(samples)


def main() -> int:
    import os

    if not os.environ.get("NETRA_API_KEY"):
        print("NETRA_API_KEY not set (add it to .env)")
        return 2

    api = client()
    print(f"endpoint={BASE_URL}\nmodel={MODEL}\n")

    print("=== 1. plain completion ===")
    timed(
        "non-streaming",
        lambda: api.chat.completions.create(
            model=MODEL, messages=[{"role": "user", "content": "Reply with exactly: pong"}], max_tokens=100
        ),
    )

    print("\n=== 2. the exact request shape from your snippet (extra_body + stream) ===")
    def snippet_request():
        stream = api.chat.completions.create(
            model=MODEL,
            messages=[{"role": "user", "content": "Hello!"}],
            temperature=1,
            top_p=1,
            stream=True,
            extra_body={"top_k": 40, "min_p": 0, "reasoning": {"effort": "low"}},
        )
        text = []
        reasoning = 0
        for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            details = getattr(delta, "reasoning_details", None)
            if details:
                reasoning += len(details)
            if delta.content:
                text.append(delta.content)
        print(f"      -> {len(''.join(text))} chars, {reasoning} reasoning detail(s)", end="")

    timed("stream + reasoning.effort=low", snippet_request, runs=2)

    print("\n=== 3. streaming time-to-first-token ===")
    start = time.perf_counter()
    first = None
    chunks = 0
    try:
        stream = api.chat.completions.create(
            model=MODEL, messages=[{"role": "user", "content": "Count from 1 to 10."}], stream=True, max_tokens=200
        )
        for chunk in stream:
            chunks += 1
            if first is None:
                first = time.perf_counter() - start
        print(f"  ttft={first:.2f}s  total={time.perf_counter() - start:.2f}s  chunks={chunks}")
    except Exception as exc:  # noqa: BLE001
        print(f"  FAILED {type(exc).__name__}: {str(exc)[:140]}")

    print("\n=== 4. TOOL CALLING (the harness depends on this) ===")
    try:
        response = api.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": "You are a coding agent. Use the tools provided."},
                {"role": "user", "content": "Write the word hello into greeting.txt"},
            ],
            tools=TOOLS,
            max_tokens=500,
        )
        message = response.choices[0].message
        calls = getattr(message, "tool_calls", None) or []
        if calls:
            for call in calls:
                print(f"  OK  -> called {call.function.name}({call.function.arguments})")
        else:
            print(f"  NO TOOL CALL -- content: {(message.content or '')[:120]!r}")
            print("  (this model may not support function calling here, which would block the harness)")
    except Exception as exc:  # noqa: BLE001
        print(f"  FAILED {type(exc).__name__}: {str(exc)[:200]}")

    print("\n=== 5. tool-result round trip ===")
    try:
        first = api.chat.completions.create(
            model=MODEL,
            messages=[{"role": "user", "content": "Write the word hello into greeting.txt"}],
            tools=TOOLS,
            max_tokens=500,
        )
        message = first.choices[0].message
        calls = getattr(message, "tool_calls", None) or []
        if not calls:
            print("  skipped (no tool call to respond to)")
        else:
            call = calls[0]
            second = api.chat.completions.create(
                model=MODEL,
                messages=[
                    {"role": "user", "content": "Write the word hello into greeting.txt"},
                    {
                        "role": "assistant",
                        "content": message.content,
                        "tool_calls": [
                            {
                                "id": call.id,
                                "type": "function",
                                "function": {"name": call.function.name, "arguments": call.function.arguments},
                            }
                        ],
                    },
                    {"role": "tool", "tool_call_id": call.id, "content": "Wrote greeting.txt"},
                ],
                tools=TOOLS,
                max_tokens=300,
            )
            print(f"  OK  -> after tool result: {(second.choices[0].message.content or '')[:110]!r}")
    except Exception as exc:  # noqa: BLE001
        print(f"  FAILED {type(exc).__name__}: {str(exc)[:200]}")

    print("\n=== 6. available models (first 15) ===")
    try:
        models = api.models.list()
        ids = [m.id for m in models][:15]
        for model_id in ids:
            print(f"  {model_id}")
    except Exception as exc:  # noqa: BLE001
        print(f"  model list unavailable: {type(exc).__name__}: {str(exc)[:120]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
