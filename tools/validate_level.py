#!/usr/bin/env python3
"""Validate one reworked Certivo level against every rule, before it goes near the VPS.

Checks the schema rules that the repo's own Zod validator enforces, plus the content rules that make a
multiple-choice question unguessable. Catching a broken file here costs nothing; catching it after it
has replaced a level on the server costs a restore.

Usage:  python3 validate_level.py <level.json> [--expect-code N] [--no-essay]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

LETTERS = "ABCD"
SKILL_SLUG = re.compile(r"^[a-z0-9-]+$")


def kind_of(q: dict) -> str:
    if "options" in q:
        return "multiple"
    if "code" in q:
        return "code"
    if "essay" in q:
        return "essay"
    return "?"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("path")
    parser.add_argument("--expect-total", type=int, default=25)
    parser.add_argument("--no-essay", action="store_true", help="fail if any essay question is present")
    parser.add_argument("--no-mixed-language", action="store_true",
                        help="fail on Indonesian words inside an English level")
    args = parser.parse_args()

    path = Path(args.path)
    problems: list[str] = []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        print(f"  NOT VALID JSON: {exc}")
        return 1

    # ---- top level ----------------------------------------------------------------
    for key in ("levelNumber", "exam", "caseStudy", "references", "questions"):
        if key not in data:
            problems.append(f"missing top-level key '{key}'")
    exam = data.get("exam") or {}
    for key in ("passThresholdPct", "timeLimitSeconds", "questionsPerAttempt"):
        if key not in exam:
            problems.append(f"exam is missing '{key}'")
    case = data.get("caseStudy") or {}
    if not case.get("title") or not case.get("scenarioMarkdown"):
        problems.append("caseStudy needs both title and scenarioMarkdown")
    if len(data.get("references") or []) < 1:
        problems.append("at least one reference is required")
    for ref in data.get("references") or []:
        if not str(ref.get("url", "")).startswith(("http://", "https://")):
            problems.append(f"reference url is not absolute: {ref.get('url')!r}")
        if ref.get("resourceType") not in ("open_source", "closed_source", "concept"):
            problems.append(f"reference resourceType invalid: {ref.get('resourceType')!r}")

    questions = data.get("questions") or []
    counts = Counter(kind_of(q) for q in questions)

    # ---- length and quotas --------------------------------------------------------
    if len(questions) != args.expect_total:
        problems.append(f"{len(questions)} questions, expected {args.expect_total}")
    if counts.get("?") :
        problems.append(f"{counts['?']} question(s) have none of options/code/essay")
    if args.no_essay and counts.get("essay"):
        problems.append(f"{counts['essay']} essay question(s) present but this level must have none")

    code_count = exam.get("codeCount")
    essay_count = exam.get("essayCount")
    if (code_count is None) != (essay_count is None):
        problems.append("exam.codeCount and exam.essayCount must both be set or both omitted")
    if code_count is not None and essay_count is not None:
        if counts.get("code", 0) < code_count:
            problems.append(f"codeCount={code_count} but only {counts.get('code',0)} code questions authored")
        if counts.get("essay", 0) < essay_count:
            problems.append(f"essayCount={essay_count} but only {counts.get('essay',0)} essay questions authored")
        if code_count + essay_count > exam.get("questionsPerAttempt", 0):
            problems.append(f"codeCount+essayCount exceeds questionsPerAttempt")
        need_other = exam["questionsPerAttempt"] - code_count - essay_count
        have_other = len(questions) - counts.get("code", 0) - counts.get("essay", 0)
        if have_other < need_other:
            problems.append(f"need {need_other} multiple-choice slots but only {have_other} authored")

    # ---- per question -------------------------------------------------------------
    positions: Counter[int] = Counter()
    long_hits = short_hits = graded = 0
    for i, q in enumerate(questions, 1):
        kind = kind_of(q)
        if not SKILL_SLUG.match(str(q.get("skill", ""))):
            problems.append(f"q{i}: skill slug invalid: {q.get('skill')!r}")
        if not q.get("prompt"):
            problems.append(f"q{i}: empty prompt")
        if not q.get("explanation"):
            problems.append(f"q{i}: empty explanation")

        if kind == "multiple":
            options = q["options"]
            if len(options) != 4:
                problems.append(f"q{i}: {len(options)} options, schema requires exactly 4")
                continue
            correct = [j for j, o in enumerate(options) if o.get("correct") is True]
            if len(correct) != 1:
                problems.append(f"q{i}: {len(correct)} correct options, schema requires exactly 1")
                continue
            for j, o in enumerate(options):
                if not str(o.get("text", "")).strip():
                    problems.append(f"q{i}: option {LETTERS[j]} is empty")
            index = correct[0]
            positions[index] += 1
            graded += 1
            lengths = [len(str(o.get("text", ""))) for o in options]
            if lengths[index] == max(lengths) and lengths.count(max(lengths)) == 1:
                long_hits += 1
            if lengths[index] == min(lengths) and lengths.count(min(lengths)) == 1:
                short_hits += 1
            # A visible spread is what a guessing learner exploits, so flag any large gap.
            if min(lengths) and max(lengths) / min(lengths) >= 2.2:
                problems.append(f"q{i}: option lengths {lengths} span a factor of "
                                f"{max(lengths)/min(lengths):.1f} — guessable by shape")

        elif kind == "code":
            spec = q["code"]
            language = spec.get("language")
            if language not in ("javascript", "python", "sql", "typescript"):
                problems.append(f"q{i}: code language invalid: {language!r}")
            if not spec.get("starterCode"):
                problems.append(f"q{i}: code.starterCode missing")
            if not spec.get("instructionsMarkdown"):
                problems.append(f"q{i}: code.instructionsMarkdown missing")
            if language == "sql" and not spec.get("setupSql"):
                problems.append(f"q{i}: sql code question needs setupSql")
            if language != "sql" and not spec.get("entryPoint"):
                problems.append(f"q{i}: {language} code question needs entryPoint")
            tests = spec.get("tests") or []
            if not tests:
                problems.append(f"q{i}: code question has no tests")
            ids = [t.get("id") for t in tests]
            if len(set(ids)) != len(ids):
                problems.append(f"q{i}: duplicate test ids")
            for t in tests:
                if "expected" not in t:
                    problems.append(f"q{i}: test {t.get('id')} has no expected value")
            if not any(t.get("hidden") for t in tests):
                problems.append(f"q{i}: no hidden test — every expected value is visible")

        elif kind == "essay":
            spec = q["essay"]
            if not spec.get("rubricMarkdown"):
                problems.append(f"q{i}: essay.rubricMarkdown missing")

    # ---- guessability -------------------------------------------------------------
    if graded:
        worst = max(positions.values())
        if worst > graded * 2 / 4:
            problems.append(f"answer position clustered: {dict(positions)} over {graded} question(s)")
        pct_long = 100.0 * long_hits / graded
        pct_short = 100.0 * short_hits / graded
        # The threshold is deliberately one-sided and stated here rather than buried: the failure being
        # prevented is a learner scoring well by always picking the longest (or shortest) option, and
        # that needs the tell to be reliable, not merely present. With 21 questions a quarter-share has a
        # binomial spread of roughly ten points, so anything under 40% is well within noise of a fair
        # set. A level is failed only above that, and the reported percentage is what a reviewer should
        # judge if it sits near the line.
        if pct_long > 40:
            problems.append(f"correct option is the longest on {pct_long:.0f}% of questions "
                            f"(threshold 40%; a fair set sits near 25%)")
        if pct_short > 40:
            problems.append(f"correct option is the shortest on {pct_short:.0f}% of questions "
                            f"(threshold 40%; a fair set sits near 25%)")

    # ---- language -----------------------------------------------------------------
    if args.no_mixed_language:
        blob = path.read_text(encoding="utf-8")
        # Common Indonesian function words. A false positive needs one of these to appear in English
        # prose, which none of them plausibly do.
        indonesian = ("yang", "dengan", "adalah", "untuk", "kamu", "tidak", "bisa", "harus",
                      "jawaban", "soal", "Tabel", "Kembalikan", "Tekan", "jalankan")
        hits = [w for w in indonesian if re.search(rf"\b{re.escape(w)}\b", blob)]
        if hits:
            problems.append(f"Indonesian words in an English level: {hits}")

    # ---- report -------------------------------------------------------------------
    print(f"  {path.name}: {len(questions)} questions  "
          f"mc={counts.get('multiple',0)} code={counts.get('code',0)} essay={counts.get('essay',0)}")
    if graded:
        print(f"    answer position : " + "  ".join(f"{LETTERS[i]}={positions.get(i,0)}" for i in range(4)))
        print(f"    correct longest : {long_hits}/{graded} ({100.0*long_hits/graded:.0f}%)")
        print(f"    correct shortest: {short_hits}/{graded} ({100.0*short_hits/graded:.0f}%)")
        lengths = [len(str(o.get("text", ""))) for q in questions if kind_of(q) == "multiple"
                   for o in q["options"]]
        if lengths:
            print(f"    option length   : min={min(lengths)} max={max(lengths)} "
                  f"mean={sum(lengths)/len(lengths):.0f}")
    if problems:
        print(f"    {len(problems)} PROBLEM(S):")
        for item in problems:
            print(f"      - {item}")
        return 1
    print("    ALL CHECKS PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
