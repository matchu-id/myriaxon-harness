"""Tally the capability matrix into a per-harness score.

Reads the 151 comparison rows in docs/CAPABILITY_MATRIX.md and counts each column's marks. Written as a script
rather than done by eye because the whole point of this repository's metric work is that a remembered count is
usually wrong.

Mark vocabulary, taken from the matrix's own "How to read this" section:
  full    -- the harness has it
  partial -- present but narrower, or measured-but-not-enforced
  absent  -- the reference tree does not contain it
  n/a     -- the artefact does not exist in that harness, which is a fact about its architecture
"""

import re
from pathlib import Path

MATRIX = Path(r"C:\Users\arian\OneDrive\ドキュメント\Myriaxon\docs\CAPABILITY_MATRIX.md")

FULL = ("✅",)
PARTIAL = ("🔶", "⏳")
ABSENT = ("❌",)
NA = ("➖",)
UNKNOWN = ("❓", "⚠")

COLUMNS = ("Myriaxon", "OpenClaw", "DeepSeek", "ClaudeCode", "PiHarness", "Opencode")


def classify(cell: str) -> str:
    for mark, name in ((FULL, "full"), (PARTIAL, "partial"), (NA, "na"), (ABSENT, "absent"),
                       (UNKNOWN, "unknown")):
        if any(m in cell for m in mark):
            return name
    return "blank"


def main() -> int:
    lines = MATRIX.read_text(encoding="utf-8").splitlines()

    # Only rows inside the numbered comparison sections (## 1..## 8), never the ledger or the metrics tables.
    in_section = False
    rows = []
    for line in lines:
        if line.startswith("## "):
            in_section = bool(re.match(r"## [1-8]\.", line))
            continue
        if not in_section or not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 5:
            continue
        if any(set(c) <= set("-: ") and c for c in cells):
            continue
        if cells[0].lower().startswith("capability"):
            continue
        rows.append(cells)

    print(f"  comparison rows in §1-§8: {len(rows)}")
    print()
    header = f"  {'harness':<12}" + "".join(f"{name:>10}" for name in ("full", "partial", "absent", "n/a", "unknown"))
    print(header)
    print("  " + "-" * (len(header) - 2))

    totals = {}
    for index, name in enumerate(COLUMNS, start=1):
        tally = {k: 0 for k in ("full", "partial", "absent", "na", "unknown", "blank")}
        for cells in rows:
            if index >= len(cells):
                tally["unknown"] += 1
                continue
            tally[classify(cells[index])] += 1
        totals[name] = tally
        print(f"  {name:<12}" + "".join(f"{tally[k]:>10}" for k in
                                        ("full", "partial", "absent", "na", "unknown")))

    print()
    print("  weighted score: full = 1, partial = 0.5, over rows that APPLY (absent + n/a excluded from the base)")
    print()
    print(f"  {'harness':<12}{'score':>8}{'of':>7}{'pct':>8}")
    print("  " + "-" * 33)
    for name, tally in totals.items():
        applies = tally["full"] + tally["partial"] + tally["absent"]
        if not applies:
            print(f"  {name:<12}{'n/a':>8}")
            continue
        score = tally["full"] + 0.5 * tally["partial"]
        print(f"  {name:<12}{score:>8.1f}{applies:>7}{100 * score / applies:>7.1f}%")

    print()
    print("  also over ALL rows, counting 'absent' and 'n/a' as zero and unknown rows as excluded:")
    print()
    print(f"  {'harness':<12}{'known':>7}{'full':>7}{'pct':>8}")
    print("  " + "-" * 31)
    for name, tally in totals.items():
        known = tally["full"] + tally["partial"] + tally["absent"] + tally["na"]
        if not known:
            continue
        score = tally["full"] + 0.5 * tally["partial"]
        print(f"  {name:<12}{known:>7}{tally['full']:>7}{100 * score / known:>7.1f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
