"""List option lengths per question, so padding is applied only where the balance needs it.

`balance_lengths.py` padded the English distractors so the correct answer is not the longest option in a
question. When a localised option is bare code it cannot carry that padding inside the span, so the padding
has to move outside the closing backtick. This prints, for each question, the length of the correct answer
and of every other option, from the **English** level file, so the decision is made from the original
balance rather than guessed.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

LEVELS = Path(__file__).resolve().parent.parent / "certivo-rework" / "fullstack-developer" / "levels"


def main() -> int:
    for nn in (sys.argv[1:] or ["01", "02", "03"]):
        data = json.loads((LEVELS / f"{nn}.json").read_text(encoding="utf-8"))
        print(f"=== level {nn} ===")
        for q_index, question in enumerate(data.get("questions", [])):
            options = question.get("options", [])
            if not options:
                continue
            lengths = [len(str(o["text"]).strip()) for o in options]
            correct = next((i for i, o in enumerate(options) if o.get("correct")), None)
            if correct is None:
                continue
            longest_other = max((n for i, n in enumerate(lengths) if i != correct), default=0)
            flag = "CORRECT IS LONGEST" if lengths[correct] > longest_other else "ok"
            if flag != "ok":
                print(f"  q{q_index} {flag}: correct={lengths[correct]} others={lengths}")
                for i, option in enumerate(options):
                    mark = "CORRECT" if i == correct else "       "
                    print(f"      {mark} [{lengths[i]:3d}] {option['text']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
