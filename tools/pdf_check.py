"""Verify the print files: page geometry, decided from each PDF's own MediaBox.

The first version of this check read a hardcoded filename and compared it to A4, so it reported a
failure for a *different, smaller* PDF and could not have been right for more than one canvas. This
walks every PDF in the output directory and checks each one against the size its own canvas implies.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from myriaxon.poster import SIZES, print_dpi  # noqa: E402

OUT = REPO / "bench-out" / "posters"
PT_PER_INCH = 72.0
failures = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global failures
    if not ok:
        failures += 1
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}{('  -- ' + detail) if detail and not ok else ''}")


def media_box(raw: bytes) -> tuple[float, float] | None:
    match = re.search(rb"/MediaBox\s*\[([^\]]+)\]", raw)
    if not match:
        return None
    values = [float(v) for v in match.group(1).split()]
    return values[2] - values[0], values[3] - values[1]


def main() -> int:
    from myriaxon.poster import PosterSpec, design_poster

    print("writing one PDF per canvas size")
    produced: list[tuple[str, tuple[float, float]]] = []
    for size in SIZES:
        result = design_poster(
            PosterSpec(title=f"{size} geometry", eyebrow="PRINT", size=size),
            OUT, stem=f"geom-{size}", formats=["pdf"],
        )
        if result.page_pt:
            produced.append((size, result.page_pt))
            width_px, height_px = SIZES[size]
            inches = (result.page_pt[0] / 72, result.page_pt[1] / 72)
            print(f"  {size:9} {width_px}x{height_px}px @ {print_dpi(size)} dpi "
                  f"-> {inches[0]:.1f}x{inches[1]:.1f} in")
        else:
            check(f"{size} produced a PDF", False, "no page_pt reported")

    print("\nreading each page box back out of the file")
    for size, expected in produced:
        path = OUT / f"geom-{size}.pdf"
        if not path.is_file():
            check(f"{size} file exists", False, str(path))
            continue
        raw = path.read_bytes()
        box = media_box(raw)
        if box is None:
            check(f"{size} declares a MediaBox", False)
            continue
        # One point of tolerance: the canvas grid does not divide evenly into points.
        ok = abs(box[0] - expected[0]) < 2 and abs(box[1] - expected[1]) < 2
        check(f"{size} page is {expected[0]:.0f}x{expected[1]:.0f} pt",
              ok, f"file says {box[0]:.0f}x{box[1]:.0f}")
        # The mistake this guards against: pixels written as points, giving a 25-inch "A4".
        check(f"{size} is not a pixels-as-points page",
              box[0] < SIZES[size][0] + 2, f"{box[0]:.0f} pt")

    print()
    print("pdf geometry verified" if not failures else f"{failures} check(s) FAILED")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
