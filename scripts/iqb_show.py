"""Spot-check why specific IQB-14 instances failed, straight from the stored artifacts.

    python scripts/iqb_show.py <artifact-dir> [task-id-substring]
"""
import json
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    out_dir = Path(sys.argv[1])
    needle = sys.argv[2] if len(sys.argv) > 2 else None
    records = []
    for line in (out_dir / "instances.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            records.append(json.loads(line))
    for record in records:
        if needle and needle not in record["task_id"]:
            continue
        state = record["state"]
        print("=" * 100)
        print(f"{record['task_id']}  attempt={record['attempt']} variant={record['variant']}  "
              f"status={state['status']}  passed={record['passed']}  steps={state['extras'].get('steps')}")
        turns = state.get("turns") or []
        if isinstance(turns, int):  # artifacts written before turn texts were persisted
            reply = ""
            print(f"--- REPLY: not stored in this artifact ({turns} turn(s)) ---")
        else:
            reply = (turns[-1].get("final_text") if turns else "") or ""
            print(f"--- REPLY ({len(reply)} chars) ---")
            print(reply[:1500] if reply else "(empty)")
        print("--- ASSERTIONS ---")
        for assertion in record["assertions"]:
            mark = "PASS" if assertion["passed"] else "FAIL"
            print(f"  [{mark}] {assertion['type']:<16} {assertion['parameter']:<22} score={assertion['score']}")
            print(f"         {assertion['evidence'][:300]}")
        if record["error"]:
            print("--- ERROR ---")
            print(record["error"][:500])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
