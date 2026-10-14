#!/usr/bin/env python3
"""Review a localised level file for the things tools/guard_localisation.py cannot see.

The guard proves that the structure survived and that every localisable string has a non-empty,
non-identical Indonesian counterpart. It cannot see a dropped backtick, a lost `**bold**` marker, a
paragraph break that collapsed, an English number left in `4.2` form, an `Anda` that should be `kamu`,
or a sentence that was left half-translated. Those are the failures a reviewer would catch by hand, so
this catches them mechanically first.

    python tools/review_localisation.py <original.json> <translated.id.json>

Hard problems (exit 1) are markdown or numbering damage and `Anda`. Warnings are second-person
references with no `kamu` nearby and English-looking words, which are usually intentional technical
terms but are worth reading once.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

SECOND_PERSON = re.compile(r"\b(you|your|yours|yourself)\b", re.IGNORECASE)
KAMU = re.compile(r"\bkamu\b", re.IGNORECASE)
ANDA = re.compile(r"\banda\b", re.IGNORECASE)
ENGLISH_NUMBER = (r"\d+\.\d(?!\d)", r"\b\d{1,3},\d{3}\b")
FLAGGED_ENGLISH = re.compile(
    r"\b(the|and|of|is|are|was|were|with|without|that|this|these|those|which|who|whose|you|your|"
    r"from|into|than|then|when|while|where|why|how|what|only|also|more|most|much|many|not|no|has|"
    r"have|had|can|cannot|will|would|should|could|be|been|being|by|as|at|or|if|in|on|to|for|it|its|"
    r"their|there|they|we|our|us|about|above|after|all|any|because|before|below|between|both|but|"
    r"during|each|few|first|second|other|same|some|such|too|very|just|once|here|"
    r"row|rows|column|columns|table|tables|query|queries|average|count|number|distinct|ordered|sort|"
    r"limit|greater|parameter|product|products|customer|customers|order|orders|price|category|missing|"
    r"probability|prediction|outcome|feature|features|weight|range|inclusive|ascending|descending|"
    r"threshold|accuracy|correct|wrong|answer|option|write|returns|returned|safe|unsafe|false|true|"
    r"positive|negative|class|classes|label|labels|score|scores|split|train|trains|trained|set|sets|"
    r"grid|search|setting|settings|choose|chosen|change|changes|value|values|end|ends|start|starts|"
    r"point|points|line|lines|copy|copies|code|sorted|float)\b",
    re.IGNORECASE,
)
# The DRY acronym spells out an English second-person pronoun; that is a name, not a missed `kamu`.
SECOND_PERSON_EXEMPT = "Don't Repeat Yourself"


def pairs(original: dict, translated: dict):
    """Yield (label, English text, Indonesian text) for every field that should be localised."""
    case_en, case_id = original.get("caseStudy", {}), translated.get("caseStudy", {})
    for english_key, indonesian_key in (("title", "titleId"), ("scenarioMarkdown", "scenarioMarkdownId")):
        if english_key in case_en:
            yield f"caseStudy.{english_key}", case_en[english_key], case_id.get(indonesian_key, "")
    for index, (question_en, question_id) in enumerate(
            zip(original.get("questions", []), translated.get("questions", [])), start=1):
        for english_key, indonesian_key in (("prompt", "promptId"), ("explanation", "explanationId")):
            if english_key in question_en:
                yield f"q{index}.{english_key}", question_en[english_key], question_id.get(indonesian_key, "")
        for option_index, (option_en, option_id) in enumerate(
                zip(question_en.get("options", []), question_id.get("options", [])), start=1):
            yield (f"q{index}.option{option_index}.text", option_en.get("text", ""),
                   option_id.get("textId", ""))
        for spec_key, english_key, indonesian_key in (
                ("code", "instructionsMarkdown", "instructionsMarkdownId"),
                ("essay", "rubricMarkdown", "rubricMarkdownId")):
            spec_en, spec_id = question_en.get(spec_key), question_id.get(spec_key)
            if spec_en and english_key in spec_en:
                yield (f"q{index}.{spec_key}", spec_en[english_key], (spec_id or {}).get(indonesian_key, ""))


def main() -> int:
    parser = argparse.ArgumentParser(description="Review a localised level file beyond the guard.")
    parser.add_argument("original", type=Path)
    parser.add_argument("translated", type=Path)
    args = parser.parse_args()

    original = json.loads(args.original.read_text(encoding="utf-8"))
    translated = json.loads(args.translated.read_text(encoding="utf-8"))

    problems: list[str] = []
    warnings: list[str] = []
    checked = 0

    for label, english, indonesian in pairs(original, translated):
        checked += 1
        if not indonesian.strip():
            problems.append(f"{label}: empty")
            continue

        spans_en = re.findall(r"`[^`]*`", english)
        spans_id = re.findall(r"`[^`]*`", indonesian)
        if sorted(spans_en) != sorted(spans_id):
            problems.append(f"{label}: code spans differ, EN {spans_en} vs ID {spans_id}")
        for marker, description in (("**", "bold markers"), ("—", "em dashes"), ('"', "straight quotes"),
                                    ("×", "multiplication signs")):
            if english.count(marker) != indonesian.count(marker):
                problems.append(f"{label}: {description} {english.count(marker)} vs {indonesian.count(marker)}")
        if english.count(chr(10) + chr(10)) != indonesian.count(chr(10) + chr(10)):
            problems.append(f"{label}: paragraph breaks differ")
        if ANDA.search(indonesian):
            problems.append(f"{label}: uses Anda rather than kamu")
        for pattern in ENGLISH_NUMBER:
            for hit in re.findall(pattern, indonesian):
                problems.append(f"{label}: English number format {hit!r}")

        if SECOND_PERSON.search(english) and not KAMU.search(indonesian) \
                and SECOND_PERSON_EXEMPT not in english:
            warnings.append(f"{label}: English has a second-person reference, Indonesian has no 'kamu'")

        hits = sorted({match.group(0).lower() for match in FLAGGED_ENGLISH.finditer(indonesian)})
        if hits:
            warnings.append(f"{label}: English-looking word(s): {', '.join(hits)}")

    print(f"  {args.translated.name}: {checked} localised field(s) reviewed")
    print(f"    markdown, numbering and register problems: {len(problems)}")
    for problem in problems:
        print(f"      {problem}")
    print(f"    warnings worth reading: {len(warnings)}")
    for warning in warnings:
        print(f"      {warning}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
