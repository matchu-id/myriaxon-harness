#!/usr/bin/env python3
"""Check the type-stripper against the real reference file.

The stripper removes parameter and return annotations from the reference before Node runs it. Four regex
attempts each removed something they should have kept, so this prints the stripped output for the actual
reference and then runs every function against one case, which is the only evidence that the
transformation is faithful.

Usage:  python3 test_ts_stripper.py
"""

from __future__ import annotations

import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))
from verify_ts_questions import find_node, run_one, strip_types  # noqa: E402

REFERENCE = TOOLS.parent / "certivo-rework" / "fullstack-developer" / "reference.js"

PRODUCTS = [
    {"name": "Desk", "price": 200, "inStock": True},
    {"name": "Lamp", "price": 40, "inStock": True},
    {"name": "Chair", "price": 120, "inStock": False},
    {"name": "Shelf", "price": 90, "inStock": True},
]

CASES = [
    ("inStockAbove", [PRODUCTS, 150], ["Desk"]),
    ("productsUnder", [PRODUCTS, 100], ["Lamp", "Shelf"]),
    ("extractClasses", ['<div class="card wide"></div><b class="card"></b>'], ["card", "wide"]),
    ("formatPrice", [1205], "$12.05"),
    ("scaleToWidth", [800, 600, 400], 300),
    ("cartTotal", [[{"priceCents": 250, "qty": 4}]], 1000),
    ("countByStock", [PRODUCTS], {"inStock": 3, "outOfStock": 1}),
    (
        "countInStockByCategory",
        [[{"name": "A", "category": "x", "inStock": True},
          {"name": "B", "category": "y", "inStock": False}]],
        {"x": 1, "y": 0},
    ),
    ("filterProducts", [PRODUCTS, "la"], [{"name": "Lamp", "price": 40, "inStock": True}]),
    (
        "mergeCartItems",
        [[{"id": 1, "qty": 2}], [{"id": 1, "qty": 3}, {"id": 2, "qty": 1}]],
        [{"id": 1, "qty": 5}, {"id": 2, "qty": 1}],
    ),
]


def main() -> int:
    node = find_node()
    if node is None:
        print("  no node binary, cannot run")
        return 1

    source = REFERENCE.read_text(encoding="utf-8")
    try:
        stripped = strip_types(source)
    except ValueError as error:
        print(f"  the stripper refused the reference: {error}")
        return 1

    print("  --- stripped output, parameter lists only ---")
    for line in stripped.splitlines():
        if line.startswith("function "):
            print(f"  | {line}")
    print("  --- results ---")

    failures = 0
    for entry, args, expected in CASES:
        ok, actual = run_one(node, stripped, entry, args)
        if ok and actual == expected:
            print(f"  {entry}: ok")
        else:
            failures += 1
            print(f"  {entry}: FAILED -- expected {expected!r}, got {actual!r}")

    print()
    print(f"  {len(CASES) - failures}/{len(CASES)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
