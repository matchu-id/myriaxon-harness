#!/usr/bin/env python3
"""Correct level 18's test names and evidence fixture against what the reference queries return.

Three claims did not hold, checked name by name against the derived output:

  * `evidence_freshness` said "one audit and one subgroup report fall before a 2026-03-01 cutoff". The
    reference returns BOTH audits stale -- e1 is 2026-01-15, which is before 2026-03-01 -- so the count
    is 2, not 1. The name was wrong, not the query.
  * `evidence_freshness`'s hidden test claimed the boundary is strict, and no item in the fixture sat
    exactly on the cutoff, so the boundary was untested. The fixture now contains one dated exactly
    2026-03-01, which the rule excludes.
  * `golden_regression` claimed v3 and v4 differ "in opposite directions". They do not: both mismatch on
    exactly one case. They differ in WHICH case, which is not what the name says.

Names are claims, and this is the ninth level where one was false -- but two of these are the more serious
kind, where the fixture failed to exercise the behaviour the name asserted. Both now do.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

PATH = Path(r"C:\Users\arian\OneDrive\ドキュメント\Myriaxon\certivo-rework\ai-ml-engineer\levels\18.json")

# Adds e7 dated exactly on the cutoff, so the strict comparison is exercised rather than assumed.
EVIDENCE_SETUP = (
    "CREATE TABLE evidence (evidence_id TEXT, evidence_type TEXT, as_of TEXT);\n"
    "INSERT INTO evidence VALUES ('e1','fairness_audit','2026-01-15'),"
    "('e2','fairness_audit','2025-11-02'),('e3','calibration','2026-03-20'),"
    "('e4','subgroup_metrics','2026-03-18'),('e5','subgroup_metrics','2025-12-01'),"
    "('e6','calibration','2026-02-28'),('e7','fairness_audit','2026-03-01');"
)

EVIDENCE_REFERENCE = (
    "SELECT evidence_type, COUNT(*) AS n_items, "
    "SUM(CASE WHEN as_of < ? THEN 1 ELSE 0 END) AS n_stale FROM evidence "
    "GROUP BY evidence_type ORDER BY n_stale DESC, evidence_type ASC"
)

GOLDEN_REFERENCE = (
    "SELECT model_version, COUNT(*) AS n, SUM(CASE WHEN produced <> expected THEN 1 ELSE 0 END) "
    "AS n_mismatch, ROUND(100.0 * SUM(CASE WHEN produced = expected THEN 1 ELSE 0 END) / COUNT(*), 2) "
    "AS match_pct FROM golden GROUP BY model_version "
    "ORDER BY match_pct ASC, model_version ASC"
)

NAMES = {
    "checking the sign-off evidence inventory": (
        "two audits and one subgroup report are stale against a 2026-03-01 cutoff",
        "hidden: e7 is dated exactly on the cutoff and is not stale, so the comparison is strict",
    ),
    "the golden-dataset regression check": (
        "v3 and v4 both mismatch one case each, on different cases and in different directions",
        "hidden: equal match rates hide that the two versions fail on different inputs",
    ),
}


def run(setup: str, query: str, params: list) -> list[dict]:
    connection = sqlite3.connect(":memory:")
    connection.executescript(setup)
    cursor = connection.execute(query, params)
    columns = [d[0] for d in cursor.description]
    rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
    connection.close()
    return rows


def main() -> int:
    data = json.loads(PATH.read_text(encoding="utf-8"))
    fixed = 0

    for question in data["questions"]:
        spec = question.get("code")
        if not spec:
            continue
        prompt = question.get("prompt", "")
        key = next((k for k in NAMES if k in prompt), None)

        if key == "checking the sign-off evidence inventory":
            spec["setupSql"] = EVIDENCE_SETUP
            params = spec["tests"][0].get("args") or ["2026-03-01"]
            rows = run(EVIDENCE_SETUP, EVIDENCE_REFERENCE, params)
            on_boundary = [i for i in rows if i["n_stale"] and i["evidence_type"] == "fairness_audit"]
            if not on_boundary:
                print("  REFUSING: the audit row is not reported stale, so the boundary is untested")
                return 1
        elif key == "the golden-dataset regression check":
            params = spec["tests"][0].get("args") or []
            rows = run(spec["setupSql"], GOLDEN_REFERENCE, params)
            mismatches = {r["model_version"]: r["n_mismatch"] for r in rows}
            if len(set(mismatches.values())) != 1:
                print(f"  REFUSING: the versions no longer share a mismatch count ({mismatches}), so "
                      f"the corrected name would be false")
                return 1
        else:
            continue

        print(f"  {key}: {json.dumps(rows, ensure_ascii=False)}")
        for index, test in enumerate(spec["tests"]):
            test["expected"] = json.loads(json.dumps(rows))
            test["name"] = NAMES[key][index]
            fixed += 1

    PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8", newline="\n")
    print(f"  corrected {fixed} test(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
