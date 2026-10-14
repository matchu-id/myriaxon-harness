#!/usr/bin/env python3
"""Replace two level 3 questions whose option lengths were guessable by shape.

Both had the problem that code-fragment answers cannot be padded without corrupting the syntax: q6 spanned
19 to 62 characters (a factor of 3.3) and q13 spanned 18 to 53 (2.9). The balancer pads distractors to
equalise lengths, but padding inside a code fence produces code that no longer runs, so it left them alone
and the span detector flagged them.

The fix is to ask the same thing with prose options, which pad cleanly, rather than to weaken either the
question or the detector. The technical content is unchanged: template-literal interpolation in one case
and non-mutating object updates in the other.
"""

from __future__ import annotations

import json
from pathlib import Path

PATH = Path(r"C:\Users\arian\OneDrive\ドキュメント\Myriaxon\certivo-rework\fullstack-developer\levels\03.json")

REPLACEMENTS = {
    "builds the string": {
        "prompt": "Which template literal interpolates both `name` and `price` into the string?",
        "explanation": "A template literal interpolates only what is inside `${ }`, so the price needs its "
                       "own pair of braces — `${name} — $${price}`. Writing `$price` without braces is not "
                       "an interpolation at all: it prints the word, which is the mistake that makes a "
                       "price render as the literal text `$price` on the page.",
        "options": [
            { "text": "The one writing `${name}` and `$${price}`, each in its own braces", "correct": True },
            { "text": "The one writing `${name}` and `$price`, with no braces on the price", "correct": False },
            { "text": "The one writing `$name` and `$$price`, with no braces on either value", "correct": False },
            { "text": "The one writing `{name}` and `${price}`, with single braces on the name", "correct": False },
        ],
    },
    "without mutating the original": {
        "prompt": "A cart item's quantity must become 2 without mutating the original object. Which approach?",
        "explanation": "Spreading the original into a new object and overriding `qty` leaves the original "
                       "untouched. Assigning to `cartItem.qty` changes the object in place, which many "
                       "frameworks cannot detect because the reference itself has not changed — so nothing "
                       "re-renders, and the bug looks like a stale view rather than a state problem.",
        "options": [
            { "text": "Spread the original into a new object and override the quantity field", "correct": True },
            { "text": "Assign the new quantity directly to the property on the original object", "correct": False },
            { "text": "Pass the original object to a copy helper that updates it in place", "correct": False },
            { "text": "Push the original and a second object holding the new quantity into an array", "correct": False },
        ],
    },
}

EXTRA = [
    {
        "skill": "frontend-frameworks",
        "prompt": "Why split a page into components rather than writing one large one?",
        "explanation": "Each component owns one piece of the interface, so it can be reasoned about, "
                       "reused and tested on its own. A single large component has to be understood as a "
                       "whole, and a change to one part risks the behaviour of every other part that "
                       "happens to share its state.",
        "options": [
            { "text": "One piece of the interface can be reasoned about, reused and tested alone", "correct": True },
            { "text": "The browser renders many small components faster than one large component", "correct": False },
            { "text": "Each component gets its own stylesheet scope, which avoids class collisions", "correct": False },
            { "text": "It reduces the total amount of code the project has to maintain over time", "correct": False },
        ],
    },
    {
        "skill": "javascript-typescript",
        "prompt": "A component receives an array prop and calls `.sort()` on it directly. What is the problem?",
        "explanation": "`sort` reorders the array in place, so it mutates a value the component was given "
                       "rather than one it owns. The parent's data changes without the parent knowing, "
                       "which is how a list ends up in an order nothing asked for. Sorting a copy — "
                       "`[...items].sort(...)` — keeps the change local.",
        "options": [
            { "text": "It reorders a value the component was given rather than one it owns", "correct": True },
            { "text": "It returns a new array, so the parent's data is left out of date instead", "correct": False },
            { "text": "It sorts alphabetically rather than by the field the component intended", "correct": False },
            { "text": "It fails whenever the array contains objects rather than plain strings", "correct": False },
        ],
    },
]


def main() -> int:
    data = json.loads(PATH.read_text(encoding="utf-8"))
    replaced = 0

    for question in data["questions"]:
        if "options" not in question:
            continue
        for marker, replacement in REPLACEMENTS.items():
            if marker in question.get("prompt", ""):
                question["prompt"] = replacement["prompt"]
                question["explanation"] = replacement["explanation"]
                question["options"] = replacement["options"]
                replaced += 1
                break

    for question in EXTRA:
        if any(q.get("prompt") == question["prompt"] for q in data["questions"]):
            continue
        data["questions"].append(question)

    PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8", newline="\n")

    counts = {"mc": 0, "code": 0, "essay": 0}
    for question in data["questions"]:
        kind = "mc" if "options" in question else ("code" if "code" in question else "essay")
        counts[kind] += 1
    print(f"  replaced {replaced} question(s); added {len(EXTRA)}")
    print(f"  now: {len(data['questions'])} questions {counts}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
