#!/usr/bin/env python3
"""Second pass on the authored catalogue: close the remaining ladder gaps.

The first pass added the obvious interior rungs and landed at 62 of the target 100. This adds the rest,
choosing thresholds that are **reachable and distinct** rather than rounding the count up:

* `skill_star` — the single-skill ladder gains 1 and 2 star rungs, so early progress is visible at all. A
  one-rung-every-star ladder over 1–10 reads as a progress bar, which is what a learner at that stage wants.
* `skill_count_at_star` — combinations at 3 stars and 6 stars, which are the levels a learner actually passes
  through, plus a 25-skill rung above the existing 20.
* `levels_completed` — extends to 300 and 500. With 700 levels in the catalogue these are long-run goals
  rather than decoration, and each sits far enough from its neighbour to be its own milestone.
* `tracks_completed` — extends to 15, 20 and all 35. The top rung requires finishing the entire catalogue and
  is meant to be nearly unreachable; it is the one badge that should be.
* `perfect_score`, `speed_pass` — one further rung each, at the far end where a learner has plateaued.

Every rung states a threshold a real submission can satisfy. None is a restatement of a neighbour: each family
is monotone and the gaps are wide enough that no two rungs fire on the same exam unless the learner has just
crossed both, which is how a ladder is supposed to behave.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys

HOST = "admin-certivo@139.190.97.11"
TARGET = "/srv/certivo/content/badges.json"

NEW: list[tuple[str, str, str, str, dict, str]] = [
    # ---- perfect_score
    ("flawless-hundred", "Flawless Hundred", "🌌", "perfect_score", {"count": 100},
     "Score 100% on one hundred exams."),

    # ---- skill_star: a rung per star from 1, so early progress is visible
    ("novice", "Novice", "🌾", "skill_star", {"stars": 1},
     "Reach 1 star in any skill."),
    ("student", "Student", "📖", "skill_star", {"stars": 2},
     "Reach 2 stars in any skill."),
    ("practitioner", "Practitioner", "🛠️", "skill_star", {"stars": 4},
     "Reach 4 stars in any skill."),

    # ---- skill_count_at_star: the rungs a learner passes through, plus the far end
    ("curious", "Curious", "🔍", "skill_count_at_star", {"stars": 3, "count": 4},
     "Reach 3 stars in four different skills."),
    ("informed", "Informed", "📰", "skill_count_at_star", {"stars": 3, "count": 8},
     "Reach 3 stars in eight different skills."),
    ("assured", "Assured", "🧷", "skill_count_at_star", {"stars": 6, "count": 4},
     "Reach 6 stars in four different skills."),
    ("accomplished", "Accomplished", "🎗️", "skill_count_at_star", {"stars": 6, "count": 8},
     "Reach 6 stars in eight different skills."),
    ("eminent", "Eminent", "🔱", "skill_count_at_star", {"stars": 9, "count": 10},
     "Reach 9 stars in ten different skills."),
    ("titan", "Titan", "⚓", "skill_count_at_star", {"stars": 10, "count": 25},
     "Reach 10 stars in twenty-five different skills."),
    ("mythic", "Mythic", "🦅", "skill_count_at_star", {"stars": 10, "count": 35},
     "Reach 10 stars in thirty-five different skills."),

    # ---- levels_completed: the long run
    ("three-hundred", "Three Hundred", "🏰", "levels_completed", {"count": 300},
     "Pass three hundred levels."),
    ("five-hundred", "Five Hundred", "🗼", "levels_completed", {"count": 500},
     "Pass five hundred levels."),

    # ---- tracks_completed: depth, up to the whole catalogue
    ("fifteen-tracks", "Fifteen Tracks", "🎪", "tracks_completed", {"count": 15},
     "Complete fifteen tracks."),
    ("twenty-tracks", "Twenty Tracks", "🏆", "tracks_completed", {"count": 20},
     "Complete twenty tracks."),
    ("every-track", "Every Track", "👑", "tracks_completed", {"count": 35},
     "Complete every track in the catalogue."),

    # ---- speed_pass: one further rung at the far end
    ("blink", "Blink", "🌀", "speed_pass", {"maxFraction": 0.12},
     "Pass an exam using less than 12% of its time limit."),

    # ---- level_streak: one further rung
    ("unbroken-fifty", "Unbroken Fifty", "🪨", "level_streak", {"count": 50},
     "Pass fifty consecutive levels on the first attempt."),

    # ---- attempts_to_pass: the most stubborn rung worth holding
    ("undeterred", "Undeterred", "🌋", "attempts_to_pass", {"maxAttempts": 20},
     "Pass an exam within twenty attempts."),
]


def ssh(command: str, stdin: bytes | None = None, timeout: int = 600) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=25", HOST, command],
        input=stdin, capture_output=True, timeout=timeout,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Close the ladder gaps in the authored catalogue.")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    raw = (ssh(f"cat {TARGET}").stdout or b"").decode("utf-8", "replace")
    existing = json.loads(raw)
    print(f"  existing: {len(existing)} authored entries")

    ids = {b["id"] for b in existing}
    clash = [entry[0] for entry in NEW if entry[0] in ids]
    if clash:
        print(f"  REFUSING: already present: {', '.join(clash)}")
        return 1

    # A rung that restates its neighbour is not a rung. Check every family for a duplicate criteriaValue.
    def key_of(badge: dict) -> tuple:
        return (badge["criteriaType"], tuple(sorted(badge["criteriaValue"].items())))

    existing_keys = {key_of(b) for b in existing}
    restated = [entry[0] for entry in NEW
                if (entry[3], tuple(sorted(entry[4].items()))) in existing_keys]
    if restated:
        print(f"  REFUSING: rung(s) restate an existing threshold: {', '.join(restated)}")
        return 1

    base = max(b["sortOrder"] for b in existing) + 10
    added = [
        {"id": i, "name": n, "description": d, "iconEmoji": e,
         "criteriaType": ct, "criteriaValue": cv, "sortOrder": base + k}
        for k, (i, n, e, ct, cv, d) in enumerate(NEW)
    ]
    total = len(existing) + len(added)
    print(f"  adding {len(added)}; result {total} authored, sortOrder {base}..{base + len(added) - 1}")

    by_type: dict[str, int] = {}
    for badge in existing + added:
        by_type[badge["criteriaType"]] = by_type.get(badge["criteriaType"], 0) + 1
    print("  authored rungs per criteria type:")
    for criteria_type, count in sorted(by_type.items(), key=lambda kv: -kv[1]):
        print(f"    {criteria_type:<22} {count}")

    if args.dry_run:
        print("  dry run: nothing written")
        return 0

    ssh(f"cp -a {TARGET} {TARGET}.before-100b")
    text = json.dumps(existing + added, indent=2, ensure_ascii=False) + "\n"
    if ssh(f"cat > {TARGET}", text.encode("utf-8")).returncode != 0:
        print("  write failed")
        return 1
    verify = (ssh(f"python3 -c \"import json;print(len(json.load(open('{TARGET}',encoding='utf-8'))))\""
                  ).stdout or b"").decode().strip()
    print(f"  written; the file parses with {verify} entries")
    return 0 if verify == str(total) else 1


if __name__ == "__main__":
    sys.exit(main())
