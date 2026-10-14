"""Add PiHarness and Opencode columns to the capability matrix.

Transforms `| Capability | MYX | OC | DSH | CC | Notes |` into a seven-column table with `PI` and `OCD` inserted,
across all eight numbered comparison sections. Every data row gets `❓` initially, because a row nobody has verified
must not read as "absent" -- the difference between "does not have it" and "not checked" is the whole reason this
document has an `unknown` mark at all.
"""

from __future__ import annotations

import re
from pathlib import Path

MATRIX = Path(r"C:\Users\arian\OneDrive\ドキュメント\Myriaxon\docs\CAPABILITY_MATRIX.md")

OLD_HEADER = "| Capability | MYX | OC | DSH | CC | Notes |"
NEW_HEADER = "| Capability | MYX | OC | DSH | CC | PI | OCD | Notes |"
OLD_SEP = "|---|---|---|---|---|---|"
NEW_SEP = "|---|---|---|---|---|---|---|---|"


def main() -> int:
    lines = MATRIX.read_text(encoding="utf-8").splitlines()
    out: list[str] = []
    in_section = False
    changed_rows = 0
    changed_headers = 0

    for line in lines:
        if line.startswith("## "):
            in_section = bool(re.match(r"## [1-8]\.", line))
            out.append(line)
            continue

        if in_section and line.strip() == OLD_HEADER:
            out.append(NEW_HEADER)
            changed_headers += 1
            continue

        if in_section and line.strip() == OLD_SEP:
            out.append(NEW_SEP)
            continue

        # A data row inside a comparison section has SIX cells: capability + four harness marks + notes.
        #
        # Two earlier attempts got this wrong in opposite directions. The first required 6 pipes and matched
        # nothing; the second required 5 CELLS and also matched nothing. Both were guesses at the shape of a table
        # the script should have been reading. Counting the cells and printing them is what settled it: the original
        # header `| Capability | MYX | OC | DSH | CC | Notes |` is itself 6 cells, so the separator's 7 pipes were
        # always one more than the data rows' 6 -- the tables were already inconsistent and nobody had noticed.
        if in_section and line.startswith("|"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            if (len(cells) == 6
                    and not any(set(c) <= set("-: ") and c for c in cells)
                    and cells[0].lower() != "capability"):
                capability, myx, oc, dsh, cc, notes = cells
                # `❓` for both new columns: unverified, not absent. The scorer counts it as `unknown`, which
                # excludes it from the base rather than scoring it zero.
                out.append(
                    f"| {capability} | {myx} | {oc} | {dsh} | {cc} | ❓ | ❓ | {notes} |"
                )
                changed_rows += 1
                continue

        out.append(line)

    MATRIX.write_text("\n".join(out) + "\n", encoding="utf-8", newline="\n")
    print(f"  headers rewritten : {changed_headers}")
    print(f"  data rows given PI/OCD cells: {changed_rows}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
