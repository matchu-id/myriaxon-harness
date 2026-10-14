"""How long does Gemini spend thinking, and can it be turned off?

    python scripts/gemini_thinking.py

gemini-3.x reasons before answering. That reasoning is billed as output tokens and adds latency, and
whether it happens at all is governed by the thinking config / --thinking flag. This measures the
split so the tradeoff is visible rather than guessed:
  * time to first CONTENT token (thinking happens before this)
  * total time
  * thoughts_token_count, which the API reports separately when available
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

PROMPT = "What is 17 * 23? Reply with just the number."


def run(client, model, *, thinking: bool, runs: int = 3) -> dict:
    """One configuration, `runs` samples. Returns medians and token accounting."""
    ttfts, totals, thoughts, out_tokens = [], [], [], []
    for _ in range(runs):
        start = time.perf_counter()
        first_content = None
        text = ""
        usage = None
        try:
            config = {"thinking_config": {"thinking_budget": 0}} if not thinking else None
            for chunk in client.models.generate_content_stream(
                model=model, contents=PROMPT, config=config
            ):
                if chunk.text and first_content is None:
                    first_content = time.perf_counter() - start
                text += chunk.text or ""
                if getattr(chunk, "usage_metadata", None):
                    usage = chunk.usage_metadata
        except Exception as exc:  # noqa: BLE001
            return {"error": f"{type(exc).__name__}: {str(exc)[:90]}"}
        totals.append(time.perf_counter() - start)
        ttfts.append(first_content if first_content is not None else totals[-1])
        if usage is not None:
            thoughts.append(getattr(usage, "thoughts_token_count", None) or 0)
            out_tokens.append(getattr(usage, "candidates_token_count", None) or 0)
    return {
        "ttft": statistics.median(ttfts),
        "total": statistics.median(totals),
        "thoughts": statistics.median(thoughts) if thoughts else None,
        "out": statistics.median(out_tokens) if out_tokens else None,
    }


def main() -> int:
    import google.genai as genai

    from myriaxon.config import resolve_config
    from rich.console import Console

    config = resolve_config(Console(quiet=True))
    model = config.model
    client = genai.Client(vertexai=True, project=config.vertex_project, location=config.vertex_location)
    print(f"model={model}  prompt={PROMPT!r}\n")

    # Warm the instance first so the cold start is not attributed to thinking.
    print("warming up (first request pays the provider cold start)...")
    warm_start = time.perf_counter()
    try:
        for _ in client.models.generate_content_stream(model=model, contents="hi"):
            pass
        print(f"  cold start paid: {time.perf_counter() - warm_start:.1f}s\n")
    except Exception as exc:  # noqa: BLE001
        print(f"  warm-up failed: {type(exc).__name__}: {str(exc)[:80]}\n")

    for label, thinking in (("thinking ON (default)", True), ("thinking OFF (budget=0)", False)):
        result = run(client, model, thinking=thinking)
        if "error" in result:
            print(f"  {label:<26} FAILED {result['error']}")
            continue
        thoughts = f"{result['thoughts']:.0f}" if result["thoughts"] is not None else "n/a"
        out = f"{result['out']:.0f}" if result["out"] is not None else "n/a"
        print(f"  {label:<26} ttft={result['ttft']:5.2f}s  total={result['total']:5.2f}s  "
              f"thought_tokens={thoughts:<6} answer_tokens={out}")

    print("\n=== reading this ===")
    print("  ttft here is the time until the first ANSWER token, so it includes thinking time.")
    print("  thought_tokens > 0 means the model really did reason before answering.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
