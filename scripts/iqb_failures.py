"""Show the capability curve and every failing assertion from a scorecard JSON.

    python scripts/iqb_failures.py bench-out/<run>/<label>.scorecard.json
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))

    curve = data.get("complexity_curve") or {}
    if curve:
        print("capability curve by difficulty:")
        for level, row in sorted(curve.items()):
            rate = row.get("pass_rate")
            shown = "n/a" if rate is None else f"{rate:.2f}"
            print(f"  {level}  tasks={row.get('tasks', 0):<3} instances={row.get('instances', 0):<3} pass_rate={shown}")
        print()

    print("failing assertions:")
    total = 0
    for entry in data.get("parameters", []):
        for failure in (entry.get("detail") or {}).get("failing_assertions", []):
            total += 1
            flag = "" if failure.get("critical", True) else "  [advisory]"
            print(f"  {entry['name']:<28} {failure['task']:<44} {failure['type']:<16} {failure['evidence'][:120]}{flag}")
    if not total:
        print("  (none)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
