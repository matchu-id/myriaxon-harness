#!/usr/bin/env python3
"""Count the live questions per level for the two reworked tracks.

Three attempts were needed to get this query right, and each failure was a wrong assumption about the
schema rather than a wrong question:

  * `questions.level_id` does not exist -- the path is `questions.exam_id -> exams.level_id -> levels.id`.
  * `tracks.slug` does not exist; `tracks.name` holds the slug, with `tracks.id` also being one.
  * filtering on `t.name` matched nothing even though the rows were plainly there, so the join is keyed on
    `track_id` instead -- which is what the foreign keys actually say, and removes the question entirely.

The identifiers are readable strings: `ai-ml-engineer.L01.exam.q01` for a question. That makes the counts
easy to check against the workspace's own files, which is the point.
"""

from __future__ import annotations

import subprocess
import sys

HOST = "admin-certivo@139.190.97.11"
DB = "certivo-db-1"
USER = "certivo"
DATABASE = "certivo"

PER_LEVEL = """
SELECT l.track_id, l.level_number, count(q.id) AS n,
       count(*) FILTER (WHERE q.kind = 'multiple_choice') AS mc,
       count(*) FILTER (WHERE q.kind = 'code')            AS code,
       count(*) FILTER (WHERE q.kind = 'essay')           AS essay
FROM levels l
LEFT JOIN exams e     ON e.level_id = l.id
LEFT JOIN questions q ON q.exam_id = e.id
WHERE l.track_id IN ('fullstack-developer', 'ai-ml-engineer')
GROUP BY l.track_id, l.level_number
ORDER BY l.track_id, l.level_number;
"""


def psql(sql: str, timeout: int = 300) -> str:
    result = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=25", HOST,
         f"docker exec {DB} psql -U {USER} -d {DATABASE} -t -A -F'|' -c \"{sql}\""],
        capture_output=True, text=True, encoding="utf-8", timeout=timeout,
    )
    return ((result.stdout or "") + (result.stderr or "")).strip()


def main() -> int:
    print(f"    {'track':<22}{'lvl':>4}{'total':>7}{'mc':>5}{'code':>6}{'essay':>7}")
    current = None
    totals: dict[str, list[int]] = {}
    for line in psql(PER_LEVEL).splitlines():
        parts = line.split("|")
        if len(parts) != 6:
            print(f"    {line}")
            continue
        track, level, n, mc, code, essay = parts
        if track != current:
            print()
            current = track
        print(f"    {track:<22}{level:>4}{n:>7}{mc:>5}{code:>6}{essay:>7}")
        acc = totals.setdefault(track, [0, 0, 0, 0])
        for index, value in enumerate((n, mc, code, essay)):
            acc[index] += int(value)

    print()
    print("  === totals ===")
    for track, (n, mc, code, essay) in sorted(totals.items()):
        print(f"    {track:<22} total={n:<5} mc={mc:<5} code={code:<4} essay={essay}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
