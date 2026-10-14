"""Probe which Vertex AI models and locations this GCP account can actually use.

    python scripts/probe_vertex_models.py

Uses the ambient Application Default Credentials. Prints a grid of location x model results so the
working combination is found by evidence rather than guesswork -- Google's model availability varies
per project, per region, and per tenant.
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
LOCATIONS = ["global", "us-central1", "us-east5", "europe-west4"]
MODELS = ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-1.5-flash", "gemini-2.5-pro"]


def main() -> int:
    import google.genai as genai

    print(f"project={PROJECT}")
    print(f"{'location':<14} {'model':<20} result")
    working = []
    for location in LOCATIONS:
        for model in MODELS:
            try:
                client = genai.Client(vertexai=True, project=PROJECT, location=location)
                response = client.models.generate_content(model=model, contents="say pong")
                text = (response.text or "").strip()[:30]
                print(f"{location:<14} {model:<20} OK -> {text!r}")
                working.append((location, model))
            except Exception as exc:  # noqa: BLE001 -- this script exists to report failures
                message = str(exc)
                if "Unsupported model interaction" in message:
                    short = "unsupported model interaction"
                elif "404" in message or "NOT_FOUND" in message:
                    short = "not found"
                elif "permission" in message.lower() or "403" in message:
                    short = "permission denied"
                elif "not supported" in message.lower():
                    short = "not supported"
                else:
                    short = f"{type(exc).__name__}: {message[:70]}"
                print(f"{location:<14} {model:<20} FAIL: {short}")

    print()
    if working:
        print("WORKING combinations:")
        for location, model in working:
            print(f"  location={location} model={model}")
    else:
        print("No combination worked. The project may not have the Vertex AI API enabled, or the")
        print("tenant may restrict which models the workforce identity can call.")
    return 0 if working else 1


if __name__ == "__main__":
    raise SystemExit(main())
