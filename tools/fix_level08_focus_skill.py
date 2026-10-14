#!/usr/bin/env python3
"""Give level 8 the `html-css` question its focus skills require.

The repo's publish gate refuses a level that declares a focus skill no question tests:

    `levels/08.json focus skill "html-css" has no questions`

Level 8's subject is a catalogue with a search box, and every question in it was tagged
`databases-data-modeling`, `frontend-frameworks` or `backend-api-design`. So the gate was right: the level
claimed to teach something it never asked about.

The question added here belongs to the case study rather than being bolted on -- a search box and a results
list are markup before they are anything else, and the result count a shopper needs announced is an
`aria-live` concern. One question is removed to hold the level at 25: the composite-index question, whose
subject is covered more thoroughly by level 15's query-planning material.
"""

from __future__ import annotations

import json
from pathlib import Path

PATH = Path(r"C:\Users\arian\OneDrive\ドキュメント\Myriaxon\certivo-rework\fullstack-developer\levels\08.json")

ADDITION = {
    "skill": "html-css",
    "prompt": "A search box filters the catalogue as the shopper types, and the number of matches updates "
              "below it. What does the markup need so the count is not just visual?",
    "explanation": "A live region, so the change is announced rather than only shown — `role=\"status\"` or "
                   "`aria-live=\"polite\"` on the element holding the count. Without it a screen reader user "
                   "types into the box and hears nothing about the results changing, which is the same "
                   "silence as a loading spinner with no announcement.",
    "options": [
        {"text": "A live region around the count, so a change to it is announced", "correct": True},
        {"text": "A `title` attribute on the count, read aloud when it is hovered", "correct": False},
        {"text": "Autofocus on the count, so the keyboard moves there when it updates", "correct": False},
        {"text": "A larger font on the count, so it is visible without scrolling", "correct": False},
    ],
}

# Chosen because level 15 covers query plans and index choice in far more depth, so this level loses least
# by giving up its one composite-index question.
DROP_PROMPT = "What does a composite index on `(category_id, price)` help with"


def main() -> int:
    data = json.loads(PATH.read_text(encoding="utf-8"))
    questions = data["questions"]

    before_mc = sum(1 for q in questions if "options" in q)
    before_code = sum(1 for q in questions if "code" in q)
    print(f"  before: total={len(questions)} mc={before_mc} code={before_code}")

    if any(q.get("skill") == "html-css" for q in questions):
        print("  level 8 already has an html-css question; nothing to do")
        return 0

    remaining = [q for q in questions if DROP_PROMPT not in q.get("prompt", "")]
    if len(remaining) != len(questions) - 1:
        print(f"  REFUSING: the question to drop matched {len(questions) - len(remaining)} time(s), expected 1")
        return 1

    remaining.append(ADDITION)
    data["questions"] = remaining

    after_mc = sum(1 for q in remaining if "options" in q)
    after_code = sum(1 for q in remaining if "code" in q)
    if (len(remaining), after_mc, after_code) != (25, 21, 4):
        print(f"  REFUSING to write: would leave total={len(remaining)} mc={after_mc} code={after_code}")
        return 1

    PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8", newline="\n")
    print(f"  after:  total={len(remaining)} mc={after_mc} code={after_code}")
    print("  added the html-css question and removed the composite-index one")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
