"""Inspect how Myriaxon's context budget behaves for a given model and conversation shape.

    python scripts/check_budget.py deepseek-v4-flash
    python scripts/check_budget.py gemma-2-2b --results 4 --chars 20000
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from myriaxon.context_budget import evaluate, shrink_tool_result  # noqa: E402
from myriaxon.model_profiles import describe_profile, detect_profile  # noqa: E402


def fake_messages(count: int, chars: int) -> list[dict]:
    body = "x" * chars
    return [{"role": "system", "content": "You are a coding agent."}] + [
        {"role": "tool", "tool_call_id": str(i), "content": body} for i in range(count)
    ]


PROMPT = "You are a coding agent.\n" * 20
TOOLS_CHARS = 8_000


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("model")
    parser.add_argument("--results", type=int, default=4, help="how many big tool results to simulate")
    parser.add_argument("--chars", type=int, default=20_000, help="characters per tool result")
    parser.add_argument("--profile", default=None)
    parser.add_argument("--window", type=int, default=None, help="force --max-input-tokens")
    args = parser.parse_args()

    profile = detect_profile(args.model, explicit=args.profile, max_input_tokens=args.window)
    print("profile:", describe_profile(profile))
    print()
    print(f"{'scenario':<26} {'meter':<14} {'used':>9} {'headroom':>10}  action")
    for label, msgs in (
        ("empty conversation", []),
        (f"1 result x {args.chars:,}ch", fake_messages(1, args.chars)),
        (f"{args.results} results x {args.chars:,}ch", fake_messages(args.results, args.chars)),
    ):
        state = evaluate(msgs, profile, system_prompt=PROMPT, tools_chars=TOOLS_CHARS)
        print(
            f"{label:<26} {state.meter():<14} {state.used_tokens:>9,} {state.headroom_tokens:>10,}  "
            f"{state.action} ({state.percent}%, {state.confidence})"
        )
        if state.reason:
            print(f"{'':<26} {state.reason}")
    print()
    biggest = evaluate(fake_messages(args.results, args.chars), profile, system_prompt=PROMPT, tools_chars=TOOLS_CHARS)
    print("breakdown (largest scenario):", biggest.breakdown)
    print()
    text = "A" * 3000 + "MIDDLE" + "B" * 3000
    shrunk, was = shrink_tool_result(text, 1000, kind="directory listing")
    print(f"truncation: {len(text)} -> {len(shrunk)} chars, truncated={was}, marker={'omitted' in shrunk}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
