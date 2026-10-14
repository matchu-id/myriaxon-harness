#!/usr/bin/env python3
"""Append prepared questions to a level, from a JSON array file.

`insert_question.py` adds one question at a time and refuses anything that is not multiple choice. This
appends an arbitrary list, which is what an essay level needs when the essays have to be authored as a
separate batch -- and it validates each one against the schema's requirements before writing, so a
malformed question fails here rather than at the publish gate.

Usage:  python3 append_questions.py <level.json> <questions.json>
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REQUIRED = ("skill", "prompt")
EXPLANATIONS = Path(__file__).resolve().parent / "essay_explanations.json"


def fill_explanation(question: dict, texts: dict[str, str]) -> bool:
    """Populate an essay's explanation, preferring one written inline in the question itself.

    The explanation is separate from the rubric on purpose -- the rubric is what the grader reads and the
    learner never sees it, so the explanation is a second piece of writing that says what the question was
    getting at. It can be given either way:

      * inline, as an `explanation` key on the question being appended, which keeps an essay and its
        reasoning in one file; or
      * in `essay_explanations.json`, keyed by a distinctive fragment of the prompt, which is how the AI/ML
        levels were written and remains the fallback.

    Inline is preferred because the alternative needs two edits before anything can be appended, which is
    what made writing three essays take four attempts.
    """
    if question.get("explanation"):
        return True
    prompt = question.get("prompt", "")
    for fragment, explanation in texts.items():
        if fragment in prompt:
            question["explanation"] = explanation
            return True
    return False


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2

    level_path, source_path = Path(sys.argv[1]), Path(sys.argv[2])
    data = json.loads(level_path.read_text(encoding="utf-8"))
    incoming = json.loads(source_path.read_text(encoding="utf-8"))
    texts = json.loads(EXPLANATIONS.read_text(encoding="utf-8")) if EXPLANATIONS.exists() else {}
    if not isinstance(incoming, list):
        print("  the questions file must be a JSON array")
        return 1

    questions = data["questions"]
    added = 0

    for question in incoming:
        if any(q.get("prompt") == question.get("prompt") for q in questions):
            print(f"  already present, skipped: {question.get('prompt', '')[:56]}...")
            continue
        for field in REQUIRED:
            if not question.get(field):
                print(f"  REFUSING: a question is missing {field!r}")
                return 1
        if not fill_explanation(question, texts):
            print(f"  REFUSING: no explanation given and none prepared for "
                  f"{question.get('prompt', '')[:56]!r}")
            return 1
        kinds = [k for k in ("options", "code", "essay") if k in question]
        if len(kinds) != 1:
            print(f"  REFUSING: a question must have exactly one of options/code/essay, found {kinds}")
            return 1
        if kinds[0] == "essay" and not question["essay"].get("rubricMarkdown"):
            print("  REFUSING: an essay question needs a non-empty rubricMarkdown")
            return 1
        questions.append(question)
        added += 1

    level_path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                          encoding="utf-8", newline="\n")

    counts: dict[str, int] = {}
    for question in questions:
        kind = "mc" if "options" in question else ("code" if "code" in question else "essay")
        counts[kind] = counts.get(kind, 0) + 1
    print(f"  appended {added}")
    print(f"  now: {len(questions)} questions {counts}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
