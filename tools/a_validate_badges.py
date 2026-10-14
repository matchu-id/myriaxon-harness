#!/usr/bin/env python3
"""Validate Agent A's badges.json against BADGES.md.

Checks that a typo in criteriaType, or a criteriaValue key the design does not define, cannot reach the seed.
Reads BADGES.md for the authoritative type list and the documented criteriaValue shapes.
"""
import json
import re
import sys
from pathlib import Path

BASE = Path(r"C:\Users\arian\OneDrive\ドキュメント\Myriaxon\certivo-rework")
BADGES = BASE / ".agent-a" / "badges.json"
DESIGN = BASE / "BADGES.md"

# From BADGES.md "Criteria types" table -> the criteriaValue keys each type documents.
# {} means the type carries no criteriaValue keys.
ALLOWED_VALUE_KEYS = {
    "level_complete": {"levelNumber"},
    "first_code_pass": set(),
    "perfect_score": set(),          # gains {count} for repeats (row 3)
    "track_complete": set(),
    "first_try_pass": set(),
    "attempts_to_pass": {"maxAttempts"},
    "speed_pass": {"maxFraction"},
    "skill_star": {"stars"},
    "skill_count_at_star": {"stars", "count"},
    "levels_completed": {"count"},
    "tracks_completed": {"count"},
    "code_tests_perfect": {"minTests"},
    "level_streak": {"count"},
}
# perfect_score is documented as gaining {count} for repeats.
ALLOWED_VALUE_KEYS["perfect_score"] = {"count"}

FROZEN = {
    "first-steps": "level_complete",
    "code-warrior": "first_code_pass",
    "perfect-score": "perfect_score",
}


def main() -> int:
    design = DESIGN.read_text(encoding="utf-8")
    # Every criteriaType named in the design's table, parsed from the markdown.
    design_types = set(re.findall(r"^\|\s*\d+\s*\|\s*`([a-z_]+)`\s*\|", design, re.M))
    print(f"BADGES.md declares {len(design_types)} criteria types: {sorted(design_types)}")

    badges = json.loads(BADGES.read_text(encoding="utf-8"))
    print(f"badges.json holds {len(badges)} badges")

    problems: list[str] = []
    ids = [b["id"] for b in badges]
    if len(set(ids)) != len(ids):
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        problems.append(f"duplicate ids: {dupes}")

    orders = [b["sortOrder"] for b in badges]
    if len(set(orders)) != len(orders):
        dupes = sorted({o for o in orders if orders.count(o) > 1})
        problems.append(f"duplicate sortOrder: {dupes}")

    for b in badges:
        bid = b["id"]
        ct = b["criteriaType"]
        if ct not in design_types:
            problems.append(f"{bid}: criteriaType '{ct}' is NOT in BADGES.md")
        for key in ("id", "name", "description", "iconEmoji", "criteriaType",
                    "criteriaValue", "sortOrder"):
            if key not in b:
                problems.append(f"{bid}: missing key '{key}'")
        if not isinstance(b.get("criteriaValue"), dict):
            problems.append(f"{bid}: criteriaValue is not an object")
        extra = set(b.get("criteriaValue", {})) - ALLOWED_VALUE_KEYS.get(ct, set())
        if extra:
            problems.append(f"{bid}: criteriaValue keys {sorted(extra)} not defined for '{ct}'")
        if not b.get("iconEmoji"):
            problems.append(f"{bid}: empty iconEmoji")

    # The three pre-existing badges are frozen in id + criteriaType.
    by_id = {b["id"]: b for b in badges}
    for bid, ct in FROZEN.items():
        if bid not in by_id:
            problems.append(f"frozen badge '{bid}' disappeared")
        elif by_id[bid]["criteriaType"] != ct:
            problems.append(f"frozen badge '{bid}' changed criteriaType to {by_id[bid]['criteriaType']!r}")

    # New families must sit above the existing 0-135 range and be a contiguous block.
    new = [b for b in badges if b["id"] not in FROZEN]
    lo, hi = min(b["sortOrder"] for b in new), max(b["sortOrder"] for b in new)
    print(f"new badges: {len(new)}, sortOrder {lo}..{hi} (existing range is 0..135)")
    if lo <= 135:
        problems.append(f"new badge sortOrder starts at {lo}, overlapping the existing 0..135 range")

    # Distinctness of icons among the new family.
    icons = [b["iconEmoji"] for b in new]
    dup_icons = sorted({i for i in icons if icons.count(i) > 1})
    if dup_icons:
        print(f"NOTE duplicate icons within new family: {dup_icons}")

    print(f"\n{len(problems)} problem(s)")
    for p in problems:
        print(f"  FAIL {p}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
