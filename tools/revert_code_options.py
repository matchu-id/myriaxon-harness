"""Revert the eleven code-only options in level 02 to their exact English text.

These options are nothing but a code snippet, so there is no prose to translate and the Indonesian
counterpart is the same string. `guard_localisation.py` strips code spans before comparing, so identical is
the correct result for them; the earlier `-- ...` Indonesian comments are removed because
`products.map((product) => product.name) -- satu elemen per produk` is not valid JavaScript, and a
certification platform must not offer broken code as an answer choice.

Options whose English carries prose after the code (`` `#price`, karena ... ``) are untouched.

    python tools/revert_code_options.py --write
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LEVELS = ROOT / "certivo-rework" / "fullstack-developer" / "levels"
MAP = ROOT / "localisation" / "fullstack-developer-02.json"

KEYS = [
    "questions.2.options.0.text",
    "questions.2.options.1.text",
    "questions.2.options.2.text",
    "questions.2.options.3.text",
    "questions.4.options.0.text",
    "questions.5.options.1.text",
    "questions.8.options.0.text",
    "questions.9.options.0.text",
    "questions.9.options.1.text",
    "questions.13.options.1.text",
    "questions.13.options.3.text",
]


def resolve(data, dotted):
    node = data
    parts = dotted.split(".")
    for part in parts[:-1]:
        node = node[int(part)] if part.isdigit() else node[part]
    return node, parts[-1]


def main() -> int:
    write = "--write" in sys.argv
    english = json.loads((LEVELS / "02.json").read_text(encoding="utf-8"))
    localised = json.loads(MAP.read_text(encoding="utf-8"))
    for key in KEYS:
        source, leaf = resolve(english, key)
        value = source[leaf]
        if localised[key] == value:
            print(f"  already English: {key}")
            continue
        print(f"  revert {key}")
        print(f"      was: {localised[key]}")
        print(f"      now: {value}")
        localised[key] = value
    if write:
        MAP.write_text(json.dumps(localised, indent=2, ensure_ascii=False) + "\n",
                       encoding="utf-8", newline="\n")
        print(f"  wrote {MAP.name}")
    else:
        print("  dry run: nothing written (pass --write)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
