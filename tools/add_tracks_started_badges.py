#!/usr/bin/env python3
"""Add the `tracks_started` family, taking the authored catalogue from 81 to 100.

Now that the criteria type is implemented in `badges.ts` — union, known-set, `BadgeState`, the batched query,
and the `matchCriteria` case — the definitions have an earn path, which is the condition that made the earlier
version of this script unsafe to run.

Every entry is a platform badge (`track_id` null), which matters for one specific reason: `matchCriteria`
refuses a track-owned badge whose track differs from the exam's, so a *breadth* count that spans tracks must be
platform-wide or it could never fire except on the one track it named. `levels_completed` and
`tracks_completed` are platform badges for the same reason.

Thresholds run 2..35 against 35 tracks. Rung 1 is omitted: a learner earns `first-steps` the moment they sit a
first exam, so a "started one track" badge would be a duplicate of it.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys

HOST = "admin-certivo@139.190.97.11"
TARGET = "/srv/certivo/content/badges.json"

NEW: list[tuple[str, str, str, str, dict, str]] = [
    ("two-paths", "Two Paths", "🛤️", "tracks_started", {"count": 2}, "Start two tracks."),
    ("three-paths", "Three Paths", "🧭", "tracks_started", {"count": 3}, "Start three tracks."),
    ("four-paths", "Four Paths", "🧳", "tracks_started", {"count": 4}, "Start four tracks."),
    ("five-paths", "Five Paths", "🗺️", "tracks_started", {"count": 5}, "Start five tracks."),
    ("six-paths", "Six Paths", "📬", "tracks_started", {"count": 6}, "Start six tracks."),
    ("eight-paths", "Eight Paths", "🚀", "tracks_started", {"count": 8}, "Start eight tracks."),
    ("ten-paths", "Ten Paths", "🎏", "tracks_started", {"count": 10}, "Start ten tracks."),
    ("twelve-paths", "Twelve Paths", "🛰️", "tracks_started", {"count": 12}, "Start twelve tracks."),
    ("fourteen-paths", "Fourteen Paths", "🎐", "tracks_started", {"count": 14}, "Start fourteen tracks."),
    ("sixteen-paths", "Sixteen Paths", "🌍", "tracks_started", {"count": 16}, "Start sixteen tracks."),
    ("eighteen-paths", "Eighteen Paths", "🧿", "tracks_started", {"count": 18}, "Start eighteen tracks."),
    ("twenty-paths", "Twenty Paths", "🌐", "tracks_started", {"count": 20}, "Start twenty tracks."),
    ("twenty-two-paths", "Twenty-Two Paths", "🌠", "tracks_started", {"count": 22}, "Start twenty-two tracks."),
    ("twenty-five-paths", "Twenty-Five Paths", "🪐", "tracks_started", {"count": 25}, "Start twenty-five tracks."),
    ("twenty-eight-paths", "Twenty-Eight Paths", "🔭", "tracks_started", {"count": 28}, "Start twenty-eight tracks."),
    ("thirty-paths", "Thirty Paths", "🌌", "tracks_started", {"count": 30}, "Start thirty tracks."),
    ("thirty-two-paths", "Thirty-Two Paths", "🛸", "tracks_started", {"count": 32}, "Start thirty-two tracks."),
    ("every-path", "Every Path", "✨", "tracks_started", {"count": 35}, "Start every track in the catalogue."),
    ("six-hundred", "Six Hundred", "🗽", "levels_completed", {"count": 600}, "Pass six hundred levels."),
]


def ssh(command: str, stdin: bytes | None = None, timeout: int = 600) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=25", HOST, command],
        input=stdin, capture_output=True, timeout=timeout,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Add the tracks_started family.")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    existing = json.loads((ssh(f"cat {TARGET}").stdout or b"").decode("utf-8", "replace"))
    print(f"  existing: {len(existing)} authored entries")

    # The definitions are only safe to add because the code can evaluate them. Check that rather than assume.
    service = (ssh("cat /srv/certivo/worker/src/services/badges.ts").stdout or b"").decode("utf-8", "replace")
    if 'case "tracks_started"' not in service:
        print('  REFUSING: the award service has no `case "tracks_started"`, so every definition added here')
        print("  would have no earn path -- the exact defect verify_badges.py reports.")
        return 1
    print('  award service handles "tracks_started": yes')

    ids = {b["id"] for b in existing}
    def key_of(badge: dict) -> tuple:
        return (badge["criteriaType"], tuple(sorted(badge["criteriaValue"].items())))
    taken = {key_of(b) for b in existing}

    clash = [e[0] for e in NEW if e[0] in ids]
    if clash:
        print(f"  REFUSING: id(s) already present: {', '.join(clash)}")
        return 1
    restated = [e[0] for e in NEW if (e[3], tuple(sorted(e[4].items()))) in taken]
    if restated:
        print(f"  REFUSING: restates an existing threshold: {', '.join(restated)}")
        return 1

    base = max(b["sortOrder"] for b in existing) + 10
    added = [
        {"id": i, "name": n, "description": d, "iconEmoji": e,
         "criteriaType": ct, "criteriaValue": cv, "sortOrder": base + k}
        for k, (i, n, e, ct, cv, d) in enumerate(NEW)
    ]
    total = len(existing) + len(added)
    print(f"  adding {len(added)}; result {total} authored badges (sortOrder {base}..{base + len(added) - 1})")
    if total != 100:
        print(f"  REFUSING: the result is {total}, not 100")
        return 1

    if args.dry_run:
        print("  dry run: nothing written")
        return 0

    ssh(f"cp -a {TARGET} {TARGET}.before-trackstarted")
    text = json.dumps(existing + added, indent=2, ensure_ascii=False) + "\n"
    if ssh(f"cat > {TARGET}", text.encode("utf-8")).returncode != 0:
        print("  write failed")
        return 1
    verify = (ssh(f"python3 -c \"import json;print(len(json.load(open('{TARGET}',encoding='utf-8'))))\""
                  ).stdout or b"").decode().strip()
    print(f"  written; the file parses with {verify} entries")
    return 0 if verify == "100" else 1


if __name__ == "__main__":
    sys.exit(main())
