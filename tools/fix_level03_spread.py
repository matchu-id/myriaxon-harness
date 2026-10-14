#!/usr/bin/env python3
"""Fix level 3's spread fixture, whose "beyond two standard deviations" claim did not hold.

The hidden test was named "one very large claim sits beyond two standard deviations, as a normal
distribution would rarely allow", and the reference returned `beyond_2sd: 0` — the large claim did **not**
qualify. It missed by 0.5: the deviation was 3908.0 against a threshold of 3908.5.

Two attempts to fix it by enlarging the single outlier both failed, and the reason is worth writing down
because it is a fact about the statistic rather than about the fixture. A lone extreme value cannot clear
two standard deviations, because it inflates the standard deviation it is being measured against: growing
the outlier moves the threshold almost exactly as fast as it moves the deviation. `[100, 110, 120, 130,
30000]` puts the largest deviation at 23908.0 against a threshold of 23908.0 — equal to the last decimal.

What does clear it is a *two-tier* distribution: a cluster of ordinary values and a smaller cluster far
above, so the spread is driven by the gap between the tiers rather than by one point. `[50, 60, 70, 80, 90,
100, 1500]` puts the largest deviation at 1221 against a threshold of 998 — a wide margin, so the test
measures the rule rather than the arithmetic.

This is the same class of defect as level 9's vacuous flag and level 12's missing boundary: a test whose
setup cannot exercise the behaviour its name claims. It is the first one where the setup was *borderline*
rather than plainly wrong, which is worse, because a half-unit margin flips under a trivial change and the
test goes on looking meaningful.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

LEVEL = Path(r"C:\Users\arian\OneDrive\ドキュメント\Myriaxon\certivo-rework\data-scientist\levels\03.json")
REFERENCE = Path(r"C:\Users\arian\OneDrive\ドキュメント\Myriaxon\certivo-rework\data-scientist\reference.py")

SAMPLES = [50, 60, 70, 80, 90, 100, 1500]


def load_reference():
    spec = importlib.util.spec_from_file_location("ds_reference", REFERENCE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    reference = load_reference()
    data = json.loads(LEVEL.read_text(encoding="utf-8"))

    target = next(q for q in data["questions"]
                  if q.get("code", {}).get("entryPoint") == "describe_spread")

    result = reference.describe_spread(SAMPLES)
    print(f"  fixture {SAMPLES} -> {result}")

    mean = sum(SAMPLES) / len(SAMPLES)
    variance = sum((v - mean) ** 2 for v in SAMPLES) / len(SAMPLES)
    sd = variance ** 0.5
    deviation = max(abs(v - mean) for v in SAMPLES)
    print(f"  largest deviation {deviation:.1f} against a 2sd threshold of {2 * sd:.1f}")
    if result["beyond_2sd"] == 0:
        print("  REFUSING: the large claim still does not clear two standard deviations")
        return 1

    test = target["code"]["tests"][1]
    test["args"] = [list(SAMPLES)]
    test["expected"] = result
    test["name"] = (
        f"hidden: one large claim sits {deviation / sd:.1f} deviations out, which a normal "
        f"distribution would rarely allow"
    )

    LEVEL.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                     encoding="utf-8", newline="\n")
    print(f"  wrote; {result['beyond_2sd']} value(s) now genuinely lie beyond two deviations")
    return 0


if __name__ == "__main__":
    sys.exit(main())
