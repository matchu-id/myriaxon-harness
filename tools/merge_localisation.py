#!/usr/bin/env python3
"""Merge the Indonesian `Id` fields into the level files the seeder reads.

`apply_localisation.py` writes a separate `NN.id.json`, which is right for review -- the English source stays
pristine and a reviewer can diff the pair. It is wrong for seeding. `build-seed.ts` reads
`tracks/<slug>/levels/NN.json` and takes `q.promptId`, `opt.textId` and `caseStudy.titleId` from that same
object, so a level whose `Id` fields sit in a sibling file seeds as English-only. Staging that produced a
container reporting `{"q":500,"withId":0}` -- 500 questions, none localised -- which is exactly the failure the
count was added to catch.

This walks the two files in parallel and copies every `Id` value from the translated file into the English one.
It is deliberately written as one straight pass rather than a pair-building abstraction, because the first two
attempts at that abstraction both returned the wrong container and the indirection bought nothing.

English-only originals are copied to `levels-english-only/` first: the merge is not reversible from the merged
file, and `/srv/certivo` has no version control.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(r"C:\Users\arian\OneDrive\ドキュメント\Myriaxon")
LEVELS = ROOT / "certivo-rework" / "data-scientist" / "levels"
PRISTINE = ROOT / "certivo-rework" / "data-scientist" / "levels-english-only"

SIMPLE = {"title": "titleId", "scenarioMarkdown": "scenarioMarkdownId",
          "prompt": "promptId", "explanation": "explanationId", "text": "textId",
          "instructionsMarkdown": "instructionsMarkdownId", "rubricMarkdown": "rubricMarkdownId"}


def copy_ids(source: dict, target: dict, label: str, count: list[int]) -> None:
    """Copy every Id field from `source` into `target`, in place.

    The Id test is `key.endswith("Id")`, and the first version got it inside-out: it asked whether the key was
    one of the *Id names* (`key in SIMPLE.values()`) while iterating the translated file, where the keys are
    the *English* names. Nothing matched, so the merge reported `0 field(s) merged` across all 20 levels and
    wrote the files back unchanged -- a silent no-op that looked like success.
    """
    for key, value in source.items():
        if key.endswith("Id") and key[:-2] in SIMPLE:
            if key not in target:
                target[key] = value
                count[0] += 1
            continue
        if isinstance(value, list) and isinstance(target.get(key), list):
            for index, item in enumerate(value):
                if index < len(target[key]) and isinstance(item, dict):
                    copy_ids(item, target[key][index], f"{label}.{key}[{index}]", count)
        elif isinstance(value, dict) and isinstance(target.get(key), dict):
            copy_ids(value, target[key], f"{label}.{key}", count)


def main() -> int:
    parser = argparse.ArgumentParser(description="Merge Id fields into the seeder's level files.")
    parser.add_argument("--track", default="data-scientist")
    args = parser.parse_args()

    levels = ROOT / "certivo-rework" / args.track / "levels"
    pristine = ROOT / "certivo-rework" / args.track / "levels-english-only"
    english_files = sorted(p for p in levels.glob("*.json") if not p.name.endswith(".id.json"))
    if not english_files:
        print(f"  no English level files under {levels}")
        return 1

    pristine.mkdir(parents=True, exist_ok=True)
    total_fields = 0
    merged_levels = 0

    for english_path in english_files:
        translated_path = levels / f"{english_path.stem}.id.json"
        if not translated_path.exists():
            print(f"  {english_path.name}: no translation yet, left alone")
            continue

        original = json.loads(english_path.read_text(encoding="utf-8"))
        translated = json.loads(translated_path.read_text(encoding="utf-8"))

        # Preserve the English-only copy once, so re-running does not overwrite it with a merged file.
        backup = pristine / english_path.name
        if not backup.exists():
            shutil.copy2(english_path, backup)

        count = [0]
        copy_ids(translated, original, english_path.stem, count)
        total_fields += count[0]
        merged_levels += 1

        english_path.write_text(json.dumps(original, indent=2, ensure_ascii=False) + "\n",
                                encoding="utf-8", newline="\n")
        print(f"  {english_path.name}: {count[0]} Id field(s) merged")

    print()
    print(f"  {total_fields} field(s) merged into {merged_levels} level file(s)")
    print(f"  English-only originals: {pristine}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
