"""Smoke-test the IQB-14 judge plumbing (no benchmark run needed).

    python scripts/check_judge.py deepseek deepseek-v4-flash agent
    python scripts/check_judge.py openrouter openai/gpt-oss-120b api
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from myriaxon.bench.judge import JudgeSpec, build_judge, default_endpoint, default_key_env  # noqa: E402
from myriaxon.dotenv_support import load_dotenv_into_environ  # noqa: E402

load_dotenv_into_environ()


def main() -> int:
    provider = sys.argv[1] if len(sys.argv) > 1 else "deepseek"
    model = sys.argv[2] if len(sys.argv) > 2 else "deepseek-v4-flash"
    mode = sys.argv[3] if len(sys.argv) > 3 else "agent"
    key_env = default_key_env(provider)
    spec = JudgeSpec(
        provider=provider,
        model=model,
        base_url=default_endpoint(provider),
        api_key_env=key_env,
        mode=mode,
    )
    print("judge spec:", spec.to_dict())
    try:
        judge = build_judge(spec)
    except ValueError as exc:
        print("BUILD FAILED (expected when no key is present):", exc)
        return 2

    prompt = (
        "Score the answer against each point. Reply with JSON only: "
        '{"covered": ["a"]}.\n\nPoints:\n[{"id": "a", "text": "the answer is 42"}]\n\nAnswer:\nThe answer is 42.'
    )
    try:
        reply = judge(prompt, None)
    except Exception as exc:  # noqa: BLE001 -- this script exists to show the failure verbatim
        print(f"JUDGE CALL FAILED: {type(exc).__name__}: {str(exc)[:400]}")
        return 1
    print("judge replied:", reply[:300])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
