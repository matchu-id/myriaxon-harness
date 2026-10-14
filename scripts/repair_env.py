"""Repair an .env where two assignments ended up on one line.

    python scripts/repair_env.py --dry-run
    python scripts/repair_env.py

Why this exists: appending to a file that lacks a trailing newline concatenates onto the last line,
turning `OPENROUTER_MAN_KEY=<value>NETRA_API_KEY=<key>` into one malformed entry. Both keys then
fail to load. This script splits such a line back into its separate assignments.

Verification is built in and is the point of the script: repairing a credentials file without
proving both keys still load would be worse than leaving it alone.
"""
import argparse
import json
import os
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ENV_FILE = REPO / ".env"

# A later `KEY=` appearing inside a value is the signature of the concatenation bug. Known key names
# are matched specifically so a legitimate value containing "NAME=" cannot be split by accident.
KEY_RE = re.compile(r"(GEMINI_API_KEY|OPENROUTER_API_KEY|OPENROUTER_MAN_KEY|DEEPSEEK_API_KEY|NEOSANTAARA_API_KEY|NEOSANTARA_API_KEY|NETRA_API_KEY)=")


def mask(value: str) -> str:
    return value if len(value) <= 12 else f"{value[:8]}...({len(value)})"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not ENV_FILE.exists():
        print(f"no .env at {ENV_FILE}")
        return 2

    original = ENV_FILE.read_text(encoding="utf-8")
    trailing_newline = original.endswith("\n")
    lines = original.splitlines()

    repaired: list[str] = []
    changes = 0
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            repaired.append(line)
            continue
        matches = list(KEY_RE.finditer(stripped))
        if len(matches) <= 1:
            repaired.append(line)
            continue
        # More than one assignment on this line: split at each subsequent key start.
        print(f"  splitting: {mask(stripped[:40])} ... into {len(matches)} entries")
        for index, match in enumerate(matches):
            start = match.start()
            end = matches[index + 1].start() if index + 1 < len(matches) else len(stripped)
            repaired.append(stripped[start:end].strip())
            changes += 1

    if not changes:
        print("nothing to repair -- no concatenated assignments found")
        return 0

    print(f"\nwould produce {len(repaired)} lines:")
    for line in repaired:
        if "=" in line:
            key, _, value = line.partition("=")
            print(f"  {key}={mask(value)}")
        else:
            print(f"  {line}")

    if args.dry_run:
        return 0

    backup = ENV_FILE.with_suffix(".env.bak")
    try:
        backup.write_text(original, encoding="utf-8")
        print(f"\nbackup: {backup}")
    except OSError as exc:
        print(f"\nbackup skipped ({exc.__class__.__name__}) -- continuing; the fix is deterministic")
    # Always terminate the last line: the original bug is caused by exactly this being absent.
    ENV_FILE.write_text("\n".join(repaired) + "\n", encoding="utf-8")
    print(f"repaired {ENV_FILE} (trailing newline: {trailing_newline} -> True)")

    # Prove both keys still load, which is the entire point.
    print("\nverifying with the project's own dotenv loader:")
    from myriaxon.dotenv_support import load_dotenv_into_environ

    load_dotenv_into_environ()
    expected = [m.group(1) for line in repaired for m in [KEY_RE.match(line)] if m]
    ok = True
    for key in expected:
        value = os.environ.get(key, "")
        status = "OK " if value else "MISSING"
        if not value:
            ok = False
        print(f"  [{status}] {key:<24} {mask(value)}")
    print("\nRESULT:", "all keys load" if ok else "SOME KEYS DID NOT LOAD")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
