#!/usr/bin/env python3
"""Insert the missing 25th question into level 03, then report where the balance stands.

Level 03 was authored with 20 multiple-choice plus 4 code = 24, one short of the required 25. Counting
by hand is how that happened; `validate_level.py` is what caught it. The inserted question covers the
baseline idea the level is built around, which was already the subject of several others -- adding it
does not introduce a new topic, it closes the gap on one already being tested.

Run before `shuffle_options.py` and `balance_lengths.py`, because those two assign positions and lengths
by index and would otherwise leave the new question at the end with the old pattern.
"""

from __future__ import annotations

import json
from pathlib import Path

PATH = Path(r"C:\Users\arian\OneDrive\ドキュメント\Myriaxon\certivo-rework\ai-ml-engineer\levels\03.json")

NEW_QUESTION = {
    "skill": "model-development",
    "prompt": "Harlow's first model predicts \"not returned\" for every order and scores 88%. A teammate calls it a success. What is the correct response?",
    "explanation": "The score equals the majority-class baseline exactly, which means the model has learned nothing that distinguishes a returned order from any other. A classifier on imbalanced data has to be judged on how it handles the minority class, not on the share of rows it gets right by always saying the same thing.",
    "options": [
        {"text": "It matches the majority baseline, so it has learned nothing yet", "correct": True},
        {"text": "It is a success, because 88% is a high accuracy for a first model", "correct": False},
        {"text": "It is a success, since a model need only beat a coin flip to be useful", "correct": False},
        {"text": "It has overfitted, which is why it predicts only the common class", "correct": False},
    ],
}


def main() -> int:
    data = json.loads(PATH.read_text(encoding="utf-8"))
    questions = data["questions"]

    if any(q.get("prompt", "").startswith("Harlow's first model predicts") for q in questions):
        print("  already present, nothing inserted")
        return 0

    # Place it before the code questions so the reading order stays prose-then-practice, which is the
    # order every other level uses.
    first_code = next((i for i, q in enumerate(questions) if "code" in q), len(questions))
    questions.insert(first_code, NEW_QUESTION)

    PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8", newline="\n")

    mc = sum(1 for q in questions if "options" in q)
    code = sum(1 for q in questions if "code" in q)
    print(f"  inserted the 25th question")
    print(f"  now: {len(questions)} questions (mc={mc} code={code})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
