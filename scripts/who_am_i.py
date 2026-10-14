"""Which identity do Vertex AI calls actually assert right now?

    python scripts/who_am_i.py

Distinguishes the two things that are easy to conflate:

  * the PROJECT  -- which GCP project is billed and which models/quotas apply
  * the PRINCIPAL -- which identity Google sees on the request (this is what WIF changes)

The principal is answered by asking Google, not by inspecting the local file, because the local
file's `account` field can be empty and is not what the service sees.
"""
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


def main() -> int:
    import google.auth
    import google.auth.transport.requests

    creds, project = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    creds.refresh(google.auth.transport.requests.Request())

    print("=== credential type ===")
    print(f"  class            : {type(creds).__name__}")
    print(f"  is external (WIF): {type(creds).__name__ in ('Credentials',) and hasattr(creds, 'service_account_email') and 'external' in str(getattr(creds, '_audience', '')).lower()}")
    print(f"  project          : {project}")

    # What Google itself says the identity is. oauth2 v3 userinfo is served for both Google accounts
    # and federated ones; a workforce identity returns the federated subject here.
    print("\n=== what Google believes the principal is ===")
    try:
        import google.auth.transport.requests as tr

        session = tr.AuthorizedSession(creds)
        response = session.get("https://www.googleapis.com/oauth2/v3/userinfo", timeout=30)
        print(f"  userinfo status  : {response.status_code}")
        if response.status_code == 200:
            data = response.json()
            for key in ("email", "email_verified", "sub", "hd"):
                if key in data:
                    print(f"  {key:<16}: {data[key]}")
            print("  -> a Google account identity (authorized_user path, NOT workforce federation)")
        else:
            print(f"  body: {response.text[:200]}")
    except Exception as exc:  # noqa: BLE001
        print(f"  userinfo failed  : {type(exc).__name__}: {str(exc)[:160]}")

    print("\n=== does the token carry a workforce audience? ===")
    audience = getattr(creds, "_audience", None) or getattr(creds, "audience", None)
    print(f"  audience         : {audience or '(none -- not a WIF credential)'}")
    if audience:
        print("  -> this IS a workforce-federated credential")
    else:
        print("  -> this is NOT workforce federation; the identity is a Google account")

    print("\n=== can it still reach Vertex (the thing that matters) ===")
    try:
        import google.genai as genai

        client = genai.Client(vertexai=True, project=project, location="global")
        response = client.models.generate_content(model="gemini-3.8-flash", contents="say ok")
        print(f"  gemini-3.8-flash : OK -> {response.text!r}")
    except Exception as exc:  # noqa: BLE001
        print(f"  FAILED           : {type(exc).__name__}: {str(exc)[:200]}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
