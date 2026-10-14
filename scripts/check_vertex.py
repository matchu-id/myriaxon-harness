"""Verify the machine's GCP credentials can actually reach Vertex AI Gemini.

    python scripts/check_vertex.py [--model gemini-2.5-flash] [--location us-central1]

Prints what it found and what happened, in that order, so a failure is diagnosable without reading
any source. Never prints the credential contents -- only its type, its account and its quota project.
"""
import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="gemini-2.5-flash")
    parser.add_argument("--location", default="us-central1")
    parser.add_argument("--project", default="")
    args = parser.parse_args()

    adc = Path(os.environ.get("APPDATA", "")) / "gcloud" / "application_default_credentials.json"
    print("=== credential discovery ===")
    print(f"ADC path          : {adc}")
    print(f"ADC exists        : {adc.exists()}")
    if not adc.exists():
        print("\nNo application default credentials. Run: gcloud auth application-default login")
        return 2

    try:
        import json

        data = json.loads(adc.read_text(encoding="utf-8-sig"))
        print(f"ADC type          : {data.get('type')}")
        print(f"ADC quota project : {data.get('quota_project_id')}")
        print(f"has refresh_token : {bool(data.get('refresh_token'))}")
    except Exception as exc:  # noqa: BLE001
        print(f"ADC unreadable: {exc!r}")
        return 2

    project = args.project or data.get("quota_project_id") or os.environ.get("GOOGLE_CLOUD_PROJECT", "")
    print(f"project to use    : {project}")
    print(f"location          : {args.location}")

    try:
        import google.auth

        creds, detected_project = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
        print("\n=== google.auth.default() ===")
        print(f"credential class  : {type(creds).__name__}")
        print(f"detected project  : {detected_project}")
        print(f"has refresh token : {bool(getattr(creds, 'refresh_token', None))}")
        project = project or detected_project
    except Exception as exc:  # noqa: BLE001
        print(f"\ngoogle.auth.default() FAILED: {type(exc).__name__}: {exc}")
        return 1

    print("\n=== refreshing the access token ===")
    try:
        import google.auth.transport.requests

        creds.refresh(google.auth.transport.requests.Request())
        print(f"token refreshed   : True (expiry {getattr(creds, 'expiry', '?')})")
    except Exception as exc:  # noqa: BLE001
        print(f"token refresh FAILED: {type(exc).__name__}: {str(exc)[:400]}")
        print("\nA failed refresh usually means the credential was revoked or the SSO session expired.")
        print("Re-run: gcloud auth application-default login")
        return 1

    print("\n=== live Vertex AI call ===")
    try:
        import google.genai as genai

        client = genai.Client(vertexai=True, project=project, location=args.location)
        response = client.models.generate_content(
            model=args.model, contents="Reply with exactly: pong"
        )
        print(f"model={args.model} -> {response.text!r}")
    except Exception as exc:  # noqa: BLE001
        print(f"Vertex call FAILED: {type(exc).__name__}: {str(exc)[:500]}")
        print("\nCommon causes: the model id is not enabled in this project/location, the project")
        print("lacks the Vertex AI API, or the region does not serve that model.")
        return 1

    print("\nSUCCESS -- Myriaxon can use this account as a Gemini provider via Vertex.")
    print("Configure with:")
    print(f'  $env:GOOGLE_GENAI_USE_VERTEXAI = "true"')
    print(f'  $env:GOOGLE_CLOUD_PROJECT = "{project}"')
    print(f'  $env:GOOGLE_CLOUD_LOCATION = "{args.location}"')
    print(f"  python -m myriaxon --provider gemini --model {args.model}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
