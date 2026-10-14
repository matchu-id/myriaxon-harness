#!/usr/bin/env python3
"""Audit a Certivo track against the rework brief.

Reports, per level: question count, kind breakdown (options / code / essay), how many options each
multiple-choice question offers, and the three measurable ways a multiple-choice question can be
guessed without knowing the answer:

  * the correct option is the LONGEST of the set;
  * the correct option is the SHORTEST;
  * the correct answer always sits in the same position.

The `kind` of a question is determined by which key it carries -- `options`, `code` or `essay` -- not
by a `kind` field, which the files do not have.

Usage:  python3 audit_track_rules.py <track-dir> [--essay-from N]
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

LETTERS = "ABCDEFGH"


def kind_of(question: dict) -> str:
    if "options" in question:
        return "multiple"
    if "code" in question:
        return "code"
    if "essay" in question:
        return "essay"
    return "?"


def correct_index(question: dict) -> int | None:
    options = question.get("options") or []
    for i, option in enumerate(options):
        if isinstance(option, dict) and option.get("correct") is True:
            return i
    if isinstance(question.get("answerIndex"), int):
        return question["answerIndex"]
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("track")
    parser.add_argument("--essay-from", type=int, default=11,
                        help="first level allowed to contain essay questions")
    args = parser.parse_args()

    track = Path(args.track)
    # `NN.id.json` files hold the Indonesian translation of the same level and sit in the same directory. The
    # first localisation rollout added twenty of them and this glob picked them up: `int("18.id")` raised
    # nothing because `path.stem` is `18.id`, and the level number it parsed came out below 11, so the audit
    # reported "levels/18.id.json has 3 essay(s) but level < 11" for every essay level in the track.
    levels = sorted(p for p in track.glob("levels/*.json") if not p.name.endswith(".id.json"))
    if not levels:
        print(f"no level files under {track}")
        return 2

    print(f"=== {track.name} — {len(levels)} level(s), essays allowed from level {args.essay_from} ===\n")
    print(f"  {'lvl':>4} {'total':>6} {'mc':>4} {'code':>5} {'essay':>6} {'opts':>5} "
          f"{'ansPos':>12} {'long%':>6} {'short%':>7}  {'verdict'}")

    total_q = 0
    problems: list[str] = []
    overall_pos = Counter()
    overall_long = overall_short = overall_graded = 0

    for path in levels:
        data = json.loads(path.read_text(encoding="utf-8"))
        questions = data.get("questions") or []
        try:
            level_no = int(path.stem)
        except ValueError:
            level_no = 0

        counts = Counter(kind_of(q) for q in questions)
        option_counts = Counter(
            len(q.get("options") or []) for q in questions if kind_of(q) == "multiple"
        )

        positions = Counter()
        long_hit = short_hit = graded = 0
        for question in questions:
            if kind_of(question) != "multiple":
                continue
            index = correct_index(question)
            options = question.get("options") or []
            if index is None or not (0 <= index < len(options)):
                continue
            graded += 1
            positions[index] += 1
            lengths = [len(str((o or {}).get("text", ""))) for o in options]
            if lengths[index] == max(lengths) and lengths.count(max(lengths)) == 1:
                long_hit += 1
            if lengths[index] == min(lengths) and lengths.count(min(lengths)) == 1:
                short_hit += 1

        overall_pos.update(positions)
        overall_long += long_hit
        overall_short += short_hit
        overall_graded += graded
        total_q += len(questions)

        pct_long = 100.0 * long_hit / graded if graded else 0.0
        pct_short = 100.0 * short_hit / graded if graded else 0.0
        spread = "".join(f"{LETTERS[i]}:{positions.get(i, 0)} " for i in range(4)).strip()

        verdict = []
        if counts.get("essay") and level_no < args.essay_from:
            verdict.append(f"ESSAY NOT ALLOWED at level {level_no}")
            problems.append(f"levels/{path.stem}.json has {counts['essay']} essay(s) but level < {args.essay_from}")
        if len(questions) != 25:
            verdict.append(f"{len(questions)}q not 25")
            problems.append(f"levels/{path.stem}.json has {len(questions)} questions, expected 25")
        if graded and pct_long > 40:
            verdict.append("length gives it away")
            problems.append(f"levels/{path.stem}.json: correct option is longest on {pct_long:.0f}%")
        if graded and pct_short > 40:
            verdict.append("shortness gives it away")
        worst = max(positions.values()) if positions else 0
        if graded and worst > graded * 2 / 4:
            verdict.append("position clustered")
            problems.append(f"levels/{path.stem}.json: one answer position holds {worst}/{graded}")

        print(f"  {level_no:>4} {len(questions):>6} {counts.get('multiple',0):>4} {counts.get('code',0):>5} "
              f"{counts.get('essay',0):>6} {str(dict(option_counts)):>5} {spread:>12} "
              f"{pct_long:>5.0f}% {pct_short:>6.0f}%  {'; '.join(verdict) or 'ok'}")

    print(f"\n  totals: {total_q} question(s)")
    if overall_graded:
        spread = "  ".join(f"{LETTERS[i]}={overall_pos.get(i, 0)}" for i in range(4))
        print(f"  answer position across the track: {spread}")
        print(f"  correct is LONGEST : {overall_long}/{overall_graded} "
              f"({100.0*overall_long/overall_graded:.1f}%)   target ~25%")
        print(f"  correct is SHORTEST: {overall_short}/{overall_graded} "
              f"({100.0*overall_short/overall_graded:.1f}%)   target ~25%")

    print()
    if problems:
        print(f"  {len(problems)} problem(s) to fix:")
        for item in problems[:40]:
            print(f"    - {item}")
        if len(problems) > 40:
            print(f"    ... and {len(problems) - 40} more")
    else:
        print("  no problems found")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
