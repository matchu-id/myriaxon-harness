#!/usr/bin/env python3
"""Confirm the live questions are the reworked content rather than the originals.

The shapes match the objective exactly -- 500 questions per track, 21 multiple-choice plus 4 code for
levels 1 to 10, 18 plus 4 plus 3 for levels 11 to 20 -- but a shape can be reached by the *original* content
if it happened to be authored that way. What distinguishes them is the text.

Each level's first question is compared against the workspace file it came from. The originals were full of
`utils`, `helpers` and `misc` reorganisation questions; the rework is case studies about Loomis Market. So
this prints the live prompt for a few levels beside the workspace prompt, which is enough to tell them
apart without transferring anything.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(r"C:\Users\arian\OneDrive\ドキュメント\Myriaxon")
HOST = "admin-certivo@139.190.97.11"
DB = "certivo-db-1"
USER = "certivo"
DATABASE = "certivo"

# A phrase per level that exists only in the rework, used as an existence check rather than a comparison.
MARKERS = {
    "fullstack-developer": [
        (1, "First Product Page"),
        (8, "500 Products and a Search Box"),
        (16, "3MB Bundle"),
        (20, "Architecture Review Board"),
    ],
    "ai-ml-engineer": [],
}


def psql(sql: str, timeout: int = 300) -> str:
    result = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=25", HOST,
         f"docker exec {DB} psql -U {USER} -d {DATABASE} -t -A -F'|' -c \"{sql}\""],
        capture_output=True, text=True, encoding="utf-8", timeout=timeout,
    )
    return ((result.stdout or "") + (result.stderr or "")).strip()


def main() -> int:
    print("  === first question of selected fullstack levels, live vs workspace ===")
    for level, marker in MARKERS["fullstack-developer"]:
        live = psql(
            "SELECT left(q.prompt_markdown, 78) FROM questions q "
            f"WHERE q.exam_id = 'fullstack-developer.L{level:02d}.exam' "
            "ORDER BY q.sort_order, q.id LIMIT 1;"
        )
        path = ROOT / "certivo-rework" / "fullstack-developer" / "levels" / f"{level:02d}.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        workspace = data["questions"][0]["prompt"][:78]
        print(f"    L{level:02d} live     : {live.replace(chr(10), ' ')[:78]}")
        print(f"    L{level:02d} workspace: {workspace}")
        print()

    print("  === does the case-study marker appear anywhere in the live level? ===")
    for level, marker in MARKERS["fullstack-developer"]:
        escaped = marker.replace("'", "''")
        count = psql(
            f"SELECT count(*) FROM questions WHERE exam_id = 'fullstack-developer.L{level:02d}.exam' "
            f"AND (prompt_markdown ILIKE '%{escaped}%' OR explanation_markdown ILIKE '%{escaped}%');"
        )
        print(f"    L{level:02d} \"{marker}\": {count} question(s)")

    print()
    print("  === does the ORIGINAL content survive anywhere? (level 20 originally had 22 questions) ===")
    originals = psql(
        "SELECT count(*) FROM questions WHERE exam_id IN "
        "('fullstack-developer.L20.exam', 'fullstack-developer.L18.exam');"
    )
    print(f"    L18 + L20 questions live: {originals} (the rework gives 25 each = 50)")

    print()
    print("  === code questions carry setupSql and hidden tests? ===")
    code = psql(
        "SELECT count(*) FILTER (WHERE code_spec_json IS NOT NULL) AS with_spec, "
        "count(*) FILTER (WHERE code_spec_json::text LIKE '%hidden%') AS with_hidden, "
        "count(*) FILTER (WHERE code_spec_json::text LIKE '%setupSql%') AS with_setup "
        "FROM questions WHERE exam_id LIKE 'fullstack-developer.%' AND kind = 'code';"
    )
    print(f"    code={code}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
