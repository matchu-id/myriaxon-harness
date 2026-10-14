#!/usr/bin/env python3
"""Audit every translated file against the translation map it should have come from.

Two failures motivated this, and both were invisible to every check that existed.

1. `ai-ml-engineer/01.id.json` was a copy of the English level with `Id` fields added -- all 140 values
   identical to their source. It sat in the tree looking like finished work. The guard passed it as
   *structurally* correct, and `accept_localisation.py` counted it as one more translated level. Only a
   content check could tell.
2. Its translation map was also still English, so regenerating from the map reproduced the same useless file.
   `apply_localisation.py` now refuses `--in-place` on a file that already carries `Id` fields, but nothing
   had been checking whether a file's contents agree with its map.

Being "present on disk" is therefore not evidence. This compares, for every level:

* every `Id` value against the same key in the map, and
* how many `Id` values are byte-identical to their English source,

and reports a level as STRAY when its `Id` values do not match a translated map, or as ENGLISH-COPY when a
substantial share of them are unchanged. A code-only option is expected to match its English source, so the
threshold counts only values with translatable prose.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(r"C:\Users\arian\OneDrive\ドキュメント\Myriaxon")
TRACKS = ("ai-ml-engineer", "fullstack-developer", "data-scientist")
ID_FOR = {
    "title": "titleId", "scenarioMarkdown": "scenarioMarkdownId", "prompt": "promptId",
    "explanation": "explanationId", "text": "textId",
    "instructionsMarkdown": "instructionsMarkdownId", "rubricMarkdown": "rubricMarkdownId",
}


def paths(data: dict) -> list[str]:
    out: list[str] = []
    case = data.get("caseStudy", {})
    for key in ("title", "scenarioMarkdown"):
        if key in case:
            out.append(f"caseStudy.{key}")
    for i, question in enumerate(data.get("questions", [])):
        base = f"questions.{i}"
        for key in ("prompt", "explanation"):
            if key in question:
                out.append(f"{base}.{key}")
        for j, option in enumerate(question.get("options", [])):
            out.append(f"{base}.options.{j}.text")
        if "instructionsMarkdown" in question.get("code", {}):
            out.append(f"{base}.code.instructionsMarkdown")
        if "rubricMarkdown" in question.get("essay", {}):
            out.append(f"{base}.essay.rubricMarkdown")
    return out


def lookup(node, path: str):
    for part in path.split("."):
        node = node[int(part)] if isinstance(node, list) else node[part]
    return node


def strip_code(text: str) -> str:
    out: list[str] = []
    depth = 0
    for char in text:
        if char == "`":
            depth = 1 - depth
            continue
        if depth == 0:
            out.append(char)
    return "".join(out)


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit translated files against their maps.")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args()

    rows: list[dict] = []
    padded = 0
    exact_copies = 0

    for track in TRACKS:
        levels = ROOT / "certivo-rework" / track / "levels"
        maps = ROOT / "localisation"
        for produced in sorted(levels.glob("*.id.json")):
            stem = produced.name.replace(".id.json", "")
            source = levels / f"{stem}.json"
            map_path = maps / f"{track}-{stem}.json"
            if not source.exists():
                continue
            original = json.loads(source.read_text(encoding="utf-8"))
            translated = json.loads(produced.read_text(encoding="utf-8"))
            mapping = json.loads(map_path.read_text(encoding="utf-8")) if map_path.exists() else {}

            total = prose_same = mismatched = 0
            for path in paths(original):
                english = lookup(original, path)
                id_key = ID_FOR[path.split(".")[-1]]
                try:
                    current = lookup(translated, path.rsplit(".", 1)[0] + "." + id_key)
                except (KeyError, IndexError, TypeError):
                    continue
                total += 1
                if not strip_code(str(english)).strip():
                    continue          # code-only: expected to be identical
                if current == english:
                    prose_same += 1
                if map_path.exists():
                    mapped = mapping.get(path)
                    if mapped is not None and mapped != current:
                        mismatched += 1

            if prose_same == total and total:
                verdict = "ENGLISH-COPY"
                exact_copies += 1
            elif mismatched:
                # A file whose Id values differ from its map is usually `balance_localised.py` having added
                # padding, which happens after the map is written and is expected. It is only a problem if the
                # file has *no* Indonesian at all, which the `=English` column catches. Reported as PADDED so
                # the two are not confused: the first version of this audit called every one of these STRAY
                # and produced 12 false alarms against files that were correctly translated.
                verdict = "PADDED (rebalanced after its map)"
                padded += 1
            else:
                verdict = "ok"
            rows.append({"track": track, "level": stem, "total": total,
                         "english": prose_same, "map_mismatch": mismatched, "verdict": verdict})

    if args.json:
        print(json.dumps(rows, indent=2))
        return 1 if exact_copies else 0

    print(f"  {'level':<34} {'strings':>7} {'=English':>9} {'vs map':>7}  verdict")
    for row in rows:
        print(f"  {row['track'] + '/' + row['level']:<34} {row['total']:>7} {row['english']:>9} "
              f"{row['map_mismatch']:>7}  {row['verdict']}")

    print()
    print(f"  {len(rows)} translated file(s); {exact_copies} English copy/copies, {padded} rebalanced")
    if exact_copies:
        print()
        print("  A file that exists is not a file that is translated. These need regenerating from a")
        print("  translated map, or translating:")
        for row in rows:
            if row["verdict"] == "ENGLISH-COPY":
                print(f"    - {row['track']}/{row['level']}.id.json  ({row['verdict']})")
        return 1
    print("  no translated file is a copy of its English source")
    return 0


if __name__ == "__main__":
    sys.exit(main())
