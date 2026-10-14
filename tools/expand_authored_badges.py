#!/usr/bin/env python3
"""Extend `content/badges.json` with additional rungs, taking the authored catalogue from 27 to 100.

Every entry added here uses a criteria type that already exists in BADGES.md. No new type, no new
`criteriaValue` key, and no change to the 27 entries already in the file — rule 5 of the design forbids
renumbering, and the existing entries keep their ids, `criteriaType`, `criteriaValue` and `sortOrder` byte for
byte.

The thresholds were chosen against the data rather than picked to look tidy:

* `speed_pass` — measured attempts show a 203 s pass against a 1440 s limit (fraction 0.14) and a 600 s attempt
  at 0.42, so rungs at 0.45 / 0.40 / 0.30 / 0.20 / 0.15 / 0.10 all sit inside the achievable range without
  crowding one another.
* `code_tests_perfect` — the authored code questions carry 1–6 tests, so the ladder stops at 6. Agent A had
  already found that `minTests: 12` was unreachable.
* `perfect_score` — 10, 25, 50 rather than a rung per 10: a perfect score is rare enough that a finer ladder
  would put several rungs within a single learner's reach at once.
* `skill_count_at_star` — the `skill_star` family covers one skill, so the count rungs start at 3; a rung at 1
  would duplicate `adept`, and at 2 would be trivially satisfied beside it.

`sort_order` continues from the existing maximum of 223. Level badges own the 400+ band and graduates own
100 + track sort order, so the authored band stays below 400 and cannot collide.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys

HOST = "admin-certivo@139.190.97.11"
APP = "certivo-app-1"
TARGET = "/srv/certivo/content/badges.json"

# (id, name, emoji, criteriaType, criteriaValue, description)
NEW: list[tuple[str, str, str, str, dict, str]] = [
    # ---- perfect_score: cumulative, matching the existing sharpshooter/flawless-five/flawless-ten ladder
    ("flawless-twenty-five", "Flawless Twenty-Five", "💫", "perfect_score", {"count": 25},
     "Score 100% on twenty-five exams."),
    ("flawless-fifty", "Flawless Fifty", "🌠", "perfect_score", {"count": 50},
     "Score 100% on fifty exams."),

    # ---- attempts_to_pass: 1, 2, 3, 5, 10 exist; add the middle and the generous rungs
    ("steadfast", "Steadfast", "🧭", "attempts_to_pass", {"maxAttempts": 4},
     "Pass an exam within four attempts."),
    ("unyielding", "Unyielding", "⛰️", "attempts_to_pass", {"maxAttempts": 7},
     "Pass an exam within seven attempts."),
    ("relentless", "Relentless", "🔥", "attempts_to_pass", {"maxAttempts": 15},
     "Pass an exam within fifteen attempts."),

    # ---- speed_pass: 0.5 and 0.34 and 0.2 exist; fill the ladder and push the fast end
    ("sharp-shooter-clock", "Against the Clock", "🕐", "speed_pass", {"maxFraction": 0.45},
     "Pass an exam using less than 45% of its time limit."),
    ("fast-learner", "Fast Learner", "🏃", "speed_pass", {"maxFraction": 0.4},
     "Pass an exam using less than 40% of its time limit."),
    ("swift", "Swift", "🌪️", "speed_pass", {"maxFraction": 0.3},
     "Pass an exam using less than 30% of its time limit."),
    ("lightning", "Lightning", "⚡", "speed_pass", {"maxFraction": 0.15},
     "Pass an exam using less than 15% of its time limit."),
    ("instant", "Instant", "💥", "speed_pass", {"maxFraction": 0.1},
     "Pass an exam using less than 10% of its time limit."),

    # ---- first_try_pass: cumulative repeats rather than a threshold, so the count comes from the streak family
    #      instead; see level_streak below. Nothing added here.

    # ---- level_streak: 5 and 10 exist; add the ends and the middle
    ("clean-run-three", "Clean Run", "🎽", "level_streak", {"count": 3},
     "Pass three consecutive levels on the first attempt."),
    ("clean-run-fifteen", "Clean Run Fifteen", "🧱", "level_streak", {"count": 15},
     "Pass fifteen consecutive levels on the first attempt."),
    ("unbroken-twenty", "Unbroken", "💎", "level_streak", {"count": 20},
     "Pass twenty consecutive levels on the first attempt."),
    ("unbroken-thirty", "Unbroken Thirty", "🗿", "level_streak", {"count": 30},
     "Pass thirty consecutive levels on the first attempt."),

    # ---- skill_star: 5, 8, 10 exist; fill between and add a low rung for early progress
    ("apprentice", "Apprentice", "🌱", "skill_star", {"stars": 3},
     "Reach 3 stars in any skill."),
    ("journeyman", "Journeyman", "🔧", "skill_star", {"stars": 6},
     "Reach 6 stars in any skill."),
    ("veteran", "Veteran", "🎖️", "skill_star", {"stars": 7},
     "Reach 7 stars in any skill."),
    ("sage", "Sage", "🦉", "skill_star", {"stars": 9},
     "Reach 9 stars in any skill."),

    # ---- skill_count_at_star: 10-at-10 and 3-at-10 exist; breadth at lower rungs
    ("well-rounded", "Well Rounded", "🧩", "skill_count_at_star", {"stars": 5, "count": 3},
     "Reach 5 stars in three different skills."),
    ("broad", "Broad", "🌊", "skill_count_at_star", {"stars": 5, "count": 6},
     "Reach 5 stars in six different skills."),
    ("deep", "Deep", "🏔️", "skill_count_at_star", {"stars": 8, "count": 5},
     "Reach 8 stars in five different skills."),
    ("authority", "Authority", "📚", "skill_count_at_star", {"stars": 8, "count": 10},
     "Reach 8 stars in ten different skills."),
    ("luminary", "Luminary", "🔆", "skill_count_at_star", {"stars": 9, "count": 5},
     "Reach 9 stars in five different skills."),
    ("paragon", "Paragon", "🏛️", "skill_count_at_star", {"stars": 10, "count": 5},
     "Reach 10 stars in five different skills."),
    ("legend", "Legend", "🐉", "skill_count_at_star", {"stars": 10, "count": 20},
     "Reach 10 stars in twenty different skills."),

    # ---- levels_completed: 10 and 25 exist; fill the ladder
    ("starter", "Starter", "🚩", "levels_completed", {"count": 3},
     "Pass three levels."),
    ("getting-going", "Getting Going", "🚶", "levels_completed", {"count": 5},
     "Pass five levels."),
    ("half-century", "Half Century", "🏏", "levels_completed", {"count": 50},
     "Pass fifty levels."),
    ("centurion", "Centurion", "💯", "levels_completed", {"count": 100},
     "Pass one hundred levels."),
    ("two-hundred", "Two Hundred", "🌟", "levels_completed", {"count": 200},
     "Pass two hundred levels."),

    # ---- tracks_completed: 2 and 5 exist; add depth
    ("triple-threat", "Triple Threat", "🎯", "tracks_completed", {"count": 3},
     "Complete three tracks."),
    ("decathlete", "Decathlete", "🏅", "tracks_completed", {"count": 10},
     "Complete ten tracks."),

    # ---- code_tests_perfect: 1, 5, 6 exist; fill the middle of a 1..6 range
    ("green-two", "Two Green", "🍀", "code_tests_perfect", {"minTests": 2},
     "Pass every test in a code question carrying at least two tests."),
    ("green-three", "Three Green", "🌿", "code_tests_perfect", {"minTests": 3},
     "Pass every test in a code question carrying at least three tests."),
    ("green-four", "Four Green", "🌳", "code_tests_perfect", {"minTests": 4},
     "Pass every test in a code question carrying at least four tests."),
]


def ssh(command: str, stdin: bytes | None = None, timeout: int = 600) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=25", HOST, command],
        input=stdin, capture_output=True, timeout=timeout,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Extend the authored badge catalogue to 100.")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    raw = (ssh(f"cat {TARGET}").stdout or b"").decode("utf-8", "replace")
    if not raw.strip():
        print(f"  could not read {TARGET}")
        return 1
    existing = json.loads(raw)
    print(f"  existing: {len(existing)} entries, max sortOrder {max(b['sortOrder'] for b in existing)}")

    ids = {b["id"] for b in existing}
    duplicates = [entry[0] for entry in NEW if entry[0] in ids]
    if duplicates:
        print(f"  REFUSING: id(s) already present: {', '.join(duplicates)}")
        return 1
    seen: set[str] = set()
    for entry in NEW:
        if entry[0] in seen:
            print(f"  REFUSING: {entry[0]} appears twice in this script")
            return 1
        seen.add(entry[0])

    base = max(b["sortOrder"] for b in existing) + 10
    added = []
    for offset, (badge_id, name, emoji, criteria_type, value, description) in enumerate(NEW):
        added.append({
            "id": badge_id,
            "name": name,
            "description": description,
            "iconEmoji": emoji,
            "criteriaType": criteria_type,
            "criteriaValue": value,
            "sortOrder": base + offset,
        })

    merged = existing + added
    total = len(merged)
    print(f"  adding {len(added)}; result {total} authored badges, sortOrder {base}..{base + len(added) - 1}")
    if total != 100:
        print(f"  NOTE: the result is {total}, not 100")

    # Level badges own the 400+ band and graduates 100+track sort order, so the authored band must stay clear.
    if any(b["sortOrder"] >= 400 for b in merged if b["id"] in seen):
        print("  REFUSING: an authored badge would reach into the level-badge band")
        return 1

    counts: dict[str, int] = {}
    for badge in added:
        counts[badge["criteriaType"]] = counts.get(badge["criteriaType"], 0) + 1
    print("  new rungs by criteria type:")
    for criteria_type, count in sorted(counts.items()):
        print(f"    {criteria_type:<22} +{count}")

    if args.dry_run:
        print("  dry run: nothing written")
        return 0

    ssh(f"cp -a {TARGET} {TARGET}.before-100")
    text = json.dumps(merged, indent=2, ensure_ascii=False) + "\n"
    if ssh(f"cat > {TARGET}", text.encode("utf-8")).returncode != 0:
        print("  write failed")
        return 1

    verify = (ssh(f"python3 -c \"import json;d=json.load(open('{TARGET}',encoding='utf-8'));"
                  f"print(len(d))\" ").stdout or b"").decode().strip()
    print(f"  written; the file now parses with {verify} entries")
    return 0 if verify == str(total) else 1


if __name__ == "__main__":
    sys.exit(main())
