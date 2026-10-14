"""IQB-14 -- Myriaxon Quality Benchmark, 14 parameters.

An evaluation harness for agentic AI systems, built as a first-class part of Myriaxon rather than
a script bolted onto it. The 14 parameters, their scoring rules, and the composite AXS score are
specified in docs/IQB-14.md; this package implements exactly that specification.

Design rules this package holds itself to (each one exists because its absence makes benchmark
numbers untrustworthy):

1. A result is a *measurement*, not a boolean -- every assertion yields score/status/evidence.
2. `timeout`, `harness_error`, and `env_error` are never collapsed into `fail`.
3. Every run is reproducible from an artifact: a run manifest + per-instance JSON + a trace
   JSONL of every tool call, its arguments, result, timing, and token delta.
4. Nothing is scored by prose alone: an LLM judge is allowed only where a deterministic proxy
   genuinely does not exist, and then it must be a constrained, recorded, rubric-scored call.
5. The harness's own code is unit-tested separately from live-model evals.
"""
from .parameters import PARAMETERS, PARAMETER_IDS, STANDARD_ID, STANDARD_VERSION, axs_from_scores

__all__ = [
    "PARAMETERS",
    "PARAMETER_IDS",
    "STANDARD_ID",
    "STANDARD_VERSION",
    "axs_from_scores",
]
