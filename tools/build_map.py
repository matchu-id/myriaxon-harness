"""Build a localisation map from an English level file plus a translated line-per-value work file.

The work file (`localisation/work/NN.id.tsv`) holds `key<TAB>Indonesian` for every value, written by hand.
`loc.py import` merges it into the map JSON, but for a level that also needs code-span corrections it is
cleaner to build the map here, where two rules are applied mechanically:

1. A value whose English is nothing but backticked code keeps the English verbatim -- there is no prose to
   translate, and the guard strips code spans before comparing.
2. A distractor that would otherwise end up shorter than the correct answer gets Indonesian prose padding
   **after** its closing backtick, so the code stays copy-pasteable and the correct answer is not revealed
   by being the longest option.

    python tools/build_map.py 03 [--write]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LEVELS = ROOT / "certivo-rework" / "fullstack-developer" / "levels"
WORK = ROOT / "localisation" / "work"
MAPS = ROOT / "localisation"
BT = chr(0x60)
PAD = " dalam situasi ini"

# Values corrected by hand after the first pass: inline code comments removed, padding moved outside.
OVERRIDES = {
    "03": {
        "questions.5.options.1.text":
            BT + BT + " " + BT + "{name} \u2014 $" + BT + "{price}" + BT + " " + BT + BT,
        "questions.5.options.2.text":
            BT + "name + " + chr(0x22) + " \u2014 $" + chr(0x22) + " + price" + BT + " -- penggabungan string",
        "questions.5.options.3.text":
            BT + BT + " " + BT + "{name} \u2014 $price" + BT + " " + BT + BT
            + " -- $price tanpa kurung kurawal bukan penyisipan",
        "questions.6.options.2.text":
            BT + "const { name, price } = product" + BT + " -- destructuring objek",
        "questions.8.options.3.text":
            BT + "display: block;" + BT + " dengan lebar tetap pada setiap card, yang tidak berlaku di sini",
        "questions.15.options.3.text":
            BT + "display: grid; place-items: center" + BT + " pada panelnya di setiap kondisi yang terukur "
            "sejauh ini",
    },
}

# Distractors that need the prose padding so the correct answer is not the longest option.
PAD_KEYS = {"03": []}


def unescape(value: str) -> str:
    out: list[str] = []
    index = 0
    while index < len(value):
        char = value[index]
        if char == "\\" and index + 1 < len(value) and value[index + 1] in "nrt\\":
            out.append({"n": "\n", "r": "\r", "t": "\t", "\\": "\\"}[value[index + 1]])
            index += 2
            continue
        out.append(char)
        index += 1
    return "".join(out)


def prose_only(text: str) -> str:
    out: list[str] = []
    inside = False
    for char in text:
        if char == BT:
            inside = not inside
            continue
        if not inside:
            out.append(char)
    return "".join(out).strip()


def resolve(data, dotted):
    node = data
    parts = dotted.split(".")
    for part in parts[:-1]:
        node = node[int(part)] if part.isdigit() else node[part]
    return node, parts[-1]


def main() -> int:
    nn = sys.argv[1]
    write = "--write" in sys.argv
    english = json.loads((LEVELS / f"{nn}.json").read_text(encoding="utf-8"))

    translated: dict[str, str] = {}
    for line in (WORK / f"{nn}.id.tsv").read_text(encoding="utf-8").split("\n"):
        if not line.strip():
            continue
        key, value = line.split("\t", 1)
        translated[key] = unescape(value)

    translated.update(OVERRIDES.get(nn, {}))

    # Rule 1: code-only options keep the English.
    for key in list(translated):
        if not key.endswith(".text"):
            continue
        source, leaf = resolve(english, key)
        if prose_only(source[leaf]) == "":
            if translated[key] != source[leaf]:
                print(f"  code-only, reverted to English: {key}")
            translated[key] = source[leaf]

    # Rule 2: pad a distractor that is shorter than the correct answer.
    for key in PAD_KEYS.get(nn, []):
        if PAD not in translated[key]:
            translated[key] = translated[key] + PAD
            print(f"  padded: {key}")

    expected = json.loads((MAPS / f"fullstack-developer-{nn}.json").read_text(encoding="utf-8"))
    if list(translated) != list(expected):
        missing = [k for k in expected if k not in translated]
        extra = [k for k in translated if k not in expected]
        print(f"  REFUSING: key set differs (missing {len(missing)}, extra {len(extra)})")
        for key in (missing + extra)[:10]:
            print(f"    {key}")
        return 1

    same = [k for k in expected if expected[k] == translated[k]]
    print(f"  {len(translated)} value(s); {len(same)} unchanged from the current map")
    if write:
        path = MAPS / f"fullstack-developer-{nn}.json"
        path.write_text(json.dumps(translated, indent=2, ensure_ascii=False) + "\n",
                        encoding="utf-8", newline="\n")
        print(f"  wrote {path.name}")
    else:
        print("  dry run: nothing written (pass --write)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
