#!/usr/bin/env python3
"""Verify the expanded badge system against its design and the live database.

`certivo-rework/BADGES.md` is the agreed design: 13 criteria types, ~100 authored badges, ~700 generated
per-level badges across 35 tracks. Three separate things can go wrong with that, and none of them raises an
error anywhere:

1. **A criteria type nobody implements.** A definition row with `criteriaType: "streak_master"` and no matching
   predicate in `awardBadges()` looks completely normal in the catalogue and can never be earned. This is the
   failure mode the whole tool exists for: the badge appears in the UI, the learner chases it, it never fires.
2. **A `criteriaValue` the predicate does not read.** A badge claiming `{"stars": 10}` while the code checks
   `>= 5` is a row that lies. The two halves of this feature were written by different agents from the same
   document, which is exactly the situation where they drift.
3. **A generated badge for a level that does not exist**, or a missing one for a level that does — the loop in
   `build-seed.ts` reads each track's level count, so an off-by-one there produces a catalogue that is subtly
   short.

It also reports how many badges have ever been earned, because a catalogue that nothing has been awarded from
is indistinguishable from a catalogue that nothing *can* be awarded from, and this system currently sits at
zero.

Everything is read from the live database, so the answer describes what is deployed rather than what is
committed.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import Counter, defaultdict

import pathlib

ROOT = pathlib.Path(r"C:\Users\arian\OneDrive\ドキュメント\Myriaxon")
VPS = ROOT / "tools" / "vps.py"
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
DESIGN = ROOT / "certivo-rework" / "BADGES.md"

HOST = "admin-certivo@139.190.97.11"
APP = "certivo-app-1"

# The criteria table in BADGES.md, and the `criteriaValue` keys each type defines. Kept here rather than parsed
# from the markdown: a checker that reads its own expectations from the document it is checking cannot notice
# that the document changed.
CRITERIA: dict[str, set[str]] = {
    "level_complete": {"levelNumber"},
    "first_code_pass": set(),
    "perfect_score": {"count"},
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
    # Added after the original 13: nothing rewarded *starting* a track, and the other families were saturated
    # (skill_star at 10 stars, code_tests_perfect at the 6-test maximum the content carries). Implemented in
    # badges.ts -- union, known-set, BadgeState, the batched query and the matchCriteria case -- before any
    # definition was added, because a definition whose type the service cannot evaluate is unearnable.
    "tracks_started": {"count"},
}


def remote(program: str, kind: str = "pyhost", timeout: int = 600) -> str:
    result = subprocess.run(
        [str(PYTHON), str(VPS), kind], input=program.encode("utf-8"),
        capture_output=True, timeout=timeout,
    )
    return (result.stdout or b"").decode("utf-8", "replace")


def sql(statement: str) -> list[list[str]]:
    """Run SQL through psql and split the tab-free pipe-delimited output into rows of fields."""
    out = remote(statement, kind="sql")
    rows = []
    for line in out.splitlines():
        if line.strip():
            rows.append(line.split("|"))
    return rows


def fetch_badges() -> list[dict]:
    raw = remote(
        "import json, subprocess\n"
        "r = subprocess.run(['docker','exec','certivo-db-1','psql','-U','certivo','-d','certivo',\n"
        "                    '-t','-A','-c',\"SELECT json_agg(row_to_json(b)) FROM \"\n"
        "                    \"(SELECT id, track_id, name, icon_emoji, criteria_type, criteria_value_json, \"\n"
        "                    \"sort_order FROM badges ORDER BY sort_order) b\"],\n"
        "                   capture_output=True, text=True)\n"
        "print(r.stdout.strip())\n"
    )
    text = raw.strip()
    if not text.startswith("["):
        return []
    return json.loads(text)


def award_paths() -> tuple[set[str], set[str], set[str]]:
    """What the award logic can produce: handled criteria types, literal badge ids, and id families.

    The service was rewritten mid-expansion from hardcoded badge ids to definitions-driven predicates, and this
    function had to be rewritten with it. The first version looked for literal ids:

        if (result.hadCodeQuestion && (await award(db, userId, "code-warrior", now)))

    which the new code no longer contains at all -- it loads every definition and evaluates a predicate per
    `criteriaType`. So the old check reported **none** of the 762 badges as reachable and *twelve of thirteen*
    criteria types as unsupported, on an implementation that handles all thirteen. A checker calibrated to the
    previous architecture does not merely miss the new one; it inverts the verdict.

    What is read now:

    * **handled types** -- the string labels of the `matchCriteria` switch, which is the real answer to "can this
      badge be earned". A badge whose `criteriaType` appears there is reachable by construction.
    * **literal ids** and **id families** -- still read, because a hardcoded-only implementation must keep
      working and because the graduate badge is produced by pattern rather than by a criteria row.
    """
    source = remote("print(open('/srv/certivo/worker/src/services/badges.ts', encoding='utf-8').read())")
    if not source.strip():
        return set(), set(), set()

    # Definitions-driven: the switch inside matchCriteria names every criteria type the service can evaluate.
    handled: set[str] = set()
    switch = re.search(r"function matchCriteria\([\s\S]*?\n\}", source)
    if switch:
        handled = set(re.findall(r'case\s+"([a-z_]+)"', switch.group(0)))
    if not handled:
        # Older shape: the type appears as a literal somewhere in the service.
        handled = {name for name in CRITERIA
                   if re.search(rf'["\']{re.escape(name)}["\']', source)}

    literals = set(re.findall(r'award\(\s*db,\s*userId,\s*"([^"]+)"', source))
    literals |= set(re.findall(r'badgeId\s*=\s*`([^`]+)`', source))
    literals |= set(re.findall(r'newlyEarned\.push\(\s*"([^"]+)"', source))

    families: set[str] = set()
    if re.search(r"\.graduate", source):
        families.add("graduate")
    if re.search(r"levelNumber\s*===\s*1", source):
        families.add("first-level")
    return handled, literals, families


def schema_types() -> set[str]:
    """The criteria types `schema.ts` declares, which is now the single source in TypeScript.

    `badges.ts` derives its union and known-set from that array, so this list is the authority. The two Python
    tools cannot import TypeScript and keep their own copy — so the copy is **compared** rather than trusted.
    Three copies of one fact is survivable when something checks them; it is fatal when nothing does, which is
    how `schema.ts` came to be missing `tracks_started` while the service handled it.
    """
    source = remote("print(open('/srv/certivo/worker/src/db/schema.ts', encoding='utf-8').read())")
    block = re.search(r"BADGE_CRITERIA_TYPES = \[(.*?)\] as const;", source, re.S)
    if not block:
        return set()
    return set(re.findall(r'"([a-z_]+)"', block.group(1)))


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify the badge system against its design.")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    badges = fetch_badges()
    if not badges:
        print("  could not read any badge rows -- the query or the connection failed, which is not the same")
        print("  as an empty catalogue")
        return 1

    problems: list[str] = []
    by_type: Counter[str] = Counter()
    values_by_type: dict[str, list[dict]] = defaultdict(list)
    ids: list[str] = []
    malformed: list[str] = []

    for badge in badges:
        badge_id = badge.get("id") or "?"
        ids.append(badge_id)
        criteria_type = badge.get("criteria_type") or ""
        by_type[criteria_type] += 1
        try:
            value = json.loads(badge.get("criteria_value_json") or "{}")
        except json.JSONDecodeError:
            malformed.append(f"{badge_id}: criteria_value_json is not valid JSON")
            continue
        if not isinstance(value, dict):
            malformed.append(f"{badge_id}: criteria_value_json is {type(value).__name__}, expected an object")
            continue
        values_by_type[criteria_type].append(value)

    duplicates = [badge_id for badge_id, n in Counter(ids).items() if n > 1]
    if duplicates:
        problems.append(f"duplicate badge id(s): {', '.join(duplicates[:5])}")

    unknown_types = sorted(set(by_type) - set(CRITERIA))
    if unknown_types:
        problems.append(
            "criteria type(s) not in the design -- these can never be earned: "
            + ", ".join(f"{t} (x{by_type[t]})" for t in unknown_types))

    # The drift check: the TypeScript source of truth against this tool's copy of it.
    declared = schema_types()
    if not declared:
        problems.append("could not read BADGE_CRITERIA_TYPES from schema.ts, so the lists cannot be compared")
    else:
        only_python = sorted(set(CRITERIA) - declared)
        only_schema = sorted(declared - set(CRITERIA))
        if only_python:
            problems.append(
                "this tool lists type(s) schema.ts does not declare: " + ", ".join(only_python))
        if only_schema:
            problems.append(
                "schema.ts declares type(s) this tool does not list, so it cannot check their criteriaValue "
                "shapes: " + ", ".join(only_schema))

    # Which badges the award logic can actually reach. A badge with no earn path is a definition that will sit
    # in the catalogue forever; the whole point of the expansion is to eliminate those, so until the refactor
    # that makes awards definitions-driven lands, the count is reported rather than assumed to be zero.
    handled, literals, families = award_paths()
    reachable = 0
    unreachable: list[str] = []
    for badge in badges:
        badge_id = badge.get("id") or ""
        criteria_type = badge.get("criteria_type") or ""
        if criteria_type in handled:
            reachable += 1
        elif badge_id in literals:
            reachable += 1
        elif re.fullmatch(r".+\.L\d+", badge_id) and "first-level" in families and badge_id.endswith(".L01"):
            reachable += 1
        elif badge_id.endswith(".graduate") and "graduate" in families:
            reachable += 1
        else:
            unreachable.append(badge_id)

    # A criteria type with definitions where *no* badge of that type has an earn path. This is the failure the
    # expansion exists to remove, and it is invisible in the catalogue: `sharpshooter` renders exactly like
    # `perfect-score` and can never be awarded until the service knows what `perfect_score` with `{"count": 2}`
    # means. Reported per type rather than per badge, because 689 individual unreachable ids is noise while
    # "nine of thirteen criteria types are unsupported" is the actual finding.
    reachable_types: set[str] = set()
    for badge in badges:
        badge_id = badge.get("id") or ""
        criteria_type = badge.get("criteria_type") or ""
        if criteria_type in handled:
            reachable_types.add(criteria_type)
        elif badge_id in literals:
            reachable_types.add(criteria_type)
        elif re.fullmatch(r".+\.L\d+", badge_id) and badge_id.endswith(".L01") and "first-level" in families:
            reachable_types.add(criteria_type)
        elif badge_id.endswith(".graduate") and "graduate" in families:
            reachable_types.add(criteria_type)
    unsupported = sorted(t for t in by_type if t not in reachable_types)
    if unsupported:
        problems.append(
            f"{len(unsupported)} of {len(by_type)} criteria type(s) have no badge the award logic can "
            f"produce: {', '.join(unsupported)}")

    for criteria_type, values in values_by_type.items():
        allowed = CRITERIA.get(criteria_type)
        if allowed is None:
            continue
        for value in values:
            extra = set(value) - allowed
            if extra:
                problems.append(
                    f"{criteria_type}: criteriaValue key(s) {sorted(extra)} are not defined for this type "
                    f"(expected {sorted(allowed) or 'none'})")
            missing = allowed - set(value)
            # `perfect_score` may gain a count; a key that is optional in the design may legitimately be absent.
            if missing and criteria_type not in {"perfect_score"}:
                problems.append(f"{criteria_type}: criteriaValue is missing {sorted(missing)}")
    problems.extend(malformed)

    # Generated per-level badges: one per level of every track, and none for a level that does not exist.
    track_levels = {row[0]: int(row[1]) for row in
                    sql("SELECT t.id || '|' || count(l.id) FROM tracks t "
                        "LEFT JOIN levels l ON l.track_id = t.id GROUP BY t.id;")}
    tracks = {row[0] for row in sql("SELECT id FROM tracks;")}
    level_badges = [b for b in badges if re.fullmatch(r".+\.L\d+", b.get("id") or "")]
    graduate_badges = [b for b in badges if (b.get("id") or "").endswith(".graduate")]

    missing_levels: list[str] = []
    for track, count in track_levels.items():
        present = {b["id"] for b in level_badges if b["id"].startswith(f"{track}.L")}
        for number in range(1, count + 1):
            if f"{track}.L{number:02d}" not in present:
                missing_levels.append(f"{track}.L{number:02d}")
    if missing_levels:
        problems.append(f"{len(missing_levels)} missing level badge(s), e.g. {', '.join(missing_levels[:5])}")

    missing_graduates = sorted(tracks - {b["track_id"] for b in graduate_badges if b.get("track_id")})
    if missing_graduates:
        problems.append(f"track(s) with no graduate badge: {', '.join(missing_graduates[:5])}")

    awarded = sql("SELECT count(*) FROM user_badges;")
    awarded_count = int(awarded[0][0]) if awarded and awarded[0][0].isdigit() else 0

    if not args.quiet:
        print(f"  {len(badges)} badge definition(s), {len(by_type)} criteria type(s)")
        print(f"  {'criteria type':<24} {'count':>6}")
        for criteria_type, count in by_type.most_common():
            flag = "" if criteria_type in CRITERIA else "   NOT IN DESIGN"
            print(f"  {criteria_type:<24} {count:>6}{flag}")
        print()
        print(f"  per-level badges    : {len(level_badges)} "
              f"(expected {sum(track_levels.values())} across {len(track_levels)} track(s))")
        print(f"  graduate badges     : {len(graduate_badges)} (expected {len(tracks)})")
        print(f"  reachable from code : {reachable} of {len(badges)}")
        print(f"  badge ids in code   : {sorted(literals) or '(none)'}")
        print(f"  types in schema.ts  : {len(declared)}"
              f"{'  (matches this tool)' if declared == set(CRITERIA) else '  MISMATCH vs this tool'}")
        print(f"  awarded to anyone   : {awarded_count}")
        if unreachable and not args.quiet:
            print(f"  no earn path        : {len(unreachable)} badge(s), e.g. {', '.join(unreachable[:6])}")

    print()
    if problems:
        print(f"  {len(problems)} PROBLEM(S):")
        for problem in problems:
            print(f"    - {problem}")
        return 1
    print("  badge system consistent with the design")
    if awarded_count == 0:
        print("  NOTE: nothing has ever been awarded. Until an exam is passed against this code, every")
        print("        criteria type is unproven in production regardless of what the definitions say.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
