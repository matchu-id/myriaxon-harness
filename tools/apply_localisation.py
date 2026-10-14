#!/usr/bin/env python3
"""Add Indonesian `Id` fields to a level file, from a translations map.

Generalised from the one-off generator written for `ai-ml-engineer/levels/01.json`, which proved the approach
on 140 strings. The pattern is worth keeping because it makes the translation reviewable: the translations sit
in a plain map from JSON key path to Indonesian text, so a reviewer reads Indonesian prose rather than a diff,
and the merge itself is mechanical enough to be trusted.

    python tools/apply_localisation.py <level.json> <translations.json> [--write]

`translations.json` maps a key path to the Indonesian string:

    {
      "caseStudy.title": "Harlow Outdoors: Sebelum Model Pertama",
      "questions.0.prompt": "...",
      "questions.0.options.0.text": "...",
      "questions.0.code.instructionsMarkdown": "..."
    }

Paths name the **English** field; the tool inserts the `Id` counterpart immediately after it, so the JSON
still reads source-then-translation. Every named path must exist, and every localisable field must be covered
unless `--allow-partial` is passed -- a silent omission would otherwise look like a complete pass.

Nothing outside the `Id` fields is written. `tools/guard_localisation.py` then proves that by stripping them
and requiring the result to equal the original.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# English field -> the Indonesian field inserted directly after it.
ID_FOR = {
    "title": "titleId",
    "scenarioMarkdown": "scenarioMarkdownId",
    "prompt": "promptId",
    "explanation": "explanationId",
    "text": "textId",
    "instructionsMarkdown": "instructionsMarkdownId",
    "rubricMarkdown": "rubricMarkdownId",
}


def localisable_paths(data: dict) -> list[str]:
    """Every field in the level that should carry an Indonesian counterpart."""
    paths: list[str] = []
    case = data.get("caseStudy", {})
    for key in ("title", "scenarioMarkdown"):
        if key in case:
            paths.append(f"caseStudy.{key}")
    for index, question in enumerate(data.get("questions", [])):
        base = f"questions.{index}"
        for key in ("prompt", "explanation"):
            if key in question:
                paths.append(f"{base}.{key}")
        for option_index, option in enumerate(question.get("options", [])):
            if "text" in option:
                paths.append(f"{base}.options.{option_index}.text")
        if "instructionsMarkdown" in question.get("code", {}):
            paths.append(f"{base}.code.instructionsMarkdown")
        if "rubricMarkdown" in question.get("essay", {}):
            paths.append(f"{base}.essay.rubricMarkdown")
    return paths


def resolve(data: dict, path: str) -> tuple[dict, str]:
    """The container and final key for a dotted path, with list indices as integers."""
    parts: list[str | int] = []
    for part in path.split("."):
        parts.append(int(part) if part.isdigit() else part)
    node = data
    for part in parts[:-1]:
        node = node[part]
    return node, str(parts[-1])


def insert_after(container: dict, key: str, new_key: str, value: str) -> None:
    """Rebuild the dict with `new_key` placed directly after `key`, so key order still reads source-then-translation."""
    rebuilt = {}
    for existing, existing_value in container.items():
        rebuilt[existing] = existing_value
        if existing == key:
            rebuilt[new_key] = value
    if new_key not in rebuilt:
        raise KeyError(f"{key} not present in its container")
    container.clear()
    container.update(rebuilt)


def has_id(data: dict, path: str) -> bool:
    """Whether a localisable path already carries its Indonesian counterpart."""
    id_key = ID_FOR.get(path.split(".")[-1])
    if not id_key:
        return False
    try:
        container, key = resolve(data, path)
    except (KeyError, IndexError, TypeError):
        return False
    return id_key in container


def main() -> int:
    parser = argparse.ArgumentParser(description="Add Indonesian Id fields to a level file.")
    parser.add_argument("level", type=Path)
    parser.add_argument("translations", type=Path)
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--allow-partial", action="store_true")
    parser.add_argument("--in-place", action="store_true",
                        help="write the Id fields into the level file itself instead of a .id.json copy")
    args = parser.parse_args()

    original_text = args.level.read_text(encoding="utf-8")
    data = json.loads(original_text)
    translations = json.loads(args.translations.read_text(encoding="utf-8"))

    if args.in_place:
        # Adding an Id field beside an existing one would leave the file with two, so in-place is only for a
        # level that has none. This check exists because a stray untranslated `01.id.json` -- a copy of the
        # English with Id fields added -- sat in the tree looking like finished work.
        already = [path for path in localisable_paths(data) if has_id(data, path)]
        if already:
            print(f"  REFUSING: {args.level.name} already has {len(already)} Id field(s)")
            print("    remove them first, or translate the level's .json and let this write a fresh copy")
            return 1

    # Every localisable field must be covered, or the omissions are invisible in the output.
    expected = set(localisable_paths(data))
    provided = set(translations)
    missing = sorted(expected - provided)
    unknown = sorted(provided - expected)
    if unknown:
        print(f"  REFUSING: {len(unknown)} path(s) do not exist in the level:")
        for path in unknown[:10]:
            print(f"    {path}")
        return 1
    if missing and not args.allow_partial:
        print(f"  REFUSING: {len(missing)} localisable field(s) have no translation:")
        for path in missing[:10]:
            print(f"    {path}")
        if len(missing) > 10:
            print(f"    ... and {len(missing) - 10} more")
        return 1

    # Apply in reverse document order so an insertion never shifts a later path's indices.
    applied = 0
    for path in sorted(provided, key=lambda p: [int(x) if x.isdigit() else x for x in p.split(".")], reverse=True):
        english_key = path.split(".")[-1]
        id_key = ID_FOR.get(english_key)
        if not id_key:
            print(f"  REFUSING: {path} does not name a localisable field")
            return 1
        container, key = resolve(data, path)
        value = translations[path]
        if not isinstance(value, str) or not value.strip():
            print(f"  REFUSING: {path} is empty")
            return 1
        insert_after(container, key, id_key, value)
        applied += 1

    output = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    destination = args.level if args.in_place else args.level.with_name(
        args.level.stem + ".id" + args.level.suffix)
    print(f"  {args.level.name}: applied {applied} translation(s) -> {destination.name}")
    if missing:
        print(f"  {len(missing)} field(s) left in English (allowed by --allow-partial)")
    if not args.write:
        print("  dry run: nothing written (pass --write)")
        return 0
    destination.write_text(output, encoding="utf-8", newline="\n")
    print(f"  wrote {len(output.encode('utf-8'))} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
