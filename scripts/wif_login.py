"""Check and drive the Workforce Identity Federation (WIF) sign-in for Myriaxon.

    python scripts/wif_login.py status      # what is configured / detected right now
    python scripts/wif_login.py login       # print the exact sign-in command for this machine
    python scripts/wif_login.py verify      # confirm a fresh WIF sign-in actually reaches Gemini

The sign-in itself cannot be automated: it is a browser SAML round trip against the corporate
identity provider, and it must be completed by a human in their own terminal. Everything around it
-- detecting what is missing, naming the precise command, and verifying the result -- is automated
here so the human step is one command rather than a research project.
"""
import argparse
import glob
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

POOL = "YOUR_POOL"
PROVIDER = "YOUR_PROVIDER"
PROJECT_FALLBACK = os.environ.get("GOOGLE_CLOUD_PROJECT", "").strip()
if not PROJECT_FALLBACK:
    raise SystemExit(
        "Set GOOGLE_CLOUD_PROJECT (or MYRIAXON_VERTEX_PROJECT) before running this script."
    )
# Candidate locations for the workforce login configuration file. The file is normally downloaded
# from the Cloud console (or produced by `gcloud iam workforce-pools create-login-config`); it is NOT
# readable from the tenant without the user's own access, which is why this script looks for it
# rather than generating one.
LOGIN_CONFIG_CANDIDATES = (
    r"~\.config\gcloud\workforce-login-config.json",
    r"~\.gemini\workforce-login-config.json",
    r"~\Downloads\workforce-login-config.json",
    r"~\Downloads\*login-config*.json",
    r"~\Downloads\*clientLibraryConfig*.json",
)


def gcloud_path() -> str:
    from_env = os.environ.get("GCLOUD_PATH")
    if from_env and Path(from_env).exists():
        return from_env
    return str(Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Cloud SDK" / "google-cloud-sdk" / "bin" / "gcloud.cmd")


def find_login_config() -> Path | None:
    for pattern in LOGIN_CONFIG_CANDIDATES:
        for match in glob.glob(os.path.expanduser(pattern)):
            path = Path(match)
            if path.is_file():
                return path
    return None


def adc_info() -> dict:
    path = Path(os.environ.get("APPDATA", "")) / "gcloud" / "application_default_credentials.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:  # noqa: BLE001
        return {}
    return {
        "path": str(path),
        "type": data.get("type"),
        "quota_project": data.get("quota_project_id"),
        "has_refresh_token": bool(data.get("refresh_token")),
    }


def antigravity_wif() -> dict:
    path = Path(os.environ.get("USERPROFILE", "")) / ".gemini" / "jetski-standalone-oauth-token"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:  # noqa: BLE001
        return {}
    return {"path": str(path), "token_present": bool(data.get("token")), "saved_wif_provider": data.get("saved_wif_provider")}


def cmd_status(_args) -> int:
    print("=== gcloud ===")
    gcloud = gcloud_path()
    print(f"path             : {gcloud}")
    print(f"exists           : {Path(gcloud).exists()}")
    try:
        result = subprocess.run(
            [gcloud, "config", "list", "--format=value(core.account,core.project)"],
            capture_output=True,
            text=True,
            timeout=90,
        )
        values = [v for v in (result.stdout or "").strip().split("\t") if v]
        print(f"signed-in account: {values[0] if values else '(none)'}")
        print(f"active project   : {values[1] if len(values) > 1 else '(none)'}")
    except Exception as exc:  # noqa: BLE001
        print(f"cannot query gcloud: {type(exc).__name__}: {str(exc)[:120]}")
        print("  (a sandboxed shell blocks gcloud's writes; run this in your own terminal)")

    print("\n=== application default credentials ===")
    info = adc_info()
    for key in ("path", "type", "quota_project", "has_refresh_token"):
        print(f"{key:<17}: {info.get(key)}")

    print("\n=== Antigravity WIF token ===")
    wif = antigravity_wif()
    if not wif:
        print("not found")
    else:
        print(f"path             : {wif['path']}")
        print(f"token present    : {wif['token_present']}")
        print(f"saved provider   : {wif['saved_wif_provider']}")
        if not wif["token_present"]:
            print("  -> configuration only; Antigravity has not minted a token, so nothing here can")
            print("     be reused until you sign in to Antigravity (or use option A/B below).")

    print("\n=== workforce login config ===")
    config = find_login_config()
    print(f"found            : {config if config else '(none)'}")
    if not config:
        print("  -> needed for a terminal WIF sign-in. Get it from the Cloud console:")
        print("     IAM & Admin > Workforce Identity Federation > your pool > provider >")
        print("     'Download login configuration', or ask your GCP admin.")

    print("\n=== expected audience ===")
    print(f"//iam.googleapis.com/locations/global/workforcePools/{POOL}/providers/{PROVIDER}")
    return 0


def cmd_login(_args) -> int:
    gcloud = f'& "{gcloud_path()}"'
    config = find_login_config()
    print("Run ONE of these in your own terminal (not a sandboxed shell):\n")
    if config:
        print("Option A -- with the workforce login configuration file (already found on this machine):\n")
        print(f"  {gcloud} auth login --login-config=\"{config}\" --update-adc\n")
    else:
        print("Option A -- with the workforce login configuration file (not found here yet; download it")
        print("from the Cloud console as described by `status`):\n")
        print(f"  {gcloud} auth login --login-config=<path-to-login-config.json> --update-adc\n")
    print("Option B -- set the audience explicitly, then sign in with the browser SAML flow:\n")
    print(f"  {gcloud} config set auth/workforce_pool_audience \"//iam.googleapis.com/locations/global/workforcePools/{POOL}/providers/{PROVIDER}\"")
    print(f"  {gcloud} auth login --workforce\n")
    print("Then confirm:\n")
    print("  python scripts/wif_login.py verify\n")
    print("Notes:")
    print("  * --update-adc is what makes Myriaxon (and any google-auth based tool) pick it up.")
    print("  * Browsers on managed machines often block the localhost redirect. If the flow hangs,")
    print("    use --no-launch-browser and paste the printed URL into your work browser.")
    print("  * gcloud needs write access to %APPDATA%\\gcloud. A sandboxed shell will fail with")
    print("    WinError 5; that is the shell, not your credentials.")
    return 0


def cmd_verify(_args) -> int:
    print("confirming the current credential can reach Gemini on Vertex...\n")
    adc = adc_info()
    if not adc:
        print("no application default credentials found -- run the login first")
        return 1
    print(f"credential type: {adc.get('type')}  quota project: {adc.get('quota_project')}")

    import warnings

    warnings.filterwarnings("ignore")
    try:
        import google.auth
        import google.auth.transport.requests

        creds, project = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        creds.refresh(google.auth.transport.requests.Request())
        print(f"token refreshed: yes (project {project})")
    except Exception as exc:  # noqa: BLE001
        print(f"token refresh FAILED: {type(exc).__name__}: {str(exc)[:200]}")
        return 1

    try:
        import google.genai as genai

        client = genai.Client(vertexai=True, project=project or PROJECT_FALLBACK, location="global")
        response = client.models.generate_content(model="gemini-3.8-flash", contents="Reply with exactly: pong")
        print(f"gemini-3.8-flash: {response.text!r}")
    except Exception as exc:  # noqa: BLE001
        print(f"gemini-3.8-flash FAILED: {type(exc).__name__}: {str(exc)[:200]}")
        return 1

    print("\nOK -- Myriaxon can serve gemini-3.8-flash through this credential.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["status", "login", "verify"], nargs="?", default="status")
    args = parser.parse_args()
    return {"status": cmd_status, "login": cmd_login, "verify": cmd_verify}[args.action](args)


if __name__ == "__main__":
    raise SystemExit(main())
