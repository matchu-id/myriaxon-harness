#!/usr/bin/env python3
"""Prepare level 11 for its final shape: 21 multiple-choice, 4 code, 3 essay.

Two problems this repairs, both mine.

1. **The essays had no `explanation`.** The question schema requires one on every question regardless of
   kind, and `validate_level.py` caught all three. The explanation for an essay is not the rubric -- the
   rubric is what the grader uses and the learner never sees it -- so what goes here is the reasoning the
   question is built to draw out, written to be read after the attempt.

2. **The level had 27 questions**, three of them essays, but the brief fixes the total at 25. The shape
   that satisfies both the brief and the schema is 21 multiple-choice + 4 code + 3 essay = 28 questions...
   except the brief says 25 per level, which the ten finished levels meet as 21 + 4. So one essay has to
   go, and the level becomes 21 + 4 + ... = 25, meaning 21 multiple-choice would leave no room.

   Resolved as 18 multiple-choice + 4 code + 3 essay = 25. That keeps the brief's total, keeps all four
   code questions, and keeps three essays as rule (3) requires, at the cost of three multiple-choice
   questions. Which three to drop is a judgement, so this reports the candidates rather than picking
   silently.

Usage:  python3 fix_level11.py [--drop SLUG ...]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

PATH = Path(r"C:\Users\arian\OneDrive\ドキュメント\Myriaxon\certivo-rework\ai-ml-engineer\levels\11.json")

# Reasoning the essay is designed to surface, to be shown after the attempt. Deliberately not the
# rubric: the rubric names what earns marks, these say what the question was getting at.
ESSAY_EXPLANATIONS = {
    "how you would decide what to do about it":
        "The measurement is the easy half. A false-positive rate of 6% against 14% means one region is "
        "absorbing more of the model's cost than the other, and that is the harm to reason about — not the "
        "pooled accuracy, which cannot see it. Which fairness criterion you choose decides what counts as "
        "fixed: equal false-positive rates targets the burden, equal opportunity targets the benefit, and "
        "they can disagree. Before recommending anything, establish that the gap is real rather than "
        "sampling noise, and that it is not simply the regions differing on features the model is entitled "
        "to use. Then the decision belongs to whoever owns the consequences: accepting a disparity is a "
        "judgement about acceptable harm and legal exposure, and it should be written down with who "
        "accepted it. Removing the region column or equalising scores by group both treat the symptom — if "
        "the gap comes from the labels, it returns by another route.",
    "what the interval actually tells you":
        "A 95% interval of [2%, 14%] says the data are consistent with a gap anywhere in that range, and "
        "the midpoint has no special standing — the point estimate of 8 is one value inside it, not the "
        "answer. That the interval excludes zero means a real gap is more plausible than none; it does not "
        "mean the gap is large, and it is not a probability that the gap exists. The width is itself the "
        "finding: this audit lacks the evidence to judge against any threshold, which is different from "
        "having evidence of no problem. The next step is more observations or a longer comparable period "
        "to narrow the range — not a lower confidence level, which buys precision by weakening the claim. "
        "And the judgement still needs a pre-decided threshold: if a gap below some size would not justify "
        "action, an upper bound of 14 points generally will not rule it out.",
    "what can and cannot be concluded":
        "Twenty tests at 0.05 would produce about one significant result by chance even with no real effect "
        "anywhere, so two findings are weak evidence on their own — candidates for confirmation, not "
        "established disparities, and not dismissible either. A correction such as Bonferroni or a "
        "false-discovery-rate procedure controls that error rate, at the cost of power: modest real "
        "disparities may stop being detectable. The sharper distinction is between a subgroup named before "
        "the audit and one chosen because it looked unusual. The first supports a single test at the "
        "stated level; the second has already been through a search, so its p-value no longer means what "
        "it appears to, and it needs confirming on fresh data. The practical asymmetry matters too — a "
        "false finding costs an investigation that finds nothing, while a missed one leaves a group "
        "bearing harm unexamined.",
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--drop", nargs="*", default=None,
                        help="prompt substrings identifying which multiple-choice questions to remove")
    args = parser.parse_args()

    data = json.loads(PATH.read_text(encoding="utf-8"))
    questions = data["questions"]

    filled = 0
    for question in questions:
        if "essay" not in question:
            continue
        if question.get("explanation"):
            continue
        prompt = question.get("prompt", "")
        for fragment, explanation in ESSAY_EXPLANATIONS.items():
            if fragment in prompt:
                question["explanation"] = explanation
                filled += 1
                break
        else:
            print(f"  no explanation prepared for: {prompt[:60]}...")

    # Any multiple-choice question beyond the 18 the 25-question shape allows is a removal candidate.
    mc = [q for q in questions if "options" in q]
    if args.drop is not None:
        for fragment in args.drop:
            matches = [q for q in questions if "options" in q and fragment in q.get("prompt", "")]
            if not matches:
                print(f"  --drop {fragment!r} matched nothing")
                continue
            for match in matches:
                questions.remove(match)
                print(f"  dropped: {match['prompt'][:64]}...")

    PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8", newline="\n")

    counts: dict[str, int] = {}
    for question in questions:
        kind = "mc" if "options" in question else ("code" if "code" in question else "essay")
        counts[kind] = counts.get(kind, 0) + 1
    print(f"  explanations filled: {filled}")
    print(f"  now: {len(questions)} questions {counts}")
    if len(questions) != 25:
        print(f"  {len(mc)} multiple-choice present; the 25-question shape needs 18 "
              f"(18 + 4 code + 3 essay), so re-run with --drop naming {len(mc) - 18} of them")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
