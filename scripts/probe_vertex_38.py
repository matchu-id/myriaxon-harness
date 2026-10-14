"""Which API surface does Vertex serve for gemini-3.8-flash? Clean output, warnings suppressed.

    python scripts/probe_vertex_38.py
"""
import sys
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

warnings.filterwarnings("ignore")

PROJECT = os.environ.get("GOOGLE_CLOUD_PROJECT", "").strip()

if not PROJECT:

    raise SystemExit(

        "Set GOOGLE_CLOUD_PROJECT (or MYRIAXON_VERTEX_PROJECT) before running this script."

    )
def main() -> int:
    import google.genai as genai
    from google.genai import types

    tool = {
        "name": "read_file",
        "description": "Read a text file.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
    }

    for location in ("global", "us-central1"):
        client = genai.Client(vertexai=True, project=PROJECT, location=location)
        print(f"--- location={location} ---")

        for label, call in (
            (
                "generateContent + tools",
                lambda c=client: c.models.generate_content(
                    model="gemini-3.8-flash",
                    contents="Read notes.txt",
                    config=types.GenerateContentConfig(tools=[types.Tool(function_declarations=[tool])]),
                ),
            ),
            (
                "generateContent plain",
                lambda c=client: c.models.generate_content(model="gemini-3.8-flash", contents="say pong"),
            ),
        ):
            try:
                response = call()
                calls = [
                    part.function_call
                    for candidate in (response.candidates or [])
                    for part in (candidate.content.parts or [])
                    if getattr(part, "function_call", None)
                ]
                if calls:
                    detail = f"called {calls[0].name}({dict(calls[0].args)})"
                else:
                    detail = repr((response.text or "")[:40])
                print(f"  {label:<26} OK   {detail}")
            except Exception as exc:  # noqa: BLE001
                print(f"  {label:<26} FAIL {type(exc).__name__}: {str(exc)[:100]}")

        try:
            response = client.interactions.create(
                model="gemini-3.8-flash", input=[{"type": "user_input", "content": "say pong"}]
            )
            print(f"  {'interactions API':<26} OK   {repr(getattr(response, 'output_text', None))[:40]}")
        except Exception as exc:  # noqa: BLE001
            print(f"  {'interactions API':<26} FAIL {type(exc).__name__}: {str(exc)[:100]}")
        print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
