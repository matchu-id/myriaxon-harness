#!/usr/bin/env python3
"""Correct level 9's runTransition wording, and the case that exposed it.

The instruction said `'qty'` actions "**set** that item's quantity", but the reference adds to it -- the
same behaviour `cartReducer`'s `add` action has. Adding a case that changed an existing quantity rather
than appending a new item made the two disagree: an action of `{ id: 1, qty: 9 }` against an item already
at 2 produced 11, not 9.

So the documentation was wrong and the reference was right. Worth noting because the earlier tests could
not have caught it: both of them started from an empty cart, where setting and adding are indistinguishable.
That is the same class as the vacuous `originalIntact` flag in the same question -- a test whose setup cannot
distinguish the behaviours it claims to check.
"""

from __future__ import annotations

import json
from pathlib import Path

PATH = Path(r"C:\Users\arian\OneDrive\ドキュメント\Myriaxon\certivo-rework\fullstack-developer\levels\09.json")


def main() -> int:
    data = json.loads(PATH.read_text(encoding="utf-8"))
    fixed = 0

    for question in data["questions"]:
        spec = question.get("code")
        if not spec or spec.get("entryPoint") != "runTransition":
            continue

        spec["instructionsMarkdown"] = (
            "Apply `actions` in order to `state`, where `state.reducerName` selects which rule to apply:\n\n"
            "- `'qty'` \u2014 each action is `{ id, qty }` and **adds** that quantity to the item, appending "
            "it when absent\n"
            "- `'drop'` \u2014 each action is `{ id }` and removes that item\n\n"
            "Return an object with:\n\n"
            "- `final` \u2014 the state after every action, keeping the same `reducerName`\n"
            "- `originalIntact` \u2014 `true` when the state passed in still has exactly the items and "
            "quantities it started with\n\n"
            "Never modify the state you were given. Press **Run tests** to compile and run this TypeScript "
            "in your browser using the real TypeScript compiler. Nothing runs on the server."
        )
        spec["tests"][3]["name"] = (
            "hidden: adding to an existing item changes the state and still leaves the input intact"
        )
        spec["tests"][3]["expected"] = {
            "final": {"reducerName": "qty", "items": [{"id": 1, "qty": 11}]},
            "originalIntact": True,
        }
        fixed += 1

    PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8", newline="\n")
    print(f"  corrected wording and expectation in {fixed} question(s)")
    return 0 if fixed == 1 else 1


if __name__ == "__main__":
    raise SystemExit(main())
