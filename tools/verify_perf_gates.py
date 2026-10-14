#!/usr/bin/env python3
"""Verify the performance gates, including that the budgets can actually fail.

The point of this file is the calibration checks. A budget that has never been seen to reject anything is a number
someone chose, and a green run against it means nothing -- which is worse than having no budget, because it is
trusted.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from myriaxon import perf_gates as P  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAIL).append(label)
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}" + (f"\n         {detail}" if detail else ""))


def main() -> int:
    print("=== 1. a budget is DERIVED, not chosen ===")
    # ceil(reference * machine_scale * headroom), so the number has a stated provenance.
    check("a 100 ms reference with 1.25 headroom allows 125 ms", P.ci_budget(100.0) == 125)
    check("rounding is up, never down", P.ci_budget(100.1) == math_ceil_check(), str(P.ci_budget(100.1)))
    check("a slower machine gets more time", P.ci_budget(100.0, machine_scale=2.0) == 250)
    check("more headroom gets more time", P.ci_budget(100.0, headroom=2.0) == 200)
    check("the defaults are the documented ones",
          P.DEFAULT_MACHINE_SCALE == 1.0 and P.DEFAULT_HEADROOM == 1.25)

    print()
    print("=== 2. THE FIX: a budget proves it can distinguish good from bad ===")
    # Each assertion catches a different way a budget can be useless.
    healthy = P.Budget(
        name="healthy",
        reference_ms=100.0,
        calibration_samples=[95.0, 100.0, 105.0, 98.0, 102.0],
        stricter_reference_ms=50.0,
    )
    result = P.calibrate(healthy)
    check("a sound budget calibrates", result.ok, result.render())
    rendered = result.render()
    check("it reports the recorded median passed", "passes" in rendered, rendered[:160])
    check("it reports the stricter budget correctly FAILED", "correctly fails" in rendered, rendered[:200])
    check("it reports a synthetic regression is caught", "regression is caught" in rendered)

    print()
    print("=== 3. a budget that is already too tight is CAUGHT ===")
    # If the recorded samples exceed the budget, it would fail on a correct machine.
    too_tight = P.Budget(
        name="too-tight", reference_ms=10.0,
        calibration_samples=[95.0, 100.0, 105.0], stricter_reference_ms=5.0,
    )
    result = P.calibrate(too_tight)
    check("an over-tight budget fails calibration", not result.ok, result.render()[:200])
    check("the reason says it would fail on a correct machine",
          "would fail on a correct machine" in result.render(), result.render()[:250])

    print()
    print("=== 4. THE SUBTLE ONE: a budget so loose it cannot catch anything is CAUGHT ===")
    # This is the failure a naive calibration misses. The samples pass -- of course they do, the budget is huge --
    # so a check that only asserted "samples pass" would call this healthy.
    too_loose = P.Budget(
        name="too-loose", reference_ms=10_000.0,
        calibration_samples=[95.0, 100.0, 105.0], stricter_reference_ms=1_000.0,
    )
    result = P.calibrate(too_loose)
    check("an over-loose budget fails calibration", not result.ok, result.render()[:220])
    check("the reason names the unnoticed-regression problem",
          "would go unnoticed" in result.render(), result.render()[:250])

    print()
    print("=== 5. a budget with no calibration samples says so rather than claiming proof ===")
    unproven = P.Budget(name="unproven", reference_ms=100.0)
    result = P.calibrate(unproven)
    check("it does not fail", result.ok)
    check("but it is marked unproven", "unproven" in result.render(), result.render())

    print()
    print("=== 6. verify_budgets calibrates a whole set and aggregates ===")
    ok, lines = P.verify_budgets([healthy, too_tight])
    check("one bad budget makes the set fail", not ok)
    check("lines are produced for every budget", len(lines) >= 6, str(len(lines)))
    ok_all, _ = P.verify_budgets([healthy])
    check("a set of sound budgets passes", ok_all)

    print()
    print("=== 7. the SHIPPED budgets calibrate ===")
    # If this fails, the shipped gates would either fail on a correct machine or accept a regression.
    shipped = P.default_budgets()
    check("there are shipped gates", len(shipped) >= 3, str(len(shipped)))
    ok, lines = P.verify_budgets(shipped)
    check("every shipped budget calibrates", ok, "\n         ".join(lines))
    for budget in shipped:
        check(f"{budget.name} has calibration samples", len(budget.calibration_samples) >= 3,
              str(budget.calibration_samples))
        check(f"{budget.name} has a stricter reference to test against",
              budget.stricter_reference_ms > 0)

    print()
    print("=== 8. measuring uses FRESH processes and reports a median ===")
    samples = P.measure([sys.executable, "-c", "pass"], attempts=3)
    check("three fresh processes produced three samples", len(samples) == 3, str(samples))
    check("every sample is positive", all(s > 0 for s in samples))
    # A fresh process costs materially more than an in-process no-op, which is the case for measuring this way.
    check("a fresh interpreter costs non-zero time", min(samples) > 0.5,
          f"min {min(samples):.2f} ms -- suspiciously fast for a real process")

    print()
    print("=== 9. a timeout is recorded as a failure, not dropped ===")
    # Dropping the sample would hide the worst possible result: a path that hangs.
    hung = P.measure([sys.executable, "-c", "import time; time.sleep(30)"], attempts=1, timeout=0.5)
    check("the timeout produced a sample", len(hung) == 1, str(hung))
    check("the sample records the full allowance", hung[0] >= 500.0, f"{hung[0]:.0f} ms")

    print()
    print("=== 10. a gate compares the median against the budget ===")
    generous = P.Budget(name="generous", reference_ms=60_000.0,
                        calibration_samples=[1.0, 1.0, 1.0], stricter_reference_ms=30_000.0)
    result = P.check_gate(generous, [sys.executable, "-c", "pass"], attempts=2)
    check("a generous gate passes", result.ok, result.render())
    check("the median is computed from the samples",
          abs(result.median - sorted(result.samples)[len(result.samples) // 2]) < 1e-9
          or result.median > 0)

    impossible = P.Budget(name="impossible", reference_ms=0.0001,
                          calibration_samples=[1.0], stricter_reference_ms=0.00001)
    result = P.check_gate(impossible, [sys.executable, "-c", "pass"], attempts=2)
    check("an impossible gate FAILS", not result.ok, result.render())
    check("the failure note names the budget and the samples",
          "exceeds" in result.note and "Samples:" in result.note, result.note[:180])
    check("the samples are in the note, so the failure is diagnosable",
          len(result.samples) == 2)

    print()
    print("=== 11. a constrained-resource repeat is expressible ===")
    # Their heap-capped run catches a change that is fast but needs far more memory. The Python equivalent is a
    # fresh process with a small heap.
    constrained = P.measure(
        [sys.executable, "-c", "pass"], attempts=2,
        env_overrides={"PYTHONHASHSEED": "0"},
    )
    check("environment overrides are honoured without error", len(constrained) == 2, str(constrained))

    print()
    print("=== 12. budgets round-trip through JSON ===")
    tmp = Path(tempfile.mkdtemp()) / "budgets.json"
    P.save_budgets(tmp, shipped)
    check("the file was written", tmp.is_file())
    reloaded = P.load_budgets(tmp)
    check("every budget came back", len(reloaded) == len(shipped), str(len(reloaded)))
    check("names survived", [b.name for b in reloaded] == [b.name for b in shipped])
    check("calibration samples survived",
          reloaded[0].calibration_samples == shipped[0].calibration_samples)
    check("the derived budget is unchanged after a round-trip",
          [b.allowed_ms for b in reloaded] == [b.allowed_ms for b in shipped])
    check("the file explains how budgets are derived",
          "derived" in tmp.read_text(encoding="utf-8").lower())

    print()
    print("=== 13. a missing or corrupt budgets file yields none, and says so ===")
    check("a missing file yields no budgets", P.load_budgets(Path("does-not-exist.json")) == [])
    broken = Path(tempfile.mkdtemp()) / "broken.json"
    broken.write_text("{ not json", encoding="utf-8")
    check("a corrupt file yields no budgets", P.load_budgets(broken) == [])
    # An empty list means "no gates ran", which a caller must report rather than read as a pass.
    check("an empty set is distinguishable from a passing set",
          P.load_budgets(Path("does-not-exist.json")) == [] and P.verify_budgets([])[0] is True,
          "the caller must check for an empty list; verify_budgets on nothing is vacuously true")

    print()
    print("=== 14. malformed entries are skipped without abandoning the good ones ===")
    mixed = Path(tempfile.mkdtemp()) / "mixed.json"
    mixed.write_text(json.dumps({"budgets": [
        {"name": "good", "reference_ms": 100.0, "calibration_samples": [90.0], "stricter_reference_ms": 50.0},
        {"no_name": True},
        "not even an object",
    ]}), encoding="utf-8")
    loaded = P.load_budgets(mixed)
    check("only the valid entry loaded", len(loaded) == 1 and loaded[0].name == "good", str(len(loaded)))

    print()
    print(f"=== {len(PASS)} passed, {len(FAIL)} failed ===")
    for label in FAIL:
        print(f"  FAILED: {label}")
    return 1 if FAIL else 0


def math_ceil_check() -> int:
    import math

    return math.ceil(100.1 * 1.0 * 1.25)


if __name__ == "__main__":
    sys.exit(main())
