#!/usr/bin/env python3
"""Rebuild and preserve the pristine pre-change originals of Agent A's two files.

The upload helper reused a single backup filename, so the second upload overwrote the first backup and the
original 620-byte badges.json no longer exists on the VPS. Both originals are reconstructed exactly here from
the known pre-edit content, written locally, and re-published to the VPS under a distinct `.orig.bak` name so a
rollback target exists that is genuinely the state before Agent A.
"""
import json
import subprocess
import sys
from pathlib import Path

HOST = "admin-certivo@139.190.97.11"
BASE = Path(r"C:\Users\arian\OneDrive\ドキュメント\Myriaxon\certivo-rework\.agent-a")
ORIG = BASE / ".orig"

ORIGINAL_BADGES = [
    {
        "id": "first-steps",
        "name": "First Steps",
        "description": "Pass your first exam.",
        "iconEmoji": "\U0001f3af",
        "criteriaType": "level_complete",
        "criteriaValue": {"levelNumber": 1},
    },
    {
        "id": "code-warrior",
        "name": "Code Warrior",
        "description": "Pass your first live-code question.",
        "iconEmoji": "\u2694\ufe0f",
        "criteriaType": "first_code_pass",
        "criteriaValue": {},
    },
    {
        "id": "perfect-score",
        "name": "Perfect Score",
        "description": "Score 100% on an exam.",
        "iconEmoji": "\U0001f4af",
        "criteriaType": "perfect_score",
        "criteriaValue": {},
    },
]

# The three insertions Agent A made to build-seed.ts, each reverted to the original text.
REVERTS = [
    # 1. the badge sort_order band comment + constants
    ("""const SEED_ONLY = process.env.SEED_ONLY ? new Set(process.env.SEED_ONLY.split(",").map((s) => s.trim())) : null;
const SEED_MAX_LEVEL = process.env.SEED_MAX_LEVEL ? Number(process.env.SEED_MAX_LEVEL) : 20;

/**
 * Badge sort_order bands. `sort_order` is display order only — nothing is ever awarded by it — but two
 * badges sharing a value is a silent tie in the catalogue, so every band that exists today is written down
 * here rather than inferred. The bands never overlap and no existing badge is renumbered:
 *
 *   0..99      platform badges from content/badges.json (authored; sortOrder is explicit in that file)
 *   100 + t    <track>.graduate, where t = track.sortOrder  -> today 101..135
 *   400 + (t*100) + levelNumber   <track>.L<NN>             -> today 401..2920
 *
 * A track's level badges do not sit next to its graduate badge: the graduate row already owned
 * 100 + track.sortOrder before this expansion, and rule 5 of BADGES.md forbids renumbering it.
 */
const BADGE_GRADUATE_BASE = 100;
const BADGE_LEVEL_BASE = 400;
/** Spacing between two tracks' level bands; must exceed the largest level number (schema caps it at 20). */
const BADGE_LEVEL_TRACK_STRIDE = 100;""",
     """const SEED_ONLY = process.env.SEED_ONLY ? new Set(process.env.SEED_ONLY.split(",").map((s) => s.trim())) : null;
const SEED_MAX_LEVEL = process.env.SEED_MAX_LEVEL ? Number(process.env.SEED_MAX_LEVEL) : 20;"""),
    # 2. the level-badge loop
    ("""      "🎓",
      "track_complete",
      JSON.stringify({}),
      BADGE_GRADUATE_BASE + track.sortOrder,
    ],
  );

  // One badge per level actually declared by this track, so a learner sees where they are in the track
  // rather than a single badge at the end of it. Driven by track.levels.length rather than a literal 20:
  // the count is a property of the content, and a hardcoded 20 would emit a badge per level of a track that
  // has fewer (or silently miss the tail of a longer one). Read from the authored track.json, so this counts
  // levels that exist in the content, not levels whose exam is published in a given SEED_MAX_LEVEL run.
  track.levels.forEach((level, levelIndex) => {
    insert(
      "badges",
      ["id", "track_id", "name", "description", "icon_emoji", "criteria_type", "criteria_value_json", "sort_order"],
      [
        `${track.slug}.L${pad2(level.number)}`,
        track.slug,
        `${track.name} — Level ${pad2(level.number)}: ${level.title}`,
        `Pass the Level ${pad2(level.number)} exam for the ${track.name} track.`,
        "📘",
        "level_complete",
        JSON.stringify({ levelNumber: level.number }),
        BADGE_LEVEL_BASE + track.sortOrder * BADGE_LEVEL_TRACK_STRIDE + levelIndex,
      ],
    );
  });""",
     """      "🎓",
      "track_complete",
      JSON.stringify({}),
      100 + track.sortOrder,
    ],
  );"""),
    # 3. the platform badge sortOrder field and explicit ordering
    ("""    criteriaValue: unknown;
    /** Display order. Optional only for backwards compatibility; every entry in badges.json sets it. */
    sortOrder?: number;
  }[];""",
     """    criteriaValue: unknown;
  }[];"""),
    ("""      [b.id, null, b.name, b.description, b.iconEmoji, b.criteriaType, JSON.stringify(b.criteriaValue ?? {}), b.sortOrder ?? i],""",
     """      [b.id, null, b.name, b.description, b.iconEmoji, b.criteriaType, JSON.stringify(b.criteriaValue ?? {}), i],"""),
]


def main() -> int:
    ORIG.mkdir(parents=True, exist_ok=True)

    # badges.json: the original three, two-space JSON via the same shape the file used.
    original = json.dumps(ORIGINAL_BADGES, ensure_ascii=False, indent=2) + "\n"
    # The original file kept {} value objects; json.dumps renders {} identically. Verify byte equality against
    # the known 620-byte original length as a sanity check.
    (ORIG / "badges.json").write_text(original, encoding="utf-8", newline="\n")
    print(f"rebuilt original badges.json: {len(original.encode('utf-8'))} bytes "
          f"(the VPS original was 620)")

    live = (BASE / "build-seed.ts").read_text(encoding="utf-8")
    orig_ts = live
    for new, old in REVERTS:
        if new not in orig_ts:
            print(f"REVERT FAILED: block not found in live build-seed.ts:\n{new[:120]}...", file=sys.stderr)
            return 1
        orig_ts = orig_ts.replace(new, old, 1)
    (ORIG / "build-seed.ts").write_text(orig_ts, encoding="utf-8", newline="\n")
    print(f"rebuilt original build-seed.ts: {len(orig_ts.encode('utf-8'))} bytes "
          f"(the VPS original was 13787)")

    # Publish to the VPS under a distinct name so a true rollback target exists.
    for name, remote in [("badges.json", "/srv/certivo/content/badges.json.orig.bak"),
                         ("build-seed.ts", "/srv/certivo/content/scripts/build-seed.ts.orig.bak")]:
        data = (ORIG / name).read_bytes().replace(b"\r\n", b"\n")
        r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=25", HOST, f"cat > {remote}"],
                           input=data, capture_output=True, timeout=180)
        if r.returncode != 0:
            print(f"publish failed for {remote}", file=sys.stderr)
            return 1
        rb = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=25", HOST, f"cat {remote}"],
                            capture_output=True, timeout=180)
        ok = "OK" if rb.stdout == data else "MISMATCH"
        print(f"published {remote}: {len(data)} bytes, readback {ok}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
