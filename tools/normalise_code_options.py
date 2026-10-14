"""Revert any option that has nothing translatable outside its code spans to the English text.

A value that is *nothing but* backticked code — `` `SELECT * FROM products WHERE price < 50` `` — has no
prose to translate, so its Indonesian counterpart is the same string. `guard_localisation.py` now strips code
spans before comparing, so such a value is correctly identical and must be left byte-for-byte as the English.

This makes that rule mechanical rather than a judgement call: for every option in levels 01-03 it removes
backticked spans and asks whether any word remains. If not, the value is replaced with the English original --
which undoes inline `--` comments and trailing `dalam situasi ini` padding added to code-only options.
Options that *do* carry prose after the code (`` `#price`, karena ... ``) are left alone.

    python tools/normalise_code_options.py [--write]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MAPS = ROOT / "localisation"
LEVELS = ROOT / "certivo-rework" / "fullstack-developer" / "levels"
BT = chr(0x60)


def prose_only(text: str) -> str:
    """What remains of a value once every backticked span is removed."""
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
    write = "--write" in sys.argv
    for nn in ("01", "02", "03"):
        english = json.loads((LEVELS / f"{nn}.json").read_text(encoding="utf-8"))
        map_path = MAPS / f"fullstack-developer-{nn}.json"
        localised = json.loads(map_path.read_text(encoding="utf-8"))
        changed = 0
        for q_index, question in enumerate(english.get("questions", [])):
            for option_index, option in enumerate(question.get("options", [])):
                key = f"questions.{q_index}.options.{option_index}.text"
                if key not in localised:
                    continue
                if prose_only(localised[key]):
                    continue
                if localised[key] == option["text"]:
                    continue
                print(f"  {nn} revert {key}")
                print(f"      was: {localised[key][:90]}")
                print(f"      now: {option['text'][:90]}")
                localised[key] = option["text"]
                changed += 1
        print(f"  level {nn}: {changed} code-only option(s) restored to the English")
        if write and changed:
            map_path.write_text(json.dumps(localised, indent=2, ensure_ascii=False) + "\n",
                                encoding="utf-8", newline="\n")
            print(f"  wrote {map_path.name}")
    if not write:
        print("  dry run: nothing written (pass --write)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
