"""Measure the harness, so "faster" is a number that can regress instead of a claim.

Speed work without a measurement is guesswork, and this session already produced one example of why: the MCP
import was reported as costing 1.24 s of CLI startup based on a misread `-X importtime` column, when the real
figure was ~700 ms for that module and ~15 ms of actual startup. The number was wrong in a way that made the
work look more valuable than it was. This module exists so the next claim is checked before it is made.

## What it measures

* **Cold start** — a fresh interpreter importing the CLI and printing a version. Real, because it spawns real
  processes; a warm in-process import would measure something no user experiences.
* **Import cost by module** — parsed from `-X importtime`, reported as *self* time rather than the cumulative
  column. The cumulative column double-counts nested children, which is precisely the trap above.
* **Tool dispatch overhead** — the harness's own cost per tool call, with the tool's work excluded where it can
  be. This is the number that matters inside a session and the one nobody measures.

## What it deliberately does not measure

Model latency and token throughput dominate any real session, and neither is a property of this harness. The
report says so rather than letting a fast startup number imply a fast session.
"""

from __future__ import annotations

import json
import re
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

_IMPORTTIME_LINE = re.compile(r"import time:\s+(\d+)\s*\|\s*(\d+)\s*\|\s*(\S+)")


@dataclass
class Timing:
    label: str
    samples: list[float] = field(default_factory=list)

    @property
    def best(self) -> float:
        return min(self.samples) if self.samples else 0.0

    @property
    def median(self) -> float:
        return statistics.median(self.samples) if self.samples else 0.0

    @property
    def worst(self) -> float:
        return max(self.samples) if self.samples else 0.0

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "runs": len(self.samples),
            "best_ms": round(self.best, 1),
            "median_ms": round(self.median, 1),
            "worst_ms": round(self.worst, 1),
        }


def measure_cold_start(runs: int = 5, python: str | None = None) -> Timing:
    """Time `python -m myriaxon --version` in fresh processes.

    `--version` is the right target: it exercises the full CLI import path, which is what every invocation pays,
    and it does no network or filesystem work that would add noise unrelated to startup.
    """
    executable = python or sys.executable
    timing = Timing(label="cli cold start (--version)")
    for _ in range(max(1, runs)):
        started = time.perf_counter()
        subprocess.run(
            [executable, "-m", "myriaxon", "--version"],
            capture_output=True, text=True, timeout=120,
        )
        timing.samples.append((time.perf_counter() - started) * 1000)
    return timing


def measure_import_cost(module: str = "myriaxon.cli", python: str | None = None, top: int = 15) -> list[dict]:
    """Per-module import cost, by SELF time.

    `-X importtime` prints `self | cumulative | module`. The cumulative figure includes every module imported
    beneath it, so ranking by it credits a parent with its children's cost and can be off by an order of
    magnitude — the exact misreading that produced this session's wrong first claim. Self time is additive and
    is what shows where the time actually goes.
    """
    executable = python or sys.executable
    result = subprocess.run(
        [executable, "-X", "importtime", "-c", f"import {module}"],
        capture_output=True, text=True, timeout=300,
    )
    rows: list[dict] = []
    for line in result.stderr.splitlines():
        match = _IMPORTTIME_LINE.search(line)
        if not match:
            continue
        rows.append({
            "self_us": int(match.group(1)),
            "cumulative_us": int(match.group(2)),
            "module": match.group(3),
        })
    rows.sort(key=lambda row: row["self_us"], reverse=True)
    return rows[:top]


def measure_import_total(module: str = "myriaxon.cli", python: str | None = None) -> dict:
    """Wall-clock cost of importing a module once, in a fresh process, plus the module count."""
    executable = python or sys.executable
    script = (
        "import time, sys;"
        "t=time.perf_counter();"
        f"import {module};"
        "print(int((time.perf_counter()-t)*1000), len(sys.modules))"
    )
    result = subprocess.run([executable, "-c", script], capture_output=True, text=True, timeout=300)
    parts = (result.stdout or "").strip().split()
    if len(parts) != 2:
        return {"ms": 0, "modules": 0, "error": (result.stderr or "")[-200:]}
    return {"ms": int(parts[0]), "modules": int(parts[1])}


def measure_tool_dispatch(tool_name: str = "todo_write", runs: int = 200) -> dict:
    """In-process cost of dispatching one tool call, with the tool's own work kept trivial.

    Measures the harness's overhead — schema validation, loop detection, policy checks, dispatch — rather than
    the tool. Imported in-process because that is how a real session calls it, and a subprocess per call would
    measure process startup instead.

    Returns `{"ms": ..., "error": ...}`; a tool that cannot be dispatched harmlessly reports the reason rather
    than being silently skipped.
    """
    try:
        from . import tool_registry
        from .tool_context import ToolContext
    except Exception as exc:  # noqa: BLE001
        return {"ms": 0.0, "error": f"could not import dispatch machinery: {exc}"}

    try:
        from rich.console import Console

        ctx = ToolContext(
            client=None, model="", max_tokens=0,
            console=Console(file=open(__import__("os").devnull, "w")),
            yolo=True, tools=[], mode="auto",
        )
    except Exception as exc:  # noqa: BLE001
        return {"ms": 0.0, "error": f"could not build a ToolContext: {exc}"}

    payload = {"todos": [{"content": "benchmark probe", "status": "pending"}]}
    samples: list[float] = []
    for _ in range(max(1, runs)):
        started = time.perf_counter()
        try:
            tool_registry.dispatch_tool(tool_name, dict(payload), ctx)
        except Exception as exc:  # noqa: BLE001
            return {"ms": 0.0, "error": f"{tool_name} raised on the probe payload: {exc}"}
        samples.append((time.perf_counter() - started) * 1000)
    if not samples:
        return {"ms": 0.0, "error": "no samples collected"}
    return {
        "tool": tool_name,
        "runs": len(samples),
        "median_ms": round(statistics.median(samples), 3),
        "best_ms": round(min(samples), 3),
        "worst_ms": round(max(samples), 3),
    }


def run_bench(runs: int = 5, python: str | None = None) -> dict:
    """The full measurement, as a JSON-serialisable dict.

    Import cost is measured in a *subprocess* even though this function is already in-process, because by the
    time it runs everything is imported and the measurement would be zero.
    """
    cold = measure_cold_start(runs=runs, python=python)
    imports = measure_import_total(python=python)
    return {
        "cold_start": cold.to_dict(),
        "import": imports,
        "top_import_self_time": measure_import_cost(python=python),
        # Only meaningful when called before the modules are loaded, so it is reported as a separate probe with
        # its own caveat rather than mixed into the startup numbers.
        "dispatch": measure_tool_dispatch(),
        "caveat": (
            "Startup and import figures are harness costs. In a real session, model latency and token "
            "throughput dominate both, and neither is measured here."
        ),
    }


def render(report: dict) -> str:
    """Human-readable rendering of `run_bench` output."""
    lines: list[str] = []
    cold = report.get("cold_start", {})
    lines.append("Cold start (`myriaxon --version`, fresh process each run)")
    lines.append(
        f"  best {cold.get('best_ms', 0):.0f} ms | median {cold.get('median_ms', 0):.0f} ms | "
        f"worst {cold.get('worst_ms', 0):.0f} ms   ({cold.get('runs', 0)} runs)"
    )
    imports = report.get("import", {})
    if imports.get("ms"):
        lines.append("")
        lines.append("Import")
        lines.append(f"  myriaxon.cli {imports['ms']} ms, {imports['modules']} modules loaded")
    top = report.get("top_import_self_time") or []
    if top:
        lines.append("")
        lines.append("Slowest imports (SELF time; the cumulative column double-counts children)")
        for row in top[:10]:
            lines.append(f"  {row['self_us'] / 1000:8.2f} ms  {row['module']}")
    dispatch = report.get("dispatch", {})
    lines.append("")
    lines.append("Tool dispatch overhead")
    if dispatch.get("error"):
        lines.append(f"  not measured: {dispatch['error']}")
    else:
        lines.append(
            f"  {dispatch.get('tool')} median {dispatch.get('median_ms')} ms over "
            f"{dispatch.get('runs')} calls (validation, loop detection, policy, dispatch)"
        )
    caveat = report.get("caveat")
    if caveat:
        lines.append("")
        lines.append(f"  note: {caveat}")
    return "\n".join(lines)


def compare(baseline: dict, current: dict, tolerance: float = 0.15) -> list[str]:
    """Differences between two runs, as human-readable lines.

    `tolerance` is a fraction: a change smaller than this is reported as noise rather than as a regression,
    because process-level timing on a shared machine moves by more than that between runs. Without a floor,
    every benchmark reports a regression and the number stops being trusted.
    """
    lines: list[str] = []
    base_cold = (baseline.get("cold_start") or {}).get("median_ms", 0)
    now_cold = (current.get("cold_start") or {}).get("median_ms", 0)
    if base_cold and now_cold:
        delta = (now_cold - base_cold) / base_cold
        verdict = "within noise" if abs(delta) < tolerance else ("SLOWER" if delta > 0 else "faster")
        lines.append(
            f"  cold start median: {base_cold:.0f} ms -> {now_cold:.0f} ms "
            f"({delta * 100:+.1f}%, {verdict})"
        )
    base_import = (baseline.get("import") or {}).get("ms", 0)
    now_import = (current.get("import") or {}).get("ms", 0)
    if base_import and now_import:
        delta = (now_import - base_import) / base_import
        verdict = "within noise" if abs(delta) < tolerance else ("SLOWER" if delta > 0 else "faster")
        lines.append(
            f"  cli import:        {base_import} ms -> {now_import} ms "
            f"({delta * 100:+.1f}%, {verdict})"
        )
    if not lines:
        return ["  no comparable figures in the baseline"]
    return lines
