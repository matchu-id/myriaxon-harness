"""Correct the code-only options in levels 02 and 03: revert inline `--` comments, pad after the span.

Two mistakes are fixed here.

1. Inline `--` comments were added *inside* the backticks of code-only options to stop them being identical
   to the English. That is wrong twice over: `products.map(...) -- satu elemen per produk` is not valid
   JavaScript, and a code option is *supposed* to read the same in both languages because it is code, not
   prose. Every such comment is removed.

2. `balance_lengths.py` built the English so the correct answer is never the longest option. Where a
   localised distractor would otherwise fall shorter than the correct answer, the padding goes **after** the
   closing backtick as Indonesian prose (`... ` + "` dalam situasi ini"`), so the code stays copy-pasteable.

    python tools/fix_code_options.py
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MAPS = ROOT / "localisation"
LEVELS = ROOT / "certivo-rework" / "fullstack-developer" / "levels"

BT = chr(0x60)
PAD = " dalam situasi ini"

# Bare-code options that must read exactly like the English: code only, no comment, no prose.
RESTORE = {
    "02": [
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
    ],
    "03": [
        "questions.5.options.1.text",
        "questions.6.options.2.text",
    ],
}

# Distractors whose code is shorter than the correct answer, so they need the prose padding.
PAD_SHORT = {
    "02": [
        "questions.2.options.1.text",
        "questions.2.options.3.text",
        "questions.8.options.0.text",
    ],
}

# The correct answer itself can lose length in translation; restore its lead with a trailing clause.
PAD_CORRECT = {
    "03": {
        "questions.5.options.2.text": BT + BT + " " + BT + "{name} \u2014 $" + BT + "{price}" + BT + " " + BT + BT
        + " \u2014 kedua nilai disisipkan",
        "questions.15.options.3.text": "`display: grid; place-items: center` pada panelnya di setiap kondisi "
        + "yang terukur sejauh ini",
    },
}


def strip_code(text: str) -> str:
    """The prose left after removing every backticked span — what the guard compares."""
    out: list[str] = []
    inside = False
    for char in text:
        if char == BT:
            inside = not inside
            continue
        if not inside:
            out.append(char)
    return "".join(out).strip()


def main() -> int:
    for nn, keys in RESTORE.items():
        path = MAPS / f"fullstack-developer-{nn}.json"
        english = json.loads((LEVELS / f"{nn}.json").read_text(encoding="utf-8"))
        localised = json.loads(path.read_text(encoding="utf-8"))

        def resolve(data, dotted):
            node = data
            parts = dotted.split(".")
            for part in parts[:-1]:
                node = node[int(part)] if part.isdigit() else node[part]
            return node, parts[-1]

        for key in keys:
            container, leaf = resolve(english, key)
            value = container[leaf]
            target, target_leaf = resolve(localised, key)
            target[target_leaf] = value
            print(f"  {nn} restored  {key}: {value[:60]}")

        for key in PAD_SHORT.get(nn, []):
            current = localised[key]
            if PAD not in current:
                updated = current + (PAD if current.endswith(BT) else PAD)
                localised[key] = updated
                print(f"  {nn} padded    {key}: {updated[:70]}")

        for key, value in PAD_CORRECT.get(nn, {}).items():
            localised[key] = value
            print(f"  {nn} correct   {key}: {value[:70]}")

        path.write_text(json.dumps(localised, indent=2, ensure_ascii=False) + "\n",
                        encoding="utf-8", newline="\n")
        print(f"  wrote {path.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
