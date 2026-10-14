#!/usr/bin/env python3
"""Audit the live badge catalogue: criteria types, criteria values, ids, families, awards.

`certivo-rework/BADGES.md` (agent C scope) defines 13 criteria types, ~100 authored badges and
~700 generated `<track>.L<NN>` badges. Every one of those definitions is a row in `badges`, and a
row can be wrong in ways nothing else in the product notices:

* a `criteria_type` outside the 13 -- the badge renders exactly like an earnable one and can never
  be awarded, because no predicate in `awardBadges()` will ever match it;
* a `criteria_value_json` that is not JSON, is not an object, or carries a key its type does not
  define -- `{"stars": 10}` on a `level_complete` row is a definition that lies about itself;
* a duplicate badge id, which `badges.id` being a primary key makes impossible in SQLite/Postgres
  but possible in whatever generates the seed;
* a family that came out the wrong size -- the level badges are generated in a loop over each
  track's level count, and an off-by-one there silently shortens the catalogue;
* nothing ever awarded, which is the failure the whole expansion exists to prevent: a catalogue
  nobody can earn from looks identical to a catalogue nobody has earned from yet.

Everything is read from the running database and the deployed award service, so the report
describes what is live rather than what is committed.

The output is tables, not a verdict. A check that prints only "OK" is indistinguishable from a
check that examined nothing -- a mistake this project has already made twice -- so every section
prints the rows it looked at, and the row counts are printed even when a section is empty.

    python tools/audit_badges.py            # audit the live database
    python tools/audit_badges.py --json     # same findings, machine-readable
    python tools/audit_badges.py --offline  # no network: report that nothing was examined

Exit code is 1 when any problem is found, 2 when the audit could not run at all (so "could not
reach the database" can never be read as "catalogue is fine").
"""

from __future__ import annotations

import argparse
import contextlib
import json
import pathlib
import re
import subprocess
import sys
from collections import Counter, defaultdict

ROOT = pathlib.Path(r"C:\Users\arian\OneDrive\ドキュメント\Myriaxon")
VPS = ROOT / "tools" / "vps.py"
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"

HOST = "admin-certivo@139.190.97.11"
APP_CONTAINER = "certivo-app-1"
AWARD_SERVICE = "/srv/certivo/worker/src/services/badges.ts"

# The criteria table in BADGES.md, and the `criteriaValue` keys each type defines. Kept here
# rather than parsed out of the markdown on purpose: a checker that reads its expectations from
# the document it is checking cannot notice that the document changed.
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
    # Added after the original 13. `parse_awarded_types` filters the service's `case` labels through this
    # dict, so a type missing here is reported twice over: "not in BADGES.md" and "no earn path" -- both
    # wrong, and both about a type the service does handle. Whatever list the design uses, this has to match.
    "tracks_started": {"count"},
}

# Design says `perfect_score` "should gain {count} for repeats", so a bare `{}` is still valid.
OPTIONAL_KEYS: dict[str, set[str]] = {"perfect_score": {"count"}}

BADGE_QUERY = """
SELECT coalesce(json_agg(row_to_json(b) ORDER BY b.sort_order), '[]'::json)
FROM (
  SELECT id, track_id, name, criteria_type, criteria_value_json, sort_order
  FROM badges
) b;
"""

COUNTS_QUERY = """
SELECT 'tracks|' || count(*) FROM tracks
UNION ALL SELECT 'levels|' || count(*) FROM levels
UNION ALL SELECT 'badges|' || count(*) FROM badges
UNION ALL SELECT 'user_badges|' || count(*) FROM user_badges
UNION ALL SELECT 'users|' || count(*) FROM users
UNION ALL SELECT 'earned_users|' || count(DISTINCT user_id) FROM user_badges;
"""


# ----------------------------------------------------------------------------- remote access
def remote(program: str, kind: str = "pyhost", timeout: int = 600) -> tuple[str, str]:
    """Pipe `program` to a remote interpreter through tools/vps.py; return (stdout, stderr)."""
    result = subprocess.run(
        [str(PYTHON), str(VPS), kind],
        input=program.encode("utf-8"),
        capture_output=True,
        timeout=timeout,
    )
    return (
        (result.stdout or b"").decode("utf-8", "replace"),
        (result.stderr or b"").decode("utf-8", "replace"),
    )


def sql(statement: str) -> list[list[str]]:
    """Run SQL through psql; psql is invoked with -F'|' so each line is one row of fields."""
    out, _ = remote(statement, kind="sql")
    return [line.split("|") for line in out.splitlines() if line.strip()]


def psql_rows(statement: str) -> tuple[list[list[str]], str]:
    out, err = remote(statement, kind="sql")
    return [line.split("|") for line in out.splitlines() if line.strip()], err


# ----------------------------------------------------------------------------- pure parsing
def parse_badges(rows: list[list[str]]) -> tuple[list[dict], str | None]:
    """psql returns the JSON payload as a single field; unparseable output is a problem, not a
    silently empty catalogue."""
    text = "\n".join(row[0] for row in rows if row and row[0] is not None).strip()
    if not text:
        return [], "the badge query returned nothing at all"
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        return [], f"the badge query did not return JSON: {exc}"
    if not isinstance(parsed, list):
        return [], f"the badge query returned {type(parsed).__name__}, expected a list"
    return parsed, None


def parse_counts(rows: list[list[str]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        if len(row) >= 2 and row[1].strip().isdigit():
            counts[row[0]] = int(row[1])
    return counts


def parse_awarded_types(source: str) -> set[str]:
    """Which criteria types the deployed award logic can evaluate.

    The service reads its rules from the `badges` table and dispatches on `criteriaType` in a
    switch, so the case labels are the answer to "can this badge ever be earned". Returns an empty
    set when the service could not be read -- reported as such rather than assumed fine.
    """
    switch = re.search(r"function matchCriteria\([\s\S]*?\n\}", source)
    if not switch:
        return set()
    return {label for label in re.findall(r'case\s+"([a-z_]+)"', switch.group(0)) if label in CRITERIA}


def classify_family(badge_id: str, track_id: str | None) -> str:
    """Which family a badge belongs to, from its id shape.

    Matched on the id rather than on a hand-written list of the ~100 authored ids, so a badge added
    to `content/badges.json` later is still classified -- including as `UNRECOGNISED`, which is what
    a typo in an id pattern looks like.
    """
    if re.fullmatch(r".+\.L\d+", badge_id):
        return "level (<track>.L<NN>)"
    if badge_id.endswith(".graduate"):
        return "graduate (<track>.graduate)"
    if badge_id.endswith(".L"):
        return "UNRECOGNISED (level pattern, no number)"
    if track_id:
        return "UNRECOGNISED (track-scoped, unknown pattern)"
    return "authored (platform-wide)"


def audit(badges: list[dict], awarded_types: set[str]) -> dict:
    """Everything wrong with the catalogue rows, plus the row accounting behind each finding."""
    by_type: Counter[str] = Counter()
    ids: list[str] = []
    malformed: list[dict] = []
    undefined: list[dict] = []
    missing_key: list[dict] = []
    shapes: dict[tuple[str, tuple[str, ...]], int] = defaultdict(int)
    families: Counter[str] = Counter()
    per_family_types: dict[str, Counter[str]] = defaultdict(Counter)
    level_badges_by_track: dict[str, set[str]] = defaultdict(set)
    parse_failures = 0

    for badge in badges:
        badge_id = str(badge.get("id") or "")
        track_id = badge.get("track_id")
        criteria_type = str(badge.get("criteria_type") or "")
        ids.append(badge_id)
        by_type[criteria_type] += 1
        family = classify_family(badge_id, track_id)
        families[family] += 1
        per_family_types[family][criteria_type] += 1
        if family.startswith("level ("):
            level_badges_by_track[str(track_id)].add(badge_id)

        raw = badge.get("criteria_value_json")
        try:
            value = json.loads(raw if raw is not None else "")
        except (json.JSONDecodeError, TypeError):
            parse_failures += 1
            malformed.append({"id": badge_id, "criteria_type": criteria_type, "raw": raw})
            continue
        if not isinstance(value, dict):
            parse_failures += 1
            malformed.append({"id": badge_id, "criteria_type": criteria_type, "raw": raw})
            continue

        shapes[(criteria_type, tuple(sorted(value.keys())))] += 1
        allowed = CRITERIA.get(criteria_type)
        if allowed is None:
            continue
        extra = sorted(set(value) - allowed)
        if extra:
            undefined.append(
                {"id": badge_id, "criteria_type": criteria_type, "extra": extra, "value": value}
            )
        absent = sorted(allowed - set(value) - OPTIONAL_KEYS.get(criteria_type, set()))
        if absent:
            missing_key.append(
                {"id": badge_id, "criteria_type": criteria_type, "absent": absent, "value": value}
            )

    duplicates = sorted(badge_id for badge_id, count in Counter(ids).items() if count > 1)
    duplicate_counts = Counter(ids)
    unknown_types = sorted(set(by_type) - set(CRITERIA))
    unearnable_types = sorted(set(by_type) - awarded_types)
    unearnable_badges = sum(by_type[t] for t in unearnable_types)

    return {
        "total": len(badges),
        "by_type": by_type,
        "families": families,
        "per_family_types": per_family_types,
        "shapes": shapes,
        "malformed": malformed,
        "undefined_keys": undefined,
        "missing_keys": missing_key,
        "duplicates": duplicates,
        "duplicate_counts": duplicate_counts,
        "unknown_types": unknown_types,
        "level_badges_by_track": level_badges_by_track,
        "parse_failures": parse_failures,
        "awarded_types": awarded_types,
        "unearnable_types": unearnable_types,
        "unearnable_badges": unearnable_badges,
    }


def completeness_problems(result: dict, counts: dict[str, int]) -> list[str]:
    """Catalogue gaps that only a cross-check against `tracks`/`levels` can reveal."""
    problems: list[str] = []
    tracks = counts.get("tracks", 0)
    levels = counts.get("levels", 0)

    if tracks and levels:
        expected_levels = levels
        found_levels = sum(len(v) for v in result["level_badges_by_track"].values())
        if found_levels != expected_levels:
            problems.append(
                f"level badges: {found_levels} defined, but the database holds {expected_levels} "
                f"level rows -- {abs(expected_levels - found_levels)} "
                f"{'missing' if found_levels < expected_levels else 'extra'}"
            )
        tracks_with_levels = len(result["level_badges_by_track"])
        if tracks_with_levels != tracks:
            problems.append(
                f"level badges exist for {tracks_with_levels} track(s), but the database holds {tracks}"
            )
    if tracks:
        graduates = result["families"].get("graduate (<track>.graduate)", 0)
        if graduates != tracks:
            problems.append(f"graduate badges: {graduates} defined, {tracks} track(s) exist")
    return problems


# ----------------------------------------------------------------------------- presentation
def rule(title: str, out=sys.stdout) -> None:
    print(f"\n== {title} " + "=" * max(0, 74 - len(title)), file=out)


def table(headers: list[str], rows: list[list[object]], empty_note: str, out=sys.stdout) -> None:
    """Print a table, or say plainly that there was nothing to print and how many rows that was."""
    print(f"  ({len(rows)} row(s))", file=out)
    if not rows:
        print(f"  {empty_note}", file=out)
        return
    widths = [len(h) for h in headers]
    cells = [[str(c) for c in row] for row in rows]
    for row in cells:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    print("  " + "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)).rstrip(), file=out)
    print("  " + "  ".join("-" * widths[i] for i in range(len(headers))), file=out)
    for row in cells:
        print("  " + "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip(), file=out)


def report(result: dict, counts: dict[str, int], awarded_source_read: bool, out=sys.stdout) -> list[str]:
    problems: list[str] = []
    read = result["total"]

    if read == 0:
        # An empty catalogue is not a clean catalogue. It is the shape a failed seed leaves
        # behind, and reporting "no problems" for it is the exact mistake this tool is for.
        problems.append("the badges table is empty -- the catalogue has no definitions at all")

    rule("what was examined", out)
    print(f"  badge rows read from the live database : {read}", file=out)
    print(f"  tracks / levels / badges / user_badges : "
          f"{counts.get('tracks', '?')} / {counts.get('levels', '?')} / "
          f"{counts.get('badges', '?')} / {counts.get('user_badges', '?')}", file=out)
    print(f"  award service source read              : {'yes' if awarded_source_read else 'NO'}"
          f"  ({AWARD_SERVICE})", file=out)
    if read != counts.get("badges"):
        problems.append(
            f"the badge query returned {read} row(s) but count(*) on badges says "
            f"{counts.get('badges')} -- the audit did not see the whole table"
        )

    rule("criteria_type: every distinct type present", out)
    rows = [
        [criteria_type, count, "" if criteria_type in CRITERIA else "NOT IN BADGES.md",
         "" if criteria_type in result["awarded_types"] else "no earn path in deployed service"]
        for criteria_type, count in result["by_type"].most_common()
    ]
    table(["criteria_type", "badges", "design", "award logic"], rows,
          "no rows at all -- nothing to audit", out)
    undefined_types = sorted(result["by_type"])
    missing_types = sorted(set(CRITERIA) - set(undefined_types))
    print(f"  design types present : "
          f"{len([t for t in undefined_types if t in CRITERIA])} of {len(CRITERIA)}", file=out)
    print(f"  design types absent  : "
          f"{', '.join(missing_types) if missing_types else '(none)'}", file=out)
    if result["unknown_types"]:
        problems.append(
            "criteria type(s) not in BADGES.md, so no badge using them can ever be earned: "
            + ", ".join(f"{t} (x{result['by_type'][t]})" for t in result["unknown_types"])
        )

    rule("criteria_value_json shapes", out)
    shape_rows = [
        [criteria_type, "{" + ", ".join(keys) + "}" if keys else "{}", count,
         "ok" if (keys and not (set(keys) - CRITERIA.get(criteria_type, set())))
                or (not keys and not CRITERIA.get(criteria_type, set()))
                or (not keys and criteria_type in OPTIONAL_KEYS)
                else "CHECK"]
        for (criteria_type, keys), count in sorted(
            result["shapes"].items(), key=lambda item: (-item[1], item[0][0])
        )
    ]
    table(["criteria_type", "keys", "badges", "verdict"], shape_rows,
          "no parseable criteria values", out)
    print(f"  distinct (type, key-set) shapes        : {len(result['shapes'])}", file=out)
    print(f"  rows whose criteria_value_json will not parse as an object : "
          f"{result['parse_failures']}", file=out)
    for item in result["malformed"][:10]:
        print(f"    - {item['id']} ({item['criteria_type']}): {str(item['raw'])[:60]!r}", file=out)
    if result["malformed"]:
        problems.append(
            f"{len(result['malformed'])} badge(s) whose criteria_value_json is not a JSON object"
        )
    for item in result["undefined_keys"][:10]:
        print(f"    - {item['id']} ({item['criteria_type']}): undefined key(s) {item['extra']}",
              file=out)
    if result["undefined_keys"]:
        problems.append(
            f"{len(result['undefined_keys'])} badge(s) carry criteriaValue key(s) their type does "
            f"not define"
        )
    for item in result["missing_keys"][:10]:
        print(f"    - {item['id']} ({item['criteria_type']}): missing key(s) {item['absent']}",
              file=out)
    if result["missing_keys"]:
        problems.append(
            f"{len(result['missing_keys'])} badge(s) are missing a criteriaValue key their type "
            f"needs -- the predicate cannot read a rule that is not there"
        )

    rule("duplicate badge ids", out)
    table(["badge id", "occurrences"],
          [[badge_id, result["duplicate_counts"][badge_id]] for badge_id in result["duplicates"]],
          "none -- every id is unique", out)
    print(f"  ids inspected: {read}, duplicated: {len(result['duplicates'])}", file=out)
    if result["duplicates"]:
        problems.append(f"duplicate badge id(s): {', '.join(result['duplicates'][:5])}")

    rule("families: authored vs generated", out)
    family_order = [
        "authored (platform-wide)",
        "level (<track>.L<NN>)",
        "graduate (<track>.graduate)",
    ]
    other_families = [f for f in result["families"] if f not in family_order]
    rows = []
    for family in family_order + sorted(other_families):
        if family not in result["families"]:
            continue
        types = result["per_family_types"][family]
        rows.append([family, result["families"][family],
                     ", ".join(f"{t} x{n}" for t, n in types.most_common())])
    table(["family", "badges", "criteria types"], rows, "no badges", out)
    authored = result["families"].get("authored (platform-wide)", 0)
    generated = read - authored
    print(f"  authored : {authored}", file=out)
    print(f"  generated: {generated}  (design expects ~735: 700 level + 35 graduate)", file=out)
    unrecognised = [f for f in result["families"] if f.startswith("UNRECOGNISED")]
    if unrecognised:
        problems.append(f"badge id pattern(s) not recognised: {', '.join(unrecognised)}")
    problems.extend(completeness_problems(result, counts))

    rule("earn path in the deployed award service", out)
    print(f"  criteria types the service can evaluate : "
          f"{len(result['awarded_types'])} of {len(CRITERIA)}"
          + ("" if awarded_source_read else "   (source could not be read)"), file=out)
    table(["criteria_type", "badges", "earn path"],
          [[t, result["by_type"][t], "yes" if t in result["awarded_types"] else "NO"]
           for t in sorted(result["by_type"])],
          "no criteria types in the catalogue", out)
    print(f"  badges with no earn path: {result['unearnable_badges']} of {read}", file=out)
    if result["unearnable_types"]:
        problems.append(
            f"{len(result['unearnable_types'])} criteria type(s) with definitions have no earn "
            f"path in the deployed service, covering {result['unearnable_badges']} badge(s): "
            + ", ".join(result["unearnable_types"])
        )

    rule("awards", out)
    earned = counts.get("user_badges", 0)
    earners = counts.get("earned_users", 0)
    print(f"  user_badges rows        : {earned}", file=out)
    print(f"  distinct users with one : {earners}", file=out)
    print(f"  users in the database   : {counts.get('users', '?')}", file=out)
    print(f"  catalogue rows          : {read}", file=out)
    if earned == 0:
        print("  NOTE: nothing has ever been awarded. A catalogue nobody has earned from is", file=out)
        print("        indistinguishable from one nobody can earn from -- see the earn-path table.",
              file=out)
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit the live badge catalogue.")
    parser.add_argument("--json", action="store_true", help="machine-readable findings")
    parser.add_argument("--offline", action="store_true",
                        help="do not touch the network; report that nothing was examined")
    parser.add_argument("--service-source", help="read the award service from this file instead of the VPS")
    args = parser.parse_args()

    if args.offline:
        print("offline: no database was queried and no service source was read.")
        print("This is NOT a pass -- it is an audit that examined nothing.")
        return 2

    badge_rows, badge_err = psql_rows(BADGE_QUERY)
    badges, parse_problem = parse_badges(badge_rows)
    if parse_problem:
        print(f"could not read the badge catalogue: {parse_problem}", file=sys.stderr)
        if badge_err.strip():
            print(badge_err.strip()[:800], file=sys.stderr)
        return 2

    count_rows, count_err = psql_rows(COUNTS_QUERY)
    counts = parse_counts(count_rows)
    if not counts:
        print("could not read table counts from the database", file=sys.stderr)
        if count_err.strip():
            print(count_err.strip()[:800], file=sys.stderr)
        return 2

    awarded_source_read = False
    if args.service_source:
        source = pathlib.Path(args.service_source).read_text(encoding="utf-8")
        awarded_source_read = True
    else:
        source, _ = remote(
            f"print(open({AWARD_SERVICE!r}, encoding='utf-8').read())",
            kind="pyhost",
        )
        awarded_source_read = bool(source.strip())
    awarded_types = parse_awarded_types(source) if awarded_source_read else set()

    result = audit(badges, awarded_types)

    if args.json:
        # stdout carries the JSON document and nothing else; the tables go to stderr, so a
        # caller can pipe `--json` straight into a parser without stripping a report first.
        out = sys.stderr
        problems = report(result, counts, awarded_source_read, out=out)
        print(json.dumps(
            {
                "total": result["total"],
                "counts": counts,
                "by_type": dict(result["by_type"]),
                "families": dict(result["families"]),
                "duplicates": result["duplicates"],
                "malformed": result["malformed"],
                "undefined_keys": result["undefined_keys"],
                "missing_keys": result["missing_keys"],
                "unknown_types": result["unknown_types"],
                "awarded_types": sorted(result["awarded_types"]),
                "unearnable_types": result["unearnable_types"],
                "unearnable_badges": result["unearnable_badges"],
                "level_badges_by_track": {
                    track: len(ids) for track, ids in result["level_badges_by_track"].items()
                },
                "problems": problems,
            },
            indent=2,
        ))
        rule("result", out)
    else:
        out = sys.stdout
        problems = report(result, counts, awarded_source_read, out=out)
        rule("result", out)
    if problems:
        print(f"  {len(problems)} PROBLEM(S):", file=out)
        for problem in problems:
            print(f"    - {problem}", file=out)
        return 1
    print(f"  {result['total']} badge definition(s) checked, no problems found", file=out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
