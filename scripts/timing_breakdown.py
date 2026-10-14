"""Time the actual model round trips, separated from interpreter startup.

    python scripts/timing_breakdown.py

Answers "is Myriaxon slow because it is Python, or because of the model/endpoint" with numbers:
interpreter start, dependency imports, one generateContent call, one Interactions call, and a
full non-streaming agent-sized request with tool schemas attached.
"""
import subprocess
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
def seconds(fn):
    start = time.perf_counter()
    try:
        fn()
        return time.perf_counter() - start, None
    except Exception as exc:  # noqa: BLE001
        return time.perf_counter() - start, f"{type(exc).__name__}: {str(exc)[:90]}"


def main() -> int:
    print(f"interpreter       : {sys.version.split()[0]} ({sys.executable})")
    print(f"already running   : imports are paid once, so the numbers below exclude startup\n")

    import google.genai as genai
    from google.genai import types

    client = genai.Client(vertexai=True, project=PROJECT, location="global")

    print("=== model round trips (no streaming) ===")
    for label, fn in (
        ("generateContent 3.8-flash", lambda: client.models.generate_content(model="gemini-3.8-flash", contents="say pong")),
        ("generateContent 2.5-flash", lambda: client.models.generate_content(model="gemini-2.5-flash", contents="say pong")),
        ("interactions   3.8-flash", lambda: client.interactions.create(model="gemini-3.8-flash", input=[{"type": "user_input", "content": "say pong"}])),
    ):
        elapsed, error = seconds(fn)
        verdict = f"{elapsed:6.2f}s  OK" if error is None else f"{elapsed:6.2f}s  {error}"
        print(f"  {label:<28} {verdict}")

    print("\n=== agent-sized request (system prompt + 24 tool schemas, non-streaming) ===")
    tools = []
    for index in range(24):
        tools.append(
            types.FunctionDeclaration(
                name=f"tool_{index}",
                description="A tool with a description of roughly realistic length for this harness.",
                parameters={"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
            )
        )
    config = types.GenerateContentConfig(
        tools=[types.Tool(function_declarations=tools)],
        system_instruction="You are a coding agent. " * 40,
        max_output_tokens=2048,
    )
    elapsed, error = seconds(
        lambda: client.models.generate_content(model="gemini-3.8-flash", contents="say pong", config=config)
    )
    print(f"  {'24 tools + prompt':<28} {elapsed:6.2f}s  {error or 'OK'}")

    print("\n=== streaming time-to-first-token vs total ===")
    for label, factory in (
        (
            "generateContent_stream 3.8",
            lambda: client.models.generate_content_stream(model="gemini-3.8-flash", contents="Count from 1 to 20."),
        ),
    ):
        first = None
        count = 0
        start = time.perf_counter()
        try:
            for _chunk in factory():
                count += 1
                if first is None:
                    first = time.perf_counter() - start
            total = time.perf_counter() - start
            print(f"  {label:<28} first={first:.2f}s total={total:.2f}s chunks={count}")
        except Exception as exc:  # noqa: BLE001
            print(f"  {label:<28} FAILED {type(exc).__name__}: {str(exc)[:90]}")

    print("\n=== cold-start cost of the CLI (separate process, real) ===")
    for label, code in (
        ("python -c pass", "pass"),
        ("import myriaxon.cli", "import myriaxon.cli"),
    ):
        start = time.perf_counter()
        subprocess.run([sys.executable, "-c", code], capture_output=True)
        print(f"  {label:<28} {time.perf_counter() - start:6.2f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
