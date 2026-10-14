#!/usr/bin/env python3
"""Show exactly what each stripping rule removes from a sample.

Reasoning about a chain of regexes has now produced three wrong fixes in a row, so this prints the result
of each rule in turn. When a rule changes the text, it says what it matched.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

SAMPLES = [
    "function mergeCartItems(existing: MergeItem[], incoming: MergeItem[]): MergeItem[] {",
    "  const merged: MergeItem[] = existing.map((item) => ({ id: item.id, qty: item.qty }));",
    "  const found = merged.find((candidate) => candidate.id === item.id);",
    "function countByStock(products: StockProduct[]): { inStock: number; outOfStock: number } {",
    "  const result = { inStock: 0, outOfStock: 0 };",
    "function totalPrice(products: Product[]): number {",
]

RULES = [
    ("interface blocks", re.compile(r"^\s*(?:export\s+)?interface\s+\w+\s*\{[^}]*\}\s*$", re.M | re.S)),
    ("type aliases", re.compile(r"^\s*(?:export\s+)?type\s+\w+\s*=[^;]+;\s*$", re.M)),
    ("as casts", re.compile(r"\s+as\s+(?:const|[A-Za-z_][\w.<>\[\]| ]*)")),
    ("inline object types", re.compile(r":\s*\{\s*(?:[A-Za-z_]\w*\s*[?!]?\s*:|})[^{}]*\}")),
    ("annotations", re.compile(r":\s+[A-Za-z_][\w.<>\[\]|, ]*(?=\s*[,)=;{])")),
]


def main() -> int:
    sample = "  const merged: MergeItem[] = existing.map((item) => ({ id: item.id, qty: item.qty }));"
    print(f"  IN : {sample}")
    rule = re.compile(r":\s+[A-Za-z_][\w.<>\[\]|, ]*(?=\s*[,)=;{])")
    for match in rule.finditer(sample):
        print(f"    match at {match.start()}-{match.end()}: {match.group(0)!r}")
    print(f"  OUT: {rule.sub('', sample)}")
    print()
    for sample in SAMPLES:
        print(f"  IN : {sample}")
        text = sample
        for name, rule in RULES:
            new_text, count = rule.subn("", text)
            if count:
                print(f"    {name}: {count} replacement(s)")
                print(f"      -> {new_text}")
                text = new_text
        print(f"  OUT: {text}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
