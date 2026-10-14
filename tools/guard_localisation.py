#!/usr/bin/env python3
"""Prove that a localisation pass changed nothing except the Indonesian fields.

Localisation adds `promptId`, `explanationId`, `textId`, `instructionsMarkdownId`,
`titleId`, `scenarioMarkdownId` and `rubricMarkdownId`. Everything else in a level file -- the tests, their
`expected` values, the `hidden` flags, which option is `correct`, the `setupSql`, the `starterCode`, the
skills -- has to survive untouched. A translation pass that silently altered one of those would corrupt
questions that took far longer to verify than to translate, and the damage would be invisible: the file
still parses and the audit still passes.

So this strips every `Id` field from the translated file and requires the result to equal the original
exactly. It reports the first divergence with a path, so a mismatch is diagnosable rather than merely
reported.

It also checks coverage in the other direction: every localisable string must have an Indonesian
counterpart, and no counterpart may be empty or identical to the English -- an untranslated copy is a
translation failure that would otherwise look like success.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

LOCALISED_KEYS = (
    "promptId",
    "explanationId",
    "textId",
    "instructionsMarkdownId",
    "titleId",
    "scenarioMarkdownId",
    "rubricMarkdownId",
)


def strip_localised(node: Any) -> Any:
    """Rebuild the structure with every Indonesian field removed."""
    if isinstance(node, dict):
        return {key: strip_localised(value) for key, value in node.items() if key not in LOCALISED_KEYS}
    if isinstance(node, list):
        return [strip_localised(item) for item in node]
    return node


def first_difference(a: Any, b: Any, path: str = "") -> str | None:
    """The path of the first structural difference, so a mismatch can be located."""
    if type(a) is not type(b):
        return f"{path or '<root>'}: {type(a).__name__} vs {type(b).__name__}"
    if isinstance(a, dict):
        for key in sorted(set(a) | set(b)):
            if key not in a:
                return f"{path}.{key}: missing from the original"
            if key not in b:
                return f"{path}.{key}: added by the translation"
            found = first_difference(a[key], b[key], f"{path}.{key}")
            if found:
                return found
        return None
    if isinstance(a, list):
        if len(a) != len(b):
            return f"{path}: {len(a)} items vs {len(b)} items"
        for index, (left, right) in enumerate(zip(a, b)):
            found = first_difference(left, right, f"{path}[{index}]")
            if found:
                return found
        return None
    if a != b:
        return f"{path}: {str(a)[:60]!r} vs {str(b)[:60]!r}"
    return None


def strip_code(text: str) -> str:
    """Remove backticked spans, which are code or identifiers and read the same in every language."""
    out: list[str] = []
    depth = 0
    for char in text:
        if char == "`":
            depth = 1 - depth
            continue
        if depth == 0:
            out.append(char)
    return "".join(out)


def coverage(data: dict) -> tuple[list[str], list[str], int]:
    """Every localisable string and whether it has a usable Indonesian counterpart."""
    missing: list[str] = []
    empty: list[str] = []
    checked = 0

    def check(label: str, english: Any, indonesian: Any) -> None:
        nonlocal checked
        checked += 1
        if indonesian is None:
            missing.append(label)
            return
        if not str(indonesian).strip():
            empty.append(label)
            return
        if str(indonesian).strip() != str(english).strip():
            return
        # Identical to the English. That is a failure for prose, which should have been translated, but not
        # for a value that is nothing but backticked code: an option that reads
        # `` `SELECT * FROM products WHERE price < 50` `` is SQL, and the correct Indonesian rendering is the
        # same SQL. Flagging it forced translators to bolt Indonesian prose onto code options to satisfy the
        # checker -- which is how one level ended up with `products.map((p) => p.name) -- satu elemen per
        # produk`, invalid JavaScript presented as an answer choice.
        if not strip_code(str(english)).strip():
            return
        empty.append(f"{label} (identical to English)")

    case = data.get("caseStudy", {})
    check("caseStudy.titleId", case.get("title"), case.get("titleId"))
    check("caseStudy.scenarioMarkdownId", case.get("scenarioMarkdown"), case.get("scenarioMarkdownId"))

    for index, question in enumerate(data.get("questions", []), start=1):
        tag = f"q{index}"
        check(f"{tag}.promptId", question.get("prompt"), question.get("promptId"))
        check(f"{tag}.explanationId", question.get("explanation"), question.get("explanationId"))
        for option_index, option in enumerate(question.get("options", []), start=1):
            check(f"{tag}.option{option_index}.textId", option.get("text"), option.get("textId"))
        spec = question.get("code")
        if spec:
            check(f"{tag}.instructionsMarkdownId",
                  spec.get("instructionsMarkdown"), spec.get("instructionsMarkdownId"))
        essay = question.get("essay")
        if essay:
            check(f"{tag}.rubricMarkdownId",
                  essay.get("rubricMarkdown"), essay.get("rubricMarkdownId"))

    return missing, empty, checked


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify a localisation pass changed nothing else.")
    parser.add_argument("original", type=Path)
    parser.add_argument("translated", type=Path)
    parser.add_argument("--allow-missing", action="store_true", help="report missing rather than fail")
    args = parser.parse_args()

    original = json.loads(args.original.read_text(encoding="utf-8"))
    translated = json.loads(args.translated.read_text(encoding="utf-8"))

    stripped = strip_localised(translated)
    difference = first_difference(original, stripped)
    print(f"  {args.translated.name}")
    if difference:
        print(f"    STRUCTURE CHANGED at {difference}")
        return 1
    print("    structure identical to the original once Id fields are removed")

    missing, empty, checked = coverage(translated)
    print(f"    {checked - len(missing)}/{checked} string(s) have an Indonesian counterpart")
    for label in missing[:8]:
        print(f"      missing: {label}")
    if len(missing) > 8:
        print(f"      ... and {len(missing) - 8} more")
    for label in empty[:8]:
        print(f"      unusable: {label}")
    if len(empty) > 8:
        print(f"      ... and {len(empty) - 8} more")

    if (missing or empty) and not args.allow_missing:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
