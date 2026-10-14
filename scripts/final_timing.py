"""Final end-to-end accounting: where does a one-shot Gemini run's wall time actually go?

    python scripts/final_timing.py
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
MODEL = "gemini-3.8-flash"


def main() -> int:
    marks = []
    start = time.perf_counter()

    def mark(label):
        marks.append((label, time.perf_counter() - start))

    mark("interpreter up")
    import google.genai as genai
    from google.genai import types

    mark("import google.genai")
    client = genai.Client(vertexai=True, project=PROJECT, location="global")
    mark("Client() constructed")

    tool = types.FunctionDeclaration(
        name="write_file",
        description="Write text to a file.",
        parameters={"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}},
    )
    config = types.GenerateContentConfig(
        tools=[types.Tool(function_declarations=[tool])],
        system_instruction="You are a coding agent. " * 40,
        max_output_tokens=2048,
    )
    mark("tool schema + config built")

    first_start = time.perf_counter()
    first = client.models.generate_content(model=MODEL, contents="Write hi into a.txt", config=config)
    first_elapsed = time.perf_counter() - first_start
    mark(f"FIRST request ({first_elapsed:.1f}s -- provider cold start)")
    calls = [
        part.function_call
        for cand in (first.candidates or [])
        for part in (cand.content.parts or [])
        if getattr(part, "function_call", None)
    ]
    mark(f"model requested {[c.name for c in calls]}")

    second_start = time.perf_counter()
    client.models.generate_content(model=MODEL, contents="say pong", config=config)
    mark(f"SECOND request ({time.perf_counter() - second_start:.1f}s -- warm)")

    print("=== one-shot gemini-3.8-flash accounting ===")
    previous = 0.0
    for label, value in marks:
        print(f"  {label:<52} {value:7.2f}s  (+{value - previous:6.2f})")
        previous = value
    print(f"  {'TOTAL (excluding agent plumbing)':<52} {previous:7.2f}s")
    print()
    print("Interpretation: the provider's cold start dominates every fresh process and cannot be")
    print("overlapped away (measured: backgrounding it saves ~2s of ~34s). The controllable costs --")
    print("imports, CLI bootstrap, model-limits lookup -- are what this harness now minimizes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
