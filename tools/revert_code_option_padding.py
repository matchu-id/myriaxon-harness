#!/usr/bin/env python3
"""Find option texts a translator altered when the English option was nothing but code.

The guard originally rejected a localised value identical to its English source, which is right for prose and
wrong for code. A translator satisfied it by appending Indonesian text inside the code span, producing options
such as `products.map((product) => product.name) -- satu elemen per produk` -- invalid JavaScript offered as an
answer choice. The guard is fixed, so those edits should be reverted.

This finds them independently of any agent's report, because a reversion that was only partly done would look
like progress. It compares each option against its English source and flags any option that

  * the source had as pure code -- nothing outside the backticks -- and
  * the translation has changed at all.

For those, any Indonesian added is by definition inside the code, since there was nowhere else to put it. An
option that reads `` `#price`, because an id outranks ... `` is *not* flagged: the English already carried
prose after the code, so translating it is correct.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(r"C:\Users\arian\OneDrive\ドキュメント\Myriaxon")
TRACKS = ("ai-ml-engineer", "fullstack-developer", "data-scientist")


def prose_outside_code(text: str) -> str:
    """Everything that is not inside a backticked span."""
    out: list[str] = []
    depth = 0
    for char in text:
        if char == "`":
            depth = 1 - depth
            continue
        if depth == 0:
            out.append(char)
    # Punctuation and whitespace do not count as prose; a comma joining code to a clause does not either.
    return "".join(ch for ch in out if ch.isalpha())


def main() -> int:
    parser = argparse.ArgumentParser(description="Find Indonesian text inserted inside code spans.")
    parser.add_argument("--fix", action="store_true", help="revert them to the English text")
    args = parser.parse_args()

    flagged: list[tuple[Path, int, int, str, str]] = []
    for track in TRACKS:
        levels = ROOT / "certivo-rework" / track / "levels"
        for produced in sorted(levels.glob("*.id.json")):
            source = levels / produced.name.replace(".id.json", ".json")
            if not source.exists():
                continue
            original = json.loads(source.read_text(encoding="utf-8"))
            translated = json.loads(produced.read_text(encoding="utf-8"))
            for q_index, question in enumerate(translated.get("questions", [])):
                for o_index, option in enumerate(question.get("options", [])):
                    english = original["questions"][q_index]["options"][o_index]["text"]
                    current = option["textId"]
                    if current == english:
                        continue
                    if "`" not in english:
                        continue
                    if prose_outside_code(english):
                        continue
                    flagged.append((produced, q_index, o_index, english, current))

    if not flagged:
        print("  no code-only options have been altered")
        return 0

    print(f"  {len(flagged)} code-only option(s) altered:")
    for path, q_index, o_index, english, current in flagged:
        print(f"    {path.parent.parent.name}/{path.name} questions.{q_index}.options.{o_index}")
        print(f"      english: {english[:96]}")
        print(f"      current: {current[:96]}")

    if not args.fix:
        print()
        print("  pass --fix to revert these to the English text")
        return 1

    by_file: dict[Path, list[tuple[int, int, str]]] = {}
    for path, q_index, o_index, english, _ in flagged:
        by_file.setdefault(path, []).append((q_index, o_index, english))
    for path, edits in by_file.items():
        data = json.loads(path.read_text(encoding="utf-8"))
        for q_index, o_index, english in edits:
            data["questions"][q_index]["options"][o_index]["textId"] = english
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                        encoding="utf-8", newline="\n")
        print(f"  reverted {len(edits)} option(s) in {path.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
