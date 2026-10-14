"""Thinking cost on an easy vs a hard prompt, so the tradeoff is visible.

    python scripts/gemini_thinking_cost.py

A single "is thinking on or off faster" answer is not actionable: for a trivial question thinking only
adds latency, for a genuinely hard one it prevents wrong answers. This measures both.
"""
import statistics
import sys
import time
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
warnings.filterwarnings("ignore")

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from myriaxon.dotenv_support import load_dotenv_into_environ  # noqa: E402

load_dotenv_into_environ()

PROMPTS = [
    ("easy  (17*23)", "What is 17 * 23? Reply with just the number."),
    ("simple file op", "Write a one-line shell command that lists the 5 largest files in the current directory."),
    (
        "hard  (refactor plan)",
        "Plan how to split a 2000-line Python module into packages without breaking its public API. "
        "Consider import cycles, backwards compatibility shims, and test migration. Be specific.",
    ),
]


def measure(client, model, prompt, *, thinking, runs=2):
    times, thoughts = [], []
    for _ in range(runs):
        start = time.perf_counter()
        usage = None
        config = None if thinking else {"thinking_config": {"thinking_budget": 0}}
        for chunk in client.models.generate_content_stream(model=model, contents=prompt, config=config):
            if getattr(chunk, "usage_metadata", None):
                usage = chunk.usage_metadata
        times.append(time.perf_counter() - start)
        if usage is not None:
            thoughts.append(
                getattr(usage, "thoughts_token_count", None)
                or getattr(usage, "thought_tokens_count", None)
                or 0
            )
    return statistics.median(times), (statistics.median(thoughts) if thoughts else 0)


def main() -> int:
    import google.genai as genai
    from rich.console import Console

    from myriaxon.config import resolve_config

    config = resolve_config(Console(quiet=True))
    client = genai.Client(vertexai=True, project=config.vertex_project, location=config.vertex_location)
    print(f"model={config.model}\n")

    print("warming up...")
    for _ in client.models.generate_content_stream(model=config.model, contents="hi"):
        pass
    print()

    print(f"  {'prompt':<24} {'thinking ON':>16} {'thinking OFF':>16}   delta")
    for label, prompt in PROMPTS:
        on_time, on_thoughts = measure(client, config.model, prompt, thinking=True)
        off_time, off_thoughts = measure(client, config.model, prompt, thinking=False)
        delta = on_time - off_time
        print(
            f"  {label:<24} {on_time:6.2f}s ({on_thoughts:>4.0f}t) {off_time:6.2f}s ({off_thoughts:>4.0f}t) "
            f"  {delta:+5.2f}s"
        )

    print("\n  't' = thought tokens the API reported.")
    print("  Thinking helps most where the prompt is genuinely hard; it is pure added latency for")
    print("  trivial prompts. That is the basis for --effort/--thinking being separate controls.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
