"""Where does gemini-3.8-flash latency actually go? Warm vs cold, streaming vs not, tools vs not.

    python scripts/measure_vertex_latency.py
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


def timed(label, fn, runs=3):
    samples = []
    for _ in range(runs):
        start = time.perf_counter()
        try:
            fn()
            samples.append(time.perf_counter() - start)
        except Exception as exc:  # noqa: BLE001
            print(f"  {label:<34} FAILED {type(exc).__name__}: {str(exc)[:70]}")
            return
    print(
        f"  {label:<34} min={min(samples):6.2f}s  median={statistics.median(samples):6.2f}s  max={max(samples):6.2f}s"
    )


def main() -> int:
    import google.genai as genai
    from google.genai import types

    client = genai.Client(vertexai=True, project=PROJECT, location="global")
    tool = types.FunctionDeclaration(
        name="write_file",
        description="Write text to a file.",
        parameters={"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}},
    )
    tools = [types.Tool(function_declarations=[tool])]

    print(f"model={MODEL} location=global\n")
    print("=== plain text ===")
    timed("generate_content (no config)", lambda: client.models.generate_content(model=MODEL, contents="say pong"))
    timed(
        "generate_content (config, no tools)",
        lambda: client.models.generate_content(
            model=MODEL, contents="say pong", config=types.GenerateContentConfig(max_output_tokens=2048)
        ),
    )

    print("\n=== with tool schemas (what the agent always sends) ===")
    timed(
        "generate_content + 24 tools",
        lambda: client.models.generate_content(
            model=MODEL,
            contents="say pong",
            config=types.GenerateContentConfig(
                tools=[
                    types.Tool(
                        function_declarations=[
                            types.FunctionDeclaration(
                                name=f"tool_{i}",
                                description="A tool with a realistically long description for this harness.",
                                parameters={
                                    "type": "object",
                                    "properties": {"path": {"type": "string"}},
                                    "required": ["path"],
                                },
                            )
                            for i in range(24)
                        ]
                    )
                ],
                system_instruction="You are a coding agent. " * 40,
                max_output_tokens=2048,
            ),
        ),
    )

    print("\n=== streaming time-to-first-token ===")
    def stream_ttft():
        start = time.perf_counter()
        first = None
        for _chunk in client.models.generate_content_stream(model=MODEL, contents="Count from 1 to 10."):
            if first is None:
                first = time.perf_counter() - start
        print(f"      ttft={first:.2f}s total={time.perf_counter() - start:.2f}s", end="")
    timed("generate_content_stream", stream_ttft, runs=2)

    print("\n=== comparison: 2.5-flash, same shape ===")
    timed(
        "2.5-flash generate_content + 24 tools",
        lambda: client.models.generate_content(
            model="gemini-2.5-flash",
            contents="say pong",
            config=types.GenerateContentConfig(
                tools=[
                    types.Tool(
                        function_declarations=[
                            types.FunctionDeclaration(
                                name=f"tool_{i}",
                                description="A tool with a realistically long description for this harness.",
                                parameters={
                                    "type": "object",
                                    "properties": {"path": {"type": "string"}},
                                    "required": ["path"],
                                },
                            )
                            for i in range(24)
                        ]
                    )
                ],
                system_instruction="You are a coding agent. " * 40,
                max_output_tokens=2048,
            ),
        ),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
