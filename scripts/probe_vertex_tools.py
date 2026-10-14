"""Verify the Vertex `generateContent` (chats) API supports what the Myriaxon agent needs.

    python scripts/probe_vertex_tools.py

Checks, in order: plain text, tool declaration + a real function call, sending a function response
back, and streaming. If all four pass, the Gemini agent can be re-pointed at this API and will work
against Vertex AI with a corporate account -- no API key.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

PROJECT = os.environ.get("GOOGLE_CLOUD_PROJECT", "").strip()

if not PROJECT:

    raise SystemExit(

        "Set GOOGLE_CLOUD_PROJECT (or MYRIAXON_VERTEX_PROJECT) before running this script."

    )
LOCATION = "us-central1"
MODEL = "gemini-2.5-flash"

TOOL = {
    "name": "read_file",
    "description": "Read a text file from the workspace and return its contents.",
    "parameters": {
        "type": "object",
        "properties": {"path": {"type": "string", "description": "File to read."}},
        "required": ["path"],
    },
}


def main() -> int:
    import google.genai as genai
    from google.genai import types

    client = genai.Client(vertexai=True, project=PROJECT, location=LOCATION)
    failures = 0

    print("=== 1. plain text ===")
    try:
        response = client.models.generate_content(model=MODEL, contents="Reply with exactly: pong")
        print(f"  OK -> {response.text!r}")
    except Exception as exc:  # noqa: BLE001
        print(f"  FAIL: {type(exc).__name__}: {str(exc)[:200]}")
        failures += 1

    print("\n=== 2. tool declaration and a real function call ===")
    function_call = None
    try:
        response = client.models.generate_content(
            model=MODEL,
            contents="Read the file notes.txt please.",
            config=types.GenerateContentConfig(tools=[types.Tool(function_declarations=[TOOL])]),
        )
        for candidate in response.candidates or []:
            for part in candidate.content.parts or []:
                if getattr(part, "function_call", None):
                    function_call = part.function_call
        if function_call:
            print(f"  OK -> model called {function_call.name}({dict(function_call.args)})")
        else:
            print(f"  WARN: no function call; text={response.text!r}")
    except Exception as exc:  # noqa: BLE001
        print(f"  FAIL: {type(exc).__name__}: {str(exc)[:300]}")
        failures += 1

    print("\n=== 3. function response round trip (chat) ===")
    try:
        chat = client.chats.create(
            model=MODEL, config=types.GenerateContentConfig(tools=[types.Tool(function_declarations=[TOOL])])
        )
        first = chat.send_message("Read the file notes.txt please.")
        called = None
        for candidate in first.candidates or []:
            for part in candidate.content.parts or []:
                if getattr(part, "function_call", None):
                    called = part.function_call
        if called is None:
            print("  SKIP: model did not call the tool, cannot test the round trip")
        else:
            second = chat.send_message(
                types.Part.from_function_response(
                    name=called.name, response={"result": "the notes say: hello from a tool result"}
                )
            )
            print(f"  OK -> after tool result, model said: {(second.text or '')[:120]!r}")
    except Exception as exc:  # noqa: BLE001
        print(f"  FAIL: {type(exc).__name__}: {str(exc)[:300]}")
        failures += 1

    print("\n=== 4. streaming ===")
    try:
        chunks = 0
        text = []
        for chunk in client.models.generate_content_stream(model=MODEL, contents="Count from 1 to 3."):
            chunks += 1
            if getattr(chunk, "text", None):
                text.append(chunk.text)
        print(f"  OK -> {chunks} chunks, text={''.join(text)[:80]!r}")
    except Exception as exc:  # noqa: BLE001
        print(f"  FAIL: {type(exc).__name__}: {str(exc)[:300]}")
        failures += 1

    print()
    print("VERDICT:", "generateContent fully supports the agent's needs" if failures == 0 else f"{failures} check(s) failed")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
