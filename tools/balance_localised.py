#!/usr/bin/env python3
"""Re-balance option lengths in a translated level, so translation does not make the answer guessable.

`balance_lengths.py` pads the **English** options until the correct one is longest about a quarter of the
time. Translation does not preserve that property, and it does not break it randomly: a correct answer often
states a complete causal reason while a distractor states a short wrong mechanism, so the same translation
moves them by different amounts. Measured over the first eleven translated levels, the correct answer was
longest 26% of the time against the English content's 22%, but the aggregate concealed the real problem —
`data-scientist/01.id.json` sits at **52% longest** and `fullstack-developer/03.id.json` at 38%, both well
outside the band the English audit accepts. A level like that is more guessable in Indonesian than in English.

The mechanism is deliberately the same as the English tool, because that one was arrived at through failures
worth not repeating:

* **Padding only lengthens distractors, and a distractor is never padded past `CEILING_FACTOR` times the
  longest authored option** — inflated options are their own tell.
* **Holding a question's current rank is free.** An earlier version priced every rank change equally and no
  rank change at all for "longest", so a second run moved everything back to longest and the level ended
  worse than before. `cost_of` returns zero when the wanted rank is the current one, which is what makes a
  balanced file stay balanced.
* **The correct option may be padded, distractors never trimmed.** Trimming matched authored prose once and
  silently edited 21 of 21 questions.

The padding clauses are Indonesian and semantically neutral, matching the register of the content: each is an
ordinary qualifier that changes nothing about which option is correct.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

RANK_NAMES = ("longest", "2nd", "3rd", "shortest")
GAP = 12
CEILING_FACTOR = 2.2

# Neutral, ordinary Indonesian qualifiers. None of them can turn a wrong option into a right one: they add
# hedging or scope, not information. Chosen to match how the content already writes ("dalam situasi ini",
# "pada umumnya") rather than to read as filler.
PADDING = (
    " dalam situasi ini",
    ", yang tidak berlaku di sini",
    " terlepas dari bagaimana datanya disiapkan",
    ", meskipun kadang dianggap benar",
    " pada setiap kondisi yang diukur sejauh ini",
)
CLAUSES_LONGEST_FIRST = tuple(sorted(PADDING, key=len, reverse=True))
MAX_CLAUSES = 4


def minimal_pad(text: str, target: int) -> str:
    """Extend `text` to the shortest padding that reaches `target`, keeping the achieved rank predictable."""
    best = text.rstrip()
    for _ in range(MAX_CLAUSES):
        if len(best) >= target:
            break
        candidates = [best + clause for clause in PADDING if len(best + clause) >= target]
        if candidates:
            best = min(candidates, key=len)
            break
        best = best + PADDING[-1]
    return best


def rank_of(value: int, lengths: list[int]) -> int:
    return sorted(lengths, reverse=True).index(value)


def targets_for(answer_len: int, lengths: list[int], index: int, want: int) -> dict[int, int]:
    """Distractor index -> the length that puts the answer at rank `want` (0 longest, 3 shortest)."""
    distractors = sorted((j for j in range(4) if j != index), key=lambda j: -lengths[j])
    if want == 0:
        return {j: answer_len - GAP for j in distractors}
    if want == 1:
        return {distractors[0]: answer_len + GAP, distractors[1]: answer_len - GAP,
                distractors[2]: answer_len - 2 * GAP}
    if want == 2:
        return {distractors[0]: answer_len + 2 * GAP, distractors[1]: answer_len + GAP,
                distractors[2]: answer_len - GAP}
    return {j: answer_len + GAP for j in distractors}


def cost_of(plan: dict[int, int], lengths: list[int], ceiling: int, want: int, current_rank: int) -> float:
    """How much work a plan needs, with staying put priced at zero so a balanced file stays balanced."""
    if want == current_rank:
        return 0.0
    score = 0.0
    for j, target in plan.items():
        if target > ceiling:
            score += 50.0
        elif target > lengths[j]:
            score += 1.0 + (target - lengths[j]) / 200.0
    return score


def balanced_quota(count: int) -> dict[int, int]:
    base, extra = divmod(count, 4)
    return {rank: base + (1 if rank < extra else 0) for rank in range(4)}


def main() -> int:
    parser = argparse.ArgumentParser(description="Re-balance option lengths in a translated level.")
    parser.add_argument("path", type=Path)
    parser.add_argument("--field", default="textId")
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--report", action="store_true")
    args = parser.parse_args()

    data = json.loads(args.path.read_text(encoding="utf-8"))
    questions = [q for q in (data.get("questions") or []) if q.get("options")]
    if not questions:
        print(f"  {args.path.name}: no multiple-choice questions")
        return 0

    before = Counter()
    for question in questions:
        lengths = [len(o[args.field]) for o in question["options"]]
        index = next(i for i, o in enumerate(question["options"]) if o.get("correct"))
        before[rank_of(lengths[index], lengths)] += 1

    quota = balanced_quota(len(questions))
    plan: list[int] = []
    for question in questions:
        lengths = [len(o[args.field]) for o in question["options"]]
        index = next(i for i, o in enumerate(question["options"]) if o.get("correct"))
        plan.append(rank_of(lengths[index], lengths))

    # Fill the quota in two stages. Questions already holding a rank that still needs members keep it, which
    # is free and stops a balanced file being rearranged. The rest are placed one at a time, each into the
    # rank that still has room and costs least to reach.
    #
    # Assigning them in rank order instead was a real defect: an over-quota "longest" group was handed out to
    # ranks 0, 1, 2, 3 in sequence, so most went to "shortest" regardless of how much padding that needed.
    # Measured on fullstack-developer/01.id.json the level came out at 10% longest -- technically inside the
    # accepted band, but 2 of 21 against a fair 5, which is a worse distortion than the 38% it replaced.
    remaining = dict(quota)
    for rank in range(4):
        holders = [i for i, r in enumerate(plan) if r == rank]
        keep = min(len(holders), remaining[rank])
        remaining[rank] -= keep
        for i in holders[keep:]:
            plan[i] = -1

    def padding_cost(question: dict, want: int) -> float:
        """Characters of padding needed to put this question's answer at rank `want`."""
        options = question["options"]
        lengths = [len(o[args.field]) for o in options]
        index = next(i for i, o in enumerate(options) if o.get("correct"))
        ceiling = int(max(lengths) * CEILING_FACTOR)
        total = 0.0
        for j, target in targets_for(lengths[index], lengths, index, want).items():
            if target > ceiling:
                return 500.0
            if target > lengths[j]:
                total += target - lengths[j]
        return total

    pending = [i for i, r in enumerate(plan) if r == -1]
    pending.sort(key=lambda i: min(padding_cost(questions[i], rank) for rank in range(4) if remaining[rank]))
    for i in pending:
        available = [rank for rank in range(4) if remaining[rank] > 0]
        if not available:
            plan[i] = 3
            continue
        chosen = min(available, key=lambda rank: (padding_cost(questions[i], rank), rank))
        plan[i] = chosen
        remaining[chosen] -= 1

    changed = 0
    for question, want in zip(questions, plan):
        options = question["options"]
        lengths = [len(o[args.field]) for o in options]
        index = next(i for i, o in enumerate(options) if o.get("correct"))
        current = rank_of(lengths[index], lengths)
        if current == want:
            continue
        ceiling = int(max(lengths) * CEILING_FACTOR)
        targets = targets_for(lengths[index], lengths, index, want)
        for j, target in targets.items():
            if target > lengths[j]:
                options[j][args.field] = minimal_pad(options[j][args.field], target)
        changed += 1

    after = Counter()
    for question in questions:
        lengths = [len(o[args.field]) for o in question["options"]]
        index = next(i for i, o in enumerate(question["options"]) if o.get("correct"))
        after[rank_of(lengths[index], lengths)] += 1

    def describe(counter: Counter) -> str:
        return "  ".join(f"{RANK_NAMES[r]}:{counter.get(r, 0)}" for r in range(4))

    print(f"  {args.path.name}")
    print(f"    before: {describe(before)}")
    print(f"    after : {describe(after)}    ({changed} question(s) adjusted)")
    if args.report:
        return 0
    if not args.write:
        print("    dry run: nothing written (pass --write)")
        return 0
    args.path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                         encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
