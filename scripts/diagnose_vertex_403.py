"""Is the Vertex 403 a missing quota project, or a missing IAM role? Test, do not guess.

    python scripts/diagnose_vertex_403.py

Tries the same Vertex call under different credential configurations and reports which ones work.
The distinction matters because the two fixes are owned by different people:

  * missing quota project -> fixable here, in one command
  * missing IAM role      -> a grant on the workforce pool, done by a GCP admin
"""
import os
import sys
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
warnings.filterwarnings("ignore")

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
MODEL = "gemini-3.8-flash"


def attempt(label: str, *, quota_project: str | None, scopes: list[str] | None = None):
    """Make one Vertex call with an explicitly-controlled credential and report the outcome."""
    try:
        import google.auth
        import google.auth.transport.requests
        import google.genai as genai

        kwargs = {"scopes": scopes} if scopes else {}
        creds, detected = google.auth.default(**kwargs)
        if quota_project:
            creds = creds.with_quota_project(quota_project)
        creds.refresh(google.auth.transport.requests.Request())

        client = genai.Client(vertexai=True, project=quota_project or detected, location="global", credentials=creds)
        response = client.models.generate_content(model=MODEL, contents="say ok")
        print(f"  {label:<46} OK      -> {response.text!r}")
        return True
    except Exception as exc:  # noqa: BLE001
        message = str(exc)
        if "aiplatform.endpoints.predict" in message or "PERMISSION_DENIED" in message:
            short = "403 aiplatform.endpoints.predict denied"
        elif "serviceusage" in message.lower() or "quota" in message.lower():
            short = "403 serviceusage/quota"
        elif "401" in message or "Invalid Credentials" in message:
            short = "401 invalid credentials"
        else:
            short = f"{type(exc).__name__}: {message[:90]}"
        print(f"  {label:<46} FAILED  {short}")
        return False


def main() -> int:
    print(f"project={PROJECT} model={MODEL}\n")
    print("=== variants ===")
    attempt("ADC as-is (no quota project)", quota_project=None)
    attempt("ADC + explicit quota project", quota_project=PROJECT)
    attempt(
        "ADC + quota project + cloud-platform scope",
        quota_project=PROJECT,
        scopes=["https://www.googleapis.com/auth/cloud-platform"],
    )

    print("\n=== what the credential reports about itself ===")
    try:
        import google.auth

        creds, detected = google.auth.default()
        print(f"  credential class   : {type(creds).__name__}")
        print(f"  detected project   : {detected}")
        print(f"  quota_project_id   : {getattr(creds, 'quota_project_id', None)}")
        audience = getattr(creds, "_audience", None) or getattr(creds, "audience", None)
        print(f"  audience (WIF pool): {audience or '(none)'}")
        print(f"  is workforce fed.  : {bool(audience)}")
    except Exception as exc:  # noqa: BLE001
        print(f"  could not introspect: {type(exc).__name__}: {str(exc)[:120]}")

    print("\n=== interpretation ===")
    print("  If a variant above succeeds, the fix is the quota project, and it is a one-line command:")
    print(f"    gcloud auth application-default set-quota-project {PROJECT}")
    print("  If all variants fail with aiplatform.endpoints.predict denied, the workforce pool lacks")
    print("  an IAM role on the project and an admin must grant it:")
    print("    roles/aiplatform.user               (always required for Vertex calls)")
    print("    roles/serviceusage.serviceUsageConsumer  (required to bill/quota against the project)")
    print("  The member to grant it to is the pool principal, of the form:")
    print("    principal://iam.googleapis.com/locations/global/workforcePools/"
          "YOUR_POOL/subject/<subject-id>")
    print("  or the whole pool: principalSet://iam.googleapis.com/locations/global/workforcePools/"
          "YOUR_POOL/*")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
