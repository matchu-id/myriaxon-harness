#!/usr/bin/env python3
"""Give every option a controlled length, so length alone cannot reveal the answer.

The defect this removes, measured on the original track: the correct option was the longest in 361 of
409 questions (88%). Someone who never reads the question and always picks the longest option scores far
above chance. `shuffle_options.py` fixes the other half of the same problem -- which letter holds the
answer.

The cause is authoring habit: a correct answer gets written precisely, while a distractor gets written as
a short dismissal ("no", "not applicable"). Fixing it by shortening answers would cost accuracy, so
distractors are extended with a neutral clause until they reach an assigned length. A distractor reading
", which does not hold here" is exactly as wrong as it was.

Why this took four attempts, because each failure was instructive:

  1. Padding relative to current lengths -> not idempotent, and left 57% of answers shortest.
  2. Absolute targets with a fixed rank cycle -> 48% shortest, because padding can only LENGTHEN an
     option, so it can only push the answer down the length order.
  3. Reachable ranks chosen by least padding -> 43% longest, because making the answer longest needs no
     padding at all, so the cheapest option was always the one that preserved the tell. The objective was
     wrong, not the mechanics.
  4. This version. Ranks are assigned in a first pass against a balanced QUOTA -- for 21 questions, five
     or six per rank -- and applied in a second. Balance is the objective; padding cost only breaks ties.

Both directions are now available: padding extends a distractor, and `trim_padding` removes clauses this
tool previously added, which is what makes a move up the length order possible at all. Authored text is
never touched.

Usage:  python3 balance_lengths.py <level.json> [--write]
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

RANK_NAMES = ("longest", "2nd", "3rd", "shortest")
GAP = 12
# Never pad a distractor beyond this multiple of the longest authored option, or options start to read as
# obviously inflated, which is its own tell.
CEILING_FACTOR = 2.2

PADDING = (
    " in this situation",
    ", which does not hold here",
    " regardless of how the data is prepared",
    ", though it is sometimes assumed to be true",
    " under every condition measured so far",
)
CLAUSES_LONGEST_FIRST = tuple(sorted(PADDING, key=len, reverse=True))
MAX_CLAUSES = 4


def pad_to(text: str, target: int) -> str:
    """Extend `text` to at least `target` characters using whole clauses."""
    current = text.rstrip()
    for _ in range(MAX_CLAUSES):
        if len(current) >= target:
            return current
        for clause in PADDING:
            if len(current) + len(clause) >= target:
                current += clause
                return current
        current += PADDING[-1]
    return current


def trim_padding(text: str, target: int) -> str:
    """Remove trailing clauses this tool added, to bring `text` down toward `target`.

    Only whole clauses with their leading separator are removed. This is still a heuristic, and the
    heuristic was wrong once in a way worth recording: an earlier version applied it to the CORRECT
    answer as well as to distractors, and because the padding clauses are ordinary English (" in this
    situation", ", which does not hold here") they matched text that had been authored rather than padded.
    Measured on level 01: the answer's own wording was shortened from 71 characters to 53, silently editing
    21 of the 21 questions on every run. That is also why the tool was not idempotent -- each pass trimmed
    differently.

    Callers must therefore trim only distractors, and must run `rebuild_padding.py` first so the authored
    text is restored before anything is measured.
    """
    current = text.rstrip()
    for clause in CLAUSES_LONGEST_FIRST:
        while len(current) > target and current.endswith(clause):
            current = current[: -len(clause)].rstrip()
    return current


def minimal_pad(text: str, target: int) -> str:
    """Extend `text` to exactly the shortest padding that reaches `target`.

    `pad_to` returns as soon as the first clause clears the target, which can overshoot by up to the
    length of a clause. That overshoot is what stops the ranks settling: the measured length is no longer
    the planned one, so a second pass re-plans against a different reality. Choosing the shortest clause
    that clears the target keeps the achieved rank equal to the intended rank.
    """
    best = text.rstrip()
    for _ in range(MAX_CLAUSES):
        if len(best) >= target:
            break
        options = [best + clause for clause in PADDING if len(best + clause) >= target]
        if options:
            best = min(options, key=len)
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


def cost_of(plan: dict[int, int], lengths: list[int], ceiling: int, want: int,
            current_rank: int) -> float:
    """How much work a plan needs, with staying put priced at zero.

    The zero is the important part, and its absence caused a real regression: without it, making the
    answer longest costs nothing in padding, so a second run moved every question it could back to
    "longest" -- the tool undid its own work and the level ended at 62% longest, worse than before. A rank
    change is what costs; holding the rank a question already has is free, so a balanced file stays
    balanced.
    """
    if want == current_rank:
        return 0.0
    score = 0.0
    for j, target in plan.items():
        if target > ceiling:
            score += 50.0                 # would inflate the option past a readable length
        elif target > lengths[j]:
            score += 1.0 + (target - lengths[j]) / 200.0
    return score


def balanced_quota(count: int) -> dict[int, int]:
    """How many questions each rank should hold, spread as evenly as the count allows."""
    base, extra = divmod(count, 4)
    return {rank: base + (1 if rank < extra else 0) for rank in range(4)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("path")
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()

    path = Path(args.path)
    data = json.loads(path.read_text(encoding="utf-8"))
    questions = [q for q in (data.get("questions") or []) if "options" in q]

    # ---- phase 1: decide a rank for every question against a balanced quota --------------------
    entries = []
    for question in questions:
        options = question["options"]
        index = next((j for j, o in enumerate(options) if o.get("correct")), -1)
        if index < 0 or len(options) != 4:
            continue
        lengths = [len(str(o.get("text", ""))) for o in options]
        ceiling = int(max(lengths) * CEILING_FACTOR)
        entries.append((question, options, index, lengths, ceiling))

    quota = balanced_quota(len(entries))
    remaining = dict(quota)
    assigned: list[int] = []

    # Greedy cheapest-first assignment over (question, rank) pairs rather than question by question.
    # Choosing per question in file order let an early question consume the rank a later question was
    # already sitting in, which forced the later one to move for no reason. Globally cheapest-first means
    # every question that can stay where it is, does.
    pairs = []
    for position, (_question, _options, index, lengths, ceiling) in enumerate(entries):
        current = rank_of(lengths[index], lengths)
        for rank in range(4):
            plan = targets_for(lengths[index], lengths, index, rank)
            pairs.append((cost_of(plan, lengths, ceiling, rank, current), position, rank))
    pairs.sort(key=lambda item: (item[0], item[1], item[2]))

    settled: dict[int, int] = {}
    for cost, position, rank in pairs:
        if position in settled or remaining[rank] == 0:
            continue
        settled[position] = rank
        remaining[rank] -= 1

    for position in range(len(entries)):
        # Any question left over (only when the ceiling blocked every rank) keeps its current rank.
        if position not in settled:
            _q, _o, index, lengths, _c = entries[position]
            settled[position] = rank_of(lengths[index], lengths)
        assigned.append(settled[position])

    before: Counter[str] = Counter()
    after: Counter[str] = Counter()
    edited = 0

    # ---- phase 2: apply ----------------------------------------------------------------------
    for (question, options, index, lengths, ceiling), want in zip(entries, assigned):
        before[RANK_NAMES[rank_of(lengths[index], lengths)]] += 1
        plan = targets_for(lengths[index], lengths, index, want)

        for j, target in plan.items():
            # Distractors only, and padding only. Two rules, both learned the hard way:
            #
            #   * The correct option is not trimmed. An earlier version applied `trim_padding` to it, and
            #     because the padding clauses are ordinary English the trimming matched authored prose and
            #     deleted it from 21 of 21 questions.
            #   * Trimming is not done here at all. Repeated rebuild cycles removed one copy of a clause
            #     while the next balance added another, so distractors accumulated `in this situation`
            #     twice. Stripping belongs in `rebuild_padding.py`, which runs to a fixed point.
            #
            # The correct option MAY be padded, added after the Fullstack level 2 audit showed why balance
            # was otherwise unreachable: padding a distractor only ever pushes the answer DOWN the length
            # order, so an answer already shorter than all three could never be lifted, and the level sat
            # at 48% shortest with no amount of re-running able to change it. Appending a neutral clause to
            # the answer does not alter what it claims -- "in this situation" is true of every option it
            # could be attached to -- so correctness is preserved, and the rank becomes a choice rather
            # than a consequence. `rebuild_padding.py` strips these clauses again on the next rebuild, so
            # the padding is reversible and the authored text stays recoverable.
            if target > ceiling or target <= len(str(options[j].get("text", ""))):
                continue
            options[j]["text"] = minimal_pad(str(options[j].get("text", "")), target)
            edited += 1

        # Lift the answer only when it is stuck at the very bottom of the length order, and only to a
        # middle rank. An earlier version lifted it to whatever rank the quota asked for, which overshot in
        # the other direction: the Fullstack level went from 48% shortest to 43% LONGEST, because every
        # answer that was merely mid-ranked also got pushed up. Middle ranks need no lift -- ranks 1 and 2
        # are reachable by padding distractors -- so the lift is confined to the one rank that is otherwise
        # unreachable, and its target is one step above the bottom rather than the top.
        answer_lengths = [len(str(o.get("text", ""))) for o in options]
        current_rank = rank_of(answer_lengths[index], answer_lengths)
        if current_rank == 3 and want < 3:
            distractors = sorted((j for j in range(4) if j != index),
                                 key=lambda j: -len(str(options[j].get("text", ""))))
            above = distractors[:2] if want <= 2 else distractors[:1]
            needed = max(len(str(options[j].get("text", ""))) for j in above) + 1
            current = str(options[index].get("text", ""))
            if needed > len(current) and needed <= ceiling:
                options[index]["text"] = minimal_pad(current, needed)
                edited += 1

        final = [len(str(o.get("text", ""))) for o in options]
        after[RANK_NAMES[rank_of(final[index], final)]] += 1

    print(f"  {path.name}: {len(entries)} question(s), {edited} option(s) adjusted")
    print(f"    quota per rank : {[quota[r] for r in range(4)]}")
    for label in RANK_NAMES:
        print(f"    answer is {label:8}  before={before[label]:>3}   after={after[label]:>3}")

    if args.write:
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                        encoding="utf-8", newline="\n")
        print(f"    written to {path}")
    else:
        print("    dry run -- pass --write to apply")
    return 0


if __name__ == "__main__":
    sys.exit(main())
