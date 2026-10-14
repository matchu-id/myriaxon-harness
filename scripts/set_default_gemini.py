"""Make Gemini 3.8 Flash on Vertex the default provider, by writing ~/.myriaxon/config.json.

    python scripts/set_default_gemini.py            # use the project from your credential
    python scripts/set_default_gemini.py --project X --location global --model gemini-3.8-flash

Run this after `gcloud auth application-default login --update-adc`-style sign-in. It exists
because the interactive `myriaxon auth` wizard is six prompts for a setup that is fully derivable
from the machine's own credential, and getting one of them wrong (a region instead of `global`)
produces a 404 that looks like a broken model.

A backup of the previous config is written next to it, so this is reversible with a file copy.
"""
import argparse
import json
import os
import shutil
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
    parser.add_argument("--project", default="")
    parser.add_argument("--location", default="global")
    parser.add_argument("--model", default="gemini-3.8-flash")
    parser.add_argument("--dry-run", action="store_true", help="show what would be written, change nothing")
    args = parser.parse_args()

    # Resolve the config path from the same constant the CLI uses, so this script cannot disagree
    # with Myriaxon about where the config lives -- including when MYRIAXON_CONFIG_DIR redirects it.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from myriaxon.constants import CONFIG_FILE

    config_path = CONFIG_FILE

    project = args.project
    if not project:
        # Derive it from the credential rather than asking, so the common case is one command.
        try:
            import warnings

            warnings.filterwarnings("ignore")
            import google.auth

            _, detected = google.auth.default()
            project = detected or ""
        except Exception as exc:  # noqa: BLE001
            print(f"could not read the project from your credential: {type(exc).__name__}: {exc}")
            print("pass --project explicitly.")
            return 2
    if not project:
        print("no project detected and none given; pass --project.")
        return 2

    existing: dict = {}
    if config_path.exists():
        try:
            existing = json.loads(config_path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"existing config unreadable ({exc}); it will be replaced (a .bak copy is written).")
            existing = {}

    updated = dict(existing)
    updated.update(
        {
            "provider": "gemini",
            "model": args.model,
            # Vertex ignores base_url entirely, but the field must not be blank for the validator.
            "base_url": "https://generativelanguage.googleapis.com/",
            # Blank on purpose: Vertex authenticates with your gcloud credential (ADC), never an API
            # key. A key left here from another provider is what produced the
            # "API keys are not supported by this API" failure.
            "api_key": "",
            "vertex_project": project,
            # `global`, not a region: gemini-3.x is served only on the multi-region endpoint, and a
            # regional value turns a working model into a 404.
            "vertex_location": args.location,
        }
    )

    print("would write:" if args.dry_run else "writing:", config_path)
    for key in ("provider", "model", "vertex_project", "vertex_location", "api_key"):
        before = existing.get(key, "(unset)")
        after = updated.get(key, "")
        marker = "  " if before == after else "->"
        print(f"  {marker} {key:<16} {before!r} => {after!r}")

    if args.dry_run:
        return 0

    config_path.parent.mkdir(parents=True, exist_ok=True)

    # Backup is best-effort and MUST NOT be able to abort the write. Measured failure: shutil.copy
    # raised PermissionError creating config.json.bak (a restricted environment refuses to create a
    # new file in this directory), which killed the script *after* printing what it intended to do
    # and *before* writing anything -- so the user saw a traceback instead of the change they asked
    # for. The backup is a convenience; the write is the point.
    if config_path.exists():
        try:
            backup = config_path.with_suffix(".json.bak")
            backup.write_text(config_path.read_text(encoding="utf-8-sig"), encoding="utf-8")
            print(f"  backup           {backup}")
        except OSError as exc:
            print(f"  backup           skipped (could not write: {exc.__class__.__name__})")

    # Write the config. Two strategies, because the atomic one needs to CREATE a new file and some
    # managed/sandboxed home directories allow writing an existing file but not creating a new one
    # (measured: config.json.tmp -> PermissionError while config.json itself was writable).
    # Atomic replace is preferred -- it cannot leave a half-written config -- but never at the cost
    # of failing the operation entirely.
    payload = json.dumps(updated, indent=2)
    temp_path = config_path.with_suffix(".json.tmp")
    written_atomically = False
    try:
        temp_path.write_text(payload, encoding="utf-8")
        os.replace(temp_path, config_path)
        written_atomically = True
    except OSError:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        print("  note             atomic write unavailable (cannot create a temp file here)")
        try:
            config_path.write_text(payload, encoding="utf-8")
        except OSError as exc:
            print(f"\nFAILED to write {config_path}: {exc.__class__.__name__}: {exc}")
            print("Your existing configuration is unchanged.")
            return 1

    print(f"  wrote            {'(atomic replace)' if written_atomically else '(direct write)'}")
    print("\nDone. Verify with:")
    print("  python scripts/check_vertex.py")
    print("  python -m myriaxon")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
