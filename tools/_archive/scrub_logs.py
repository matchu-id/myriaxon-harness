"""Strip live credentials out of Harbor's trial logs.

The first version of the Myriaxon adapter wrote the model key into the container's config with a
shell command, and Harbor logs every command it runs -- so a real `sk_live_...` key ended up verbatim
in `trial.log` and possibly `exception.txt`. The adapter is fixed (the config is now uploaded as a
file), but the logs already written still contain the credential, and rotating the key does not
remove a copy that is sitting on disk.

This rewrites the affected files in place, replacing the key value with a marker, and reports exactly
what it changed. It refuses to guess: it looks for the shapes a credential actually takes
(`"api_key": "..."`, `sk-...`, `sk_live_...`) rather than redacting anything that looks vaguely
secret, because over-redacting would destroy the logs' diagnostic value.

Usage:
    python tools/scrub_logs.py            # report only
    python tools/scrub_logs.py --apply    # rewrite the files
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Directories that can hold a logged command: Harbor's job artifacts, and the detached run logs.
SEARCH_ROOTS = (
    REPO / "jobs",
    REPO / ".uv" / "tb-logs",
)

MARKER = "***REDACTED***"

# Ordered most specific first. The json form is what the adapter produced; the bare prefixed forms
# catch a key that was echoed or logged outside the json.
PATTERNS = (
    re.compile(r'("api_key"\s*:\s*")([^"]{8,})(")'),
    re.compile(r'\b(sk_live_[A-Za-z0-9_\-]{8,})'),
    re.compile(r'\b(sk-[A-Za-z0-9_\-]{16,})'),
    re.compile(r'\b(dtn_[A-Za-z0-9]{16,})'),
)


def redact(text: str) -> tuple[str, int]:
    """Return (redacted, count). Counts replacements so the report is evidence, not a claim."""
    total = 0
    for pattern in PATTERNS:
        def _sub(match: re.Match) -> str:
            nonlocal total
            total += 1
            if match.re.groups == 3:
                return f"{match.group(1)}{MARKER}{match.group(3)}"
            return MARKER

        text = pattern.sub(_sub, text)
    return text, total


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="rewrite the files; otherwise report only")
    args = parser.parse_args()

    scanned = 0
    affected: list[tuple[Path, int]] = []

    for root in SEARCH_ROOTS:
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if path.suffix.lower() not in (".log", ".txt", ".json", ".jsonl", ".yaml", ".toml"):
                continue
            scanned += 1
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                continue
            cleaned, count = redact(text)
            if count:
                affected.append((path, count))
                if args.apply:
                    try:
                        path.write_text(cleaned, encoding="utf-8", newline="")
                    except Exception as exc:  # noqa: BLE001
                        print(f"  FAILED to rewrite {path}: {type(exc).__name__}: {exc}")

    print(f"  scanned {scanned} file(s) under {', '.join(str(r.relative_to(REPO)) for r in SEARCH_ROOTS)}")
    if not affected:
        print("  no credentials found")
        return 0

    print(f"  {'rewrote' if args.apply else 'would rewrite'} {len(affected)} file(s):")
    for path, count in affected:
        print(f"    {count:>3} occurrence(s)  {path.relative_to(REPO)}")

    if not args.apply:
        print("\n  run again with --apply to rewrite them")
    else:
        print("\n  done -- verify with:  python tools/scrub_logs.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
