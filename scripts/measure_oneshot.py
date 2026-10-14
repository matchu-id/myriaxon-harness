"""Attribute one-shot CLI wall time to its phases: interpreter, imports, model, tools.

    python scripts/measure_oneshot.py "write hi into x.txt"

Run from a directory you do not mind writing to. Prints a phase table and a verdict naming the
dominant cost. This exists so "make it fast" is driven by numbers rather than by rewriting things
that turn out not to matter.
"""
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def main() -> int:
    prompt = sys.argv[1] if len(sys.argv) > 1 else "Reply with exactly: pong"

    marks: list[tuple[str, float]] = []
    start = time.perf_counter()

    def mark(label: str) -> None:
        marks.append((label, time.perf_counter() - start))

    import argparse  # noqa: F401  (first import, trivial)

    mark("stdlib ready")

    from rich.console import Console

    mark("rich")

    from myriaxon.config import resolve_config
    from myriaxon.settings import load_settings

    mark("myriaxon config+settings")

    from myriaxon.cli import _build_agent

    mark("myriaxon.cli (heaviest)")

    import google.genai as genai

    mark("google.genai")

    console = Console(quiet=True)
    settings = load_settings()
    config = resolve_config(console)
    mark("resolve_config")

    # Phase boundary only -- the agent build itself is what the CLI does next, and its cost is
    # dominated by whichever provider SDK the config selects.
    if config.provider == "gemini":
        import google.genai  # noqa: F401

        mark("provider SDK (google.genai)")
    elif config.provider in ("deepseek", "openrouter", "openai_compat"):
        import openai  # noqa: F401

        mark("provider SDK (openai)")
    else:
        import anthropic  # noqa: F401

        mark("provider SDK (anthropic)")

    sys.stdout.write("\n--- phase table (seconds since process start) ---\n")
    previous = 0.0
    for label, value in marks:
        sys.stdout.write(f"  {label:<28} {value:7.3f}   (+{value - previous:.3f})\n")
        previous = value
    sys.stdout.write(f"  {'TOTAL startup (no model)':<28} {previous:7.3f}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
