#!/usr/bin/env python3
"""Extract every localisable string from a level file, as a map a translator fills in.

This produces the input for `tools/apply_localisation.py`: a JSON object from key path to the **English**
text, with the `Id` field names left for the tool to derive. A translator replaces each value with
Indonesian, and the tool then merges it.

Splitting extraction from translation matters for a mechanical reason. The alternative -- translating inside a
copy of the level file -- means a translator who mistypes one JSON key corrupts a question, and the failure is
silent because the file still parses. Here a bad key is refused by the tool, and a wrong value cannot touch
anything the guard does not check.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from apply_localisation import localisable_paths, resolve  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Extract localisable strings from a level file.")
    parser.add_argument("level", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args()

    data = json.loads(args.level.read_text(encoding="utf-8"))
    extracted: dict[str, str] = {}
    for path in localisable_paths(data):
        container, key = resolve(data, path)
        extracted[path] = container[key]

    text = json.dumps(extracted, ensure_ascii=False, indent=2 if args.pretty else None)
    if args.output:
        args.output.write_text(text + "\n", encoding="utf-8", newline="\n")
        print(f"  {args.level.name}: {len(extracted)} string(s) -> {args.output}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
