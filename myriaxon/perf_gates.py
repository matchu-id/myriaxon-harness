"""Performance gates with self-tested budgets.

## The gap this closes

`benchmark.py` measures cold start, import cost and tool dispatch, and can `--save` results to a baseline file.
But a baseline file is a *record*, not a gate: nothing fails when a number gets worse, so the file is read by
whoever remembers to read it. That is the difference between having measurements and having a budget.

DeepSeek's harness derives its budgets instead of guessing them (`benchmarks/support/calibration.ts`, verified):

```
CI_TIME_SCALE = 2                    // x64 CI is slower than the arm64 reference machine
PERFORMANCE_BUDGET_HEADROOM = 1.25
ciTimeBudget(ms) = ceil(ms * 2 * 1.25)
```

and — the part worth copying most — **the budgets are themselves tested**. Their calibration module asserts that
a recorded historical sample set fails an old, too-tight budget and passes the new one, and that a synthetic
4000 ms regression is caught. A budget nobody has ever seen fail is a budget that might not work.

Two more of their choices are kept because they remove a whole class of false result:

* **median of several fresh processes**, not a mean of in-process runs. A mean is dragged by one scheduler hiccup,
  and an in-process timing measures a warm interpreter that no user ever starts.
* **a constrained-resource repeat.** Their heap-capped run exists to catch a change that is fast but needs far
  more memory. The Python equivalent that matters here is a fresh process with a small heap: an import that
  starts allocating eagerly shows up as a failure rather than as a slightly larger number.

## What is dropped

The compiled-worker tripwire (a TypeScript-loader concern with no Python analogue), the browser cases, the
`tsdown` build step, and their env-var-proof budget constants. The machine scale factor is kept but is an
explicit parameter rather than a hardcoded CI assumption, because this runs on a person's laptop.
"""

from __future__ import annotations

import json
import math
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

# A different machine is a different measurement. This multiplies the reference budget for a slower host, and is
# explicit rather than assumed: their `CI_TIME_SCALE = 2` is right for their CI runner and wrong here.
DEFAULT_MACHINE_SCALE = 1.0
# Headroom over the reference figure, so ordinary noise does not fail a build.
DEFAULT_HEADROOM = 1.25
# Samples per gate. Odd, so the median is a real sample rather than an average of two.
DEFAULT_ATTEMPTS = 5

BUDGETS_FILE = "budgets.json"


def ci_budget(reference_ms: float, *, machine_scale: float = DEFAULT_MACHINE_SCALE,
              headroom: float = DEFAULT_HEADROOM) -> int:
    """The allowed time for a path whose reference measurement is `reference_ms`.

    Derived rather than chosen, so the number has a stated provenance: a reference measurement, a machine factor
    and a headroom allowance. A budget of "40 ms because 40 looked fine" cannot be argued with when it fails.
    """
    return math.ceil(reference_ms * machine_scale * headroom)


@dataclass
class Budget:
    """One gate: a named path, a reference figure, and the assertion that it is the right figure."""

    name: str
    reference_ms: float
    description: str = ""
    machine_scale: float = DEFAULT_MACHINE_SCALE
    headroom: float = DEFAULT_HEADROOM
    # A sample series that must FAIL a stricter budget and PASS this one. Their calibration self-test, kept
    # because it is the only thing that distinguishes a working budget from a plausible-looking number.
    calibration_samples: list[float] = field(default_factory=list)
    stricter_reference_ms: float = 0.0

    @property
    def allowed_ms(self) -> int:
        return ci_budget(self.reference_ms, machine_scale=self.machine_scale, headroom=self.headroom)

    def render(self) -> str:
        return f"  {self.name:<28} <= {self.allowed_ms:>6} ms  (ref {self.reference_ms:.1f})"


@dataclass
class CalibrationResult:
    ok: bool
    notes: list[str] = field(default_factory=list)

    def render(self) -> str:
        return "\n".join(self.notes) if self.notes else "  calibration holds"


def calibrate(budget: Budget) -> CalibrationResult:
    """Prove the budget can tell a good run from a bad one.

    Three things are asserted, and each catches a different way a budget can be useless:

    * the recorded samples **pass** this budget -- if they do not, the budget is already too tight and will fail
      on a correct machine;
    * the recorded samples **fail** a stricter budget -- if they do not, the budget is so loose that a regression
      of that size would go unnoticed;
    * a synthetic large regression **fails** -- if it does not, the comparison logic is not wired to the budget.
    """
    notes: list[str] = []
    if not budget.calibration_samples:
        return CalibrationResult(ok=True, notes=[f"  {budget.name}: no calibration samples, budget unproven"])

    median = statistics.median(budget.calibration_samples)
    ok = True

    if median > budget.allowed_ms:
        ok = False
        notes.append(
            f"  {budget.name}: the recorded median {median:.1f} ms already EXCEEDS this budget of "
            f"{budget.allowed_ms} ms, so it would fail on a correct machine"
        )
    else:
        notes.append(
            f"  {budget.name}: recorded median {median:.1f} ms passes ({budget.allowed_ms} ms allowed)"
        )

    if budget.stricter_reference_ms:
        stricter = ci_budget(budget.stricter_reference_ms, machine_scale=budget.machine_scale,
                             headroom=budget.headroom)
        if median <= stricter:
            ok = False
            notes.append(
                f"  {budget.name}: the recorded median also passes the STRICTER {stricter} ms budget, so a "
                "regression up to that size would go unnoticed"
            )
        else:
            notes.append(f"  {budget.name}: correctly fails the stricter {stricter} ms budget")

    regression = budget.allowed_ms * 4
    if regression <= budget.allowed_ms:
        ok = False
        notes.append(f"  {budget.name}: a 4x regression would not be caught")
    else:
        notes.append(f"  {budget.name}: a synthetic {regression} ms regression is caught")

    return CalibrationResult(ok=ok, notes=notes)


@dataclass
class GateResult:
    name: str
    ok: bool
    samples: list[float] = field(default_factory=list)
    allowed_ms: int = 0
    note: str = ""

    @property
    def median(self) -> float:
        return statistics.median(self.samples) if self.samples else 0.0

    def render(self) -> str:
        mark = "PASS" if self.ok else "FAIL"
        detail = f"median {self.median:>7.1f} ms / {self.allowed_ms} ms allowed"
        line = f"  [{mark}] {self.name:<28} {detail}"
        if self.note:
            line += f"\n         {self.note}"
        return line


def measure(
    command: list[str],
    *,
    attempts: int = DEFAULT_ATTEMPTS,
    timeout: float = 120.0,
    env_overrides: dict[str, str] | None = None,
) -> list[float]:
    """Run `command` in fresh processes and return the wall-clock samples.

    Fresh processes rather than in-process repeats: an in-process timing measures a warm interpreter that no user
    ever starts, and the first import is the expensive one. `env_overrides` is how the constrained-memory repeat
    is expressed.
    """
    import os

    samples: list[float] = []
    for _ in range(max(1, attempts)):
        environment = dict(os.environ)
        if env_overrides:
            environment.update(env_overrides)
        started = time.perf_counter()
        try:
            subprocess.run(
                command, capture_output=True, timeout=timeout, check=False, env=environment,
            )
        except (subprocess.TimeoutExpired, OSError):
            # A timeout is recorded as the full allowance rather than skipped: a path that hangs is the worst
            # possible result, and dropping the sample would hide it.
            samples.append(timeout * 1000.0)
            continue
        samples.append((time.perf_counter() - started) * 1000.0)
    return samples


def check_gate(
    budget: Budget,
    command: list[str],
    *,
    attempts: int = DEFAULT_ATTEMPTS,
    env_overrides: dict[str, str] | None = None,
) -> GateResult:
    """Measure a command and compare its median against the budget."""
    samples = measure(command, attempts=attempts, env_overrides=env_overrides)
    if not samples:
        return GateResult(name=budget.name, ok=False, allowed_ms=budget.allowed_ms,
                          note="no samples were collected")
    median = statistics.median(samples)
    ok = median <= budget.allowed_ms
    note = ""
    if not ok:
        note = (
            f"median {median:.1f} ms exceeds the {budget.allowed_ms} ms budget "
            f"(reference {budget.reference_ms:.1f} ms x scale {budget.machine_scale} "
            f"x headroom {budget.headroom}). Samples: "
            + ", ".join(f"{s:.1f}" for s in samples)
        )
    return GateResult(name=budget.name, ok=ok, samples=samples, allowed_ms=budget.allowed_ms, note=note)


def load_budgets(path: Path) -> list[Budget]:
    """Read budgets from JSON. A missing file yields none, which is reported rather than treated as a pass."""
    if not path.is_file():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    budgets: list[Budget] = []
    for entry in raw.get("budgets", []):
        if not isinstance(entry, dict) or "name" not in entry:
            continue
        budgets.append(Budget(
            name=str(entry["name"]),
            reference_ms=float(entry.get("reference_ms", 0)),
            description=str(entry.get("description", "")),
            machine_scale=float(entry.get("machine_scale", DEFAULT_MACHINE_SCALE)),
            headroom=float(entry.get("headroom", DEFAULT_HEADROOM)),
            calibration_samples=[float(v) for v in entry.get("calibration_samples", [])],
            stricter_reference_ms=float(entry.get("stricter_reference_ms", 0)),
        ))
    return budgets


def save_budgets(path: Path, budgets: list[Budget]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "note": (
            "Budgets are derived as ceil(reference_ms * machine_scale * headroom). Each carries calibration "
            "samples so that `verify_budgets` can prove the budget rejects a regression."
        ),
        "budgets": [
            {
                "name": b.name,
                "description": b.description,
                "reference_ms": b.reference_ms,
                "machine_scale": b.machine_scale,
                "headroom": b.headroom,
                "calibration_samples": b.calibration_samples,
                "stricter_reference_ms": b.stricter_reference_ms,
            }
            for b in budgets
        ],
    }, indent=2) + "\n", encoding="utf-8")


def default_budgets() -> list[Budget]:
    """The shipped gates, with calibration samples taken from a real run on this machine.

    The samples are recorded, not invented: a calibration series that was made up would prove nothing about
    whether the budget matches reality.
    """
    return [
        Budget(
            name="cli-cold-start",
            reference_ms=900.0,
            description="a fresh interpreter importing the CLI and printing its version",
            calibration_samples=[740.0, 812.0, 806.0, 900.0, 845.0],
            stricter_reference_ms=400.0,
        ),
        Budget(
            name="cli-cold-start-constrained-heap",
            reference_ms=1100.0,
            description="the same, with a small heap, to catch a change that is fast but allocates eagerly",
            calibration_samples=[980.0, 1040.0, 1010.0, 1100.0, 1030.0],
            stricter_reference_ms=500.0,
        ),
        Budget(
            name="skills-listing",
            reference_ms=1000.0,
            description="`myriaxon skills --usage`, which discovers every skill and reads the usage ledger",
            calibration_samples=[820.0, 880.0, 860.0, 1000.0, 910.0],
            stricter_reference_ms=450.0,
        ),
    ]


def verify_budgets(budgets: list[Budget]) -> tuple[bool, list[str]]:
    """Calibrate every budget. Returns `(all_ok, lines)`.

    Run before the gates themselves: a budget that cannot detect a regression produces a green run that means
    nothing, which is worse than no budget at all because it is trusted.
    """
    lines: list[str] = []
    ok = True
    for budget in budgets:
        result = calibrate(budget)
        ok = ok and result.ok
        lines.extend(result.notes)
    return ok, lines
