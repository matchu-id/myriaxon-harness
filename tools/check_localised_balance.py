#!/usr/bin/env python3
"""Check whether translation broke the length balance the English content was built to have.

`balance_lengths.py` padded the English options so the correct answer is longest about a quarter of the time
rather than always. Translation does not preserve that property: Indonesian renders the same idea in a
different number of characters, and the effect is not random. A correct answer typically states a complete
causal reason while a distractor states a short wrong mechanism, so translating both moves the answer and the
distractors by different amounts.

Nothing else in the pipeline checks this after translation, so a localised level can be guessable even though
its English source is not, and the app would serve an Indonesian user a question whose correct answer is
conspicuously the longest. Measured over the first eleven translated levels the aggregate looked fine -- 26%
against the English content's 22% -- while `data-scientist/01.id.json` sat at **52% longest**. The aggregate
hid the defect, which is why this reports per level.

Note on how this file was nearly lost: an earlier version was edited with PowerShell's
`Get-Content | Set-Content -Encoding UTF8`, which re-encoded it as cp1252 and turned the workspace path in
`ROOT` into mojibake, so the tool silently matched no files and reported success. Any write to a UTF-8 source
file on this machine goes through Python with `encoding="utf-8"`, as everywhere else in this project.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(r"C:\Users\arian\OneDrive\ドキュメント\Myriaxon")
TRACKS = ("ai-ml-engineer", "fullstack-developer", "data-scientist")
FAIR = 0.25

# What "not identifiable by length" has to mean, stated precisely.
#
# The objective is that a learner cannot use length to find the answer, which rules out the answer being
# reliably longest -- that is the exploitable direction, and 35% is already generous against a fair 25%. The
# opposite direction, the answer reliably *not* longest, is a much weaker tell, since it names three options
# rather than one, and it is not something translation controls: padding can only lengthen options, and
# nothing can shorten a faithful translation.
#
# `fullstack-developer/01.id.json` sits at 10% with ranks 2/3/9/7 -- a balanced spread weighted toward "3rd",
# and its English source was at 5%, so translation improved it. Forcing it to 25% would mean padding the
# correct answer upward to look longest a quarter of the time purely to satisfy a checker, which adds words
# without improving the question. The floor is therefore 8%: low enough to admit a genuinely balanced level,
# high enough to catch one where the answer is never longest at all.
FLOOR = 0.08
CEILING = 0.35


def rates(level: dict, field: str) -> tuple[int, int, int]:
    """(questions, answer-is-longest, answer-is-shortest) over questions with four options."""
    questions = longest = shortest = 0
    for question in level.get("questions", []):
        options = question.get("options") or []
        if len(options) != 4:
            continue
        try:
            correct = next(i for i, option in enumerate(options) if option.get("correct"))
        except StopIteration:
            continue
        lengths = [len(str(option.get(field, ""))) for option in options]
        questions += 1
        if lengths[correct] == max(lengths):
            longest += 1
        if lengths[correct] == min(lengths):
            shortest += 1
    return questions, longest, shortest


def ranks(level: dict, field: str) -> dict[int, int]:
    """How many questions put the answer at each length rank, 0 being longest."""
    counts = {0: 0, 1: 0, 2: 0, 3: 0}
    for question in level.get("questions", []):
        options = question.get("options") or []
        if len(options) != 4:
            continue
        try:
            correct = next(i for i, option in enumerate(options) if option.get("correct"))
        except StopIteration:
            continue
        lengths = [len(str(option.get(field, ""))) for option in options]
        counts[sorted(lengths, reverse=True).index(lengths[correct])] += 1
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare answer-length balance before and after translation.")
    parser.add_argument("--track", action="append", dest="tracks")
    parser.add_argument("--show-ranks", action="store_true")
    args = parser.parse_args()

    tracks = args.tracks or list(TRACKS)
    problems: list[str] = []
    total_q = total_en = total_id = 0

    print(f"  {'level':<34} {'n':>4} {'EN long':>8} {'ID long':>8}  ranks (longest..shortest)  verdict")
    for track in tracks:
        levels = ROOT / "certivo-rework" / track / "levels"
        for produced in sorted(levels.glob("*.id.json")):
            source = levels / produced.name.replace(".id.json", ".json")
            if not source.exists():
                continue
            english = json.loads(source.read_text(encoding="utf-8"))
            translated = json.loads(produced.read_text(encoding="utf-8"))
            n, en_long, _ = rates(english, "text")
            _, id_long, _ = rates(translated, "textId")
            if n == 0:
                continue
            en_rate = en_long / n
            id_rate = id_long / n
            total_q += n
            total_en += en_long
            total_id += id_long
            verdict = "ok"
            if not FLOOR <= id_rate <= CEILING:
                verdict = "GUESSABLE" if id_rate > CEILING else "inverted"
                problems.append(f"{track}/{produced.name}: {id_rate:.0%} longest")
            spread = ranks(translated, "textId")
            shape = "/".join(str(spread[r]) for r in range(4))
            print(f"  {track + '/' + produced.stem:<34} {n:>4} {en_rate:>7.0%} {id_rate:>7.0%}  "
                  f"{shape:<25} {verdict}")

    if total_q:
        print()
        print(f"  English   : {total_en}/{total_q} longest ({100 * total_en / total_q:.1f}%)")
        print(f"  Indonesian: {total_id}/{total_q} longest ({100 * total_id / total_q:.1f}%)")
        print(f"  fair share is {FAIR:.0%}; accepted band {FLOOR:.0%}-{CEILING:.0%}")
    if problems:
        print()
        print(f"  {len(problems)} level(s) need re-balancing after translation:")
        for name in problems:
            print(f"    - {name}")
        return 1
    print()
    print("  translation has not made the correct answer identifiable by length")
    return 0


if __name__ == "__main__":
    sys.exit(main())
