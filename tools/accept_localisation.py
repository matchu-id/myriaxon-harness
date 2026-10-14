#!/usr/bin/env python3
"""Accept or reject the localisation rollout: verify all 60 files and report coverage per track.

`localisation_progress.py` counts and guards what exists. This is the acceptance gate, and it adds the three
checks that decide whether the rollout is actually finished rather than merely far along:

1. **Every level of every track has an `.id.json`**, not just most of them. A track at 19/20 is not localised;
   it is localised for nineteen levels and English for one, and nothing in the app would say so, because the
   missing field falls back to English silently.
2. **The guard passes on each one**, re-run here rather than taken from an agent's report.
3. **No localised value duplicates its English source.** A translation pass that copied strings through
   unchanged would satisfy the guard -- the structure is identical after stripping `Id` fields either way --
   so that failure mode needs its own check to be caught at all.

It exits non-zero when the rollout is incomplete, so it can be run repeatedly as the work lands.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(r"C:\Users\arian\OneDrive\ドキュメント\Myriaxon")
TRACKS = ("ai-ml-engineer", "fullstack-developer", "data-scientist")
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
GUARD = ROOT / "tools" / "guard_localisation.py"

# Short strings that are legitimately identical in both languages -- identifiers, acronyms, single terms
# whose Indonesian form is the same word. Compared case-insensitively and stripped of markdown punctuation.
IDENTICAL_IS_FINE = {
    "roc", "auc", "sql", "json", "html", "css", "api", "url", "http", "https", "csv", "id",
    "a", "b", "c", "d", "x", "y", "n", "1", "2", "3", "0",
}


def normalise(text: str) -> str:
    return "".join(ch for ch in text.lower() if ch.isalnum() or ch == " ").strip()


def strip_code(text: str) -> str:
    """Remove backticked spans, which are identifiers or code and must read the same in both languages."""
    out: list[str] = []
    depth = 0
    for char in text:
        if char == "`":
            depth = 1 - depth
            continue
        if depth == 0:
            out.append(char)
    return "".join(out)


def duplicates(original: dict, translated: dict) -> list[str]:
    """Localised fields that are identical to the English *and* carry no translated prose.

    A value made entirely of backticked code -- `` `SELECT * FROM products WHERE price < 50` `` -- is
    correctly identical in both languages. The guard was changed to exempt exactly that case after a
    translator, working around the stricter behaviour, produced `products.map((p) => p.name) -- satu elemen
    per produk`, which is not valid JavaScript and was being presented to learners as an answer choice.

    This mirrors the guard: strip the backticked spans, and only if translatable prose remains is an
    unchanged value a genuine miss. Two tools disagreeing about the same question is worse than either
    answer, so both now implement the same rule.
    """
    found: list[str] = []
    for path, value in walk(translated):
        counterpart = lookup(original, path)
        if counterpart is None or not isinstance(value, str):
            continue
        if value != counterpart:
            continue
        prose = normalise(strip_code(value))
        if len(prose) <= 3 or prose in IDENTICAL_IS_FINE:
            continue
        found.append(path)
    return found


def walk(node, prefix: str = ""):
    """Yield (path, value) for every Id field, with the Id suffix stripped from the path."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key.endswith("Id") and isinstance(value, str):
                yield (f"{prefix}.{key[:-2]}" if prefix else key[:-2]), value
            else:
                yield from walk(value, f"{prefix}.{key}" if prefix else key)
    elif isinstance(node, list):
        for index, item in enumerate(node):
            yield from walk(item, f"{prefix}.{index}" if prefix else str(index))


def lookup(node, path: str):
    for part in path.split("."):
        try:
            node = node[int(part)] if isinstance(node, list) else node[part]
        except (KeyError, IndexError, TypeError):
            return None
    return node


def main() -> int:
    incomplete: list[str] = []
    guard_failures: list[str] = []
    copied: list[str] = []
    verified = 0
    strings = 0

    for track in TRACKS:
        levels = ROOT / "certivo-rework" / track / "levels"
        sources = sorted(p for p in levels.glob("*.json") if not p.name.endswith(".id.json"))
        print(f"  {track} ({len(sources)} level(s))")
        track_strings = 0
        for source in sources:
            produced = source.with_name(source.stem + ".id.json")
            if not produced.exists():
                incomplete.append(f"{track}/{source.name}")
                continue
            run = subprocess.run(
                [str(PYTHON), str(GUARD), str(source), str(produced)],
                capture_output=True, text=True, encoding="utf-8", timeout=180,
            )
            if run.returncode != 0:
                guard_failures.append(f"{track}/{produced.name}")
                continue
            verified += 1
            for line in run.stdout.splitlines():
                if "string(s) have" in line:
                    track_strings += int(line.strip().split("/")[1].split(" ")[0])

            original = json.loads(source.read_text(encoding="utf-8"))
            translated = json.loads(produced.read_text(encoding="utf-8"))
            for path in duplicates(original, translated):
                copied.append(f"{track}/{produced.name}:{path}")

        strings += track_strings
        counted = sum(1 for p in levels.glob("*.id.json"))
        print(f"    {counted}/{len(sources)} file(s), {track_strings} string(s)")

    print()
    print(f"  {verified}/60 file(s) verified, {strings} string(s) covered")
    if incomplete:
        print(f"  NOT YET TRANSLATED: {len(incomplete)} file(s)")
        for name in incomplete[:8]:
            print(f"    {name}")
    if guard_failures:
        print(f"  GUARD FAILURES: {len(guard_failures)}")
        for name in guard_failures:
            print(f"    {name}")
    if copied:
        print(f"  UNTRANSLATED (identical to English): {len(copied)}")
        for name in copied[:10]:
            print(f"    {name}")

    if incomplete or guard_failures or copied:
        print()
        print("  ROLLOUT INCOMPLETE")
        return 1
    print()
    print("  ROLLOUT COMPLETE: every level localised, every file guarded, no untranslated strings")
    return 0


if __name__ == "__main__":
    sys.exit(main())
