#!/usr/bin/env python3
"""Read/write a localisation map as a line-oriented TSV, for translating by hand.

    python tools/loc.py export localisation/fullstack-developer-02.json localisation/work/02.tsv
    python tools/loc.py import localisation/fullstack-developer-02.json localisation/work/02.tsv

The map is one long JSON line, which is awkward to read and edit in a terminal. This writes each
`key<TAB>value` on its own line with control characters escaped (`\\n` -> literal backslash-n), so a
translator can read and rewrite values line by line. `import` reads that back, unescapes it, and asserts
that the key list is unchanged and byte-for-byte identical to the original before writing the JSON.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ESCAPES = {"\\": "\\\\", "\n": "\\n", "\r": "\\r", "\t": "\\t"}


def escape(value: str) -> str:
    out = value.replace("\\", "\\\\")
    for raw, encoded in (("\n", "\\n"), ("\r", "\\r"), ("\t", "\\t")):
        out = out.replace(raw, encoded)
    return out


def unescape(value: str) -> str:
    out: list[str] = []
    index = 0
    while index < len(value):
        char = value[index]
        if char == "\\" and index + 1 < len(value):
            nxt = value[index + 1]
            if nxt == "n":
                out.append("\n")
                index += 2
                continue
            if nxt == "r":
                out.append("\r")
                index += 2
                continue
            if nxt == "t":
                out.append("\t")
                index += 2
                continue
            if nxt == "\\":
                out.append("\\")
                index += 2
                continue
        out.append(char)
        index += 1
    return "".join(out)


def export(source: Path, work: Path) -> int:
    data = json.loads(source.read_text(encoding="utf-8"))
    body = "".join(f"{key}\t{escape(value)}\n" for key, value in data.items())
    work.parent.mkdir(parents=True, exist_ok=True)
    work.write_text(body, encoding="utf-8", newline="\n")
    print(f"  {source.name} -> {work.name}: {len(data)} value(s), {len(body.encode('utf-8'))} bytes")
    return 0


def import_(target: Path, work: Path) -> int:
    original = json.loads(target.read_text(encoding="utf-8"))
    lines = work.read_text(encoding="utf-8").split("\n")
    translated: dict[str, str] = dict(original)
    seen: set[str] = set()
    for line in lines:
        if not line.strip():
            continue
        if "\t" not in line:
            print(f"  REFUSING: line without a tab: {line[:80]!r}")
            return 1
        key, value = line.split("\t", 1)
        translated[key] = unescape(value)
        seen.add(key)
    if len(seen) != len(lines) - sum(1 for line in lines if not line.strip()):
        print("  REFUSING: duplicate keys in the work file")
        return 1

    if list(translated) != list(original):
        missing = [k for k in original if k not in translated]
        extra = [k for k in translated if k not in original]
        print(f"  REFUSING: keys changed (missing {len(missing)}, extra {len(extra)})")
        for key in missing[:10]:
            print(f"    missing: {key}")
        for key in extra[:10]:
            print(f"    extra: {key}")
        if not missing and not extra:
            print("    same keys, different order")
        return 1

    same = [k for k in original if original[k] == translated[k]]
    for key in same:
        print(f"  unchanged value: {key}")

    target.write_text(json.dumps(translated, indent=2, ensure_ascii=False) + "\n",
                      encoding="utf-8", newline="\n")
    print(f"  {target.name}: wrote {len(translated)} value(s), {sum(1 for k in original if original[k] != translated[k])} changed")
    return 0


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    action = sys.argv[1]
    if action == "export":
        return export(Path(sys.argv[2]), Path(sys.argv[3]))
    if action == "import":
        return import_(Path(sys.argv[2]), Path(sys.argv[3]))
    print(f"unknown action {action!r}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
