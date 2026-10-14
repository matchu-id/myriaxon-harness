"""Set Myriaxon's default provider and model, persistently.

    python scripts/set_provider.py netra
    python scripts/set_provider.py netra --model deepseek/deepseek-v4.1-flash
    python scripts/set_provider.py gemini --vertex-project X --vertex-location global
    python scripts/set_provider.py --show

Replaces the previous Gemini-only helper. Exists because the interactive `myriaxon auth` wizard is
six prompts for a choice that is fully derivable from the machine (endpoint and model come from the
provider table, the API key from the environment), and because a partially-updated config is actively
harmful: switching only `provider` leaves the previous provider's endpoint and model in place, which
sends requests to the wrong host and returns a 404 that looks like a broken model.
"""
import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from myriaxon.dotenv_support import load_dotenv_into_environ  # noqa: E402

load_dotenv_into_environ()

from myriaxon.constants import (  # noqa: E402
    CONFIG_FILE,
    DEFAULT_BASE_URL,
    DEFAULT_DEEPSEEK_BASE_URL,
    DEFAULT_DEEPSEEK_MODEL,
    DEFAULT_GEMINI_BASE_URL,
    DEFAULT_GEMINI_MODEL,
    DEFAULT_MODEL,
    DEFAULT_NETRA_BASE_URL,
    DEFAULT_NETRA_MODEL,
    DEFAULT_OPENROUTER_BASE_URL,
    DEEPSEEK_API_KEY_ENV_FALLBACK,
    GEMINI_API_KEY_ENV_FALLBACK,
    NETRA_API_KEY_ENV_FALLBACK,
    OPENROUTER_API_KEY_ENV_FALLBACK,
    PROVIDER_ANTHROPIC,
    PROVIDER_DEEPSEEK,
    PROVIDER_GEMINI,
    PROVIDER_NETRA,
    PROVIDER_OPENAI_COMPAT,
    PROVIDER_OPENROUTER,
)

# provider -> (base_url, default model, api key env var). One table, so a provider cannot be half
# registered and every `set_provider` call writes a complete, self-consistent config.
PROVIDERS = {
    PROVIDER_NETRA: (DEFAULT_NETRA_BASE_URL, DEFAULT_NETRA_MODEL, NETRA_API_KEY_ENV_FALLBACK),
    PROVIDER_GEMINI: (DEFAULT_GEMINI_BASE_URL, DEFAULT_GEMINI_MODEL, GEMINI_API_KEY_ENV_FALLBACK),
    PROVIDER_DEEPSEEK: (DEFAULT_DEEPSEEK_BASE_URL, DEFAULT_DEEPSEEK_MODEL, DEEPSEEK_API_KEY_ENV_FALLBACK),
    PROVIDER_OPENROUTER: (DEFAULT_OPENROUTER_BASE_URL, "openai/gpt-oss-120b", OPENROUTER_API_KEY_ENV_FALLBACK),
    PROVIDER_ANTHROPIC: (DEFAULT_BASE_URL, DEFAULT_MODEL, "ANTHROPIC_API_KEY"),
    PROVIDER_OPENAI_COMPAT: ("", "", "MYRIAXON_API_KEY"),
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("provider", nargs="?", choices=sorted(PROVIDERS))
    parser.add_argument("--model", default="")
    parser.add_argument("--base-url", default="")
    parser.add_argument("--vertex-project", default="")
    parser.add_argument("--vertex-location", default="global")
    parser.add_argument("--show", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.show or not args.provider:
        print(f"config file: {CONFIG_FILE}")
        if CONFIG_FILE.exists():
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8-sig"))
            for key in ("provider", "model", "base_url", "vertex_project", "vertex_location", "api_key"):
                value = data.get(key)
                shown = value if key != "api_key" else ("(set, %d chars)" % len(value) if value else "(empty)")
                print(f"  {key:<16} {shown}")
        else:
            print("  (no config yet)")
        print("\nusage: python scripts/set_provider.py <provider> [--model X]")
        return 0

    base_url, default_model, key_env = PROVIDERS[args.provider]
    model = args.model or default_model
    base_url = args.base_url or base_url
    if not base_url or not model:
        print(f"{args.provider} has no default endpoint/model; pass --base-url and --model.")
        return 2

    existing: dict = {}
    if CONFIG_FILE.exists():
        try:
            existing = json.loads(CONFIG_FILE.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            existing = {}

    updated = dict(existing)
    updated.update({"provider": args.provider, "model": model, "base_url": base_url})

    if args.provider == PROVIDER_GEMINI:
        project = args.vertex_project or existing.get("vertex_project") or ""
        if not project:
            try:
                import warnings

                warnings.filterwarnings("ignore")
                import google.auth

                _, project = google.auth.default()
                project = project or ""
            except Exception:  # noqa: BLE001
                project = ""
        updated["vertex_project"] = project
        updated["vertex_location"] = args.vertex_location
        # Vertex authenticates with OAuth/ADC, never an API key -- a leftover key from another
        # provider makes the SDK prefer key auth and fail with "API keys are not supported".
        updated["api_key"] = ""
        note = f"vertex project={project or '(none detected)'} location={args.vertex_location}"
    else:
        # Switching away from Gemini must clear its Vertex fields, or a later Gemini run resumes
        # against a stale project and a non-Gemini run carries meaningless state.
        updated.pop("vertex_project", None)
        updated.pop("vertex_location", None)
        key = os.environ.get(key_env, "")
        updated["api_key"] = key
        note = f"key from ${key_env}: {'found (%d chars)' % len(key) if key else 'NOT SET'}"

    print(f"config file: {CONFIG_FILE}")
    for key in ("provider", "model", "base_url", "vertex_project", "vertex_location", "api_key"):
        if key in updated or key in existing:
            before = existing.get(key, "(unset)")
            after = updated.get(key, "(removed)")
            if key == "api_key":
                before = "(set)" if before not in ("(unset)", "", None) else "(empty)"
                after = "(set)" if after not in ("(removed)", "", None) else "(empty)"
            marker = "  " if before == after else "->"
            print(f"  {marker} {key:<16} {before} => {after}")
    print(f"  note: {note}")

    if args.dry_run:
        return 0

    payload = json.dumps(updated, indent=2)
    temp = CONFIG_FILE.with_suffix(".json.tmp")
    try:
        temp.write_text(payload, encoding="utf-8")
        os.replace(temp, CONFIG_FILE)
        how = "atomic replace"
    except OSError:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass
        try:
            CONFIG_FILE.write_text(payload, encoding="utf-8")
            how = "direct write"
        except OSError as exc:
            print(f"\nFAILED to write {CONFIG_FILE}: {exc.__class__.__name__}: {exc}")
            print("Your existing configuration is unchanged.")
            return 1
    print(f"  wrote ({how})")
    print(f"\nDefault is now {args.provider}:{model}")
    print("Run it with:  python -m myriaxon        (or myriaxon.cmd)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
