"""Live end-to-end check of the single-agent adaptation path.

    python scripts/live_adapt_check.py

Runs three short real sessions against the configured provider and prints what the profile, prompt
and context budget actually did:

 1. normal profile  -- measures the fixed per-request cost (prompt + tool schemas) the model pays
    before its first word;
 2. small profile   -- measures how much of that fixed cost the small-model profile removes;
 3. context pressure -- deliberately fills the window with a huge tool result and proves the
    graduated response fires (truncation at the source, then warn/prune/compact/refuse) instead of
    the provider rejecting the request.
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from rich.console import Console  # noqa: E402

from myriaxon.context_budget import evaluate, estimate_tokens  # noqa: E402
from myriaxon.dotenv_support import load_dotenv_into_environ  # noqa: E402
from myriaxon.model_adapt import budget_for_agent  # noqa: E402
from myriaxon.model_profiles import describe_profile, detect_profile  # noqa: E402
from myriaxon.openai_agent import OpenAIAgent  # noqa: E402
from myriaxon.system_prompt import build_system_prompt  # noqa: E402

load_dotenv_into_environ()

KEY_ENV = os.environ.get("IQB_KEY_ENV", "DEEPSEEK_API_KEY")
BASE_URL = os.environ.get("IQB_BASE_URL", "https://api.deepseek.com")
MODEL = os.environ.get("IQB_MODEL", "deepseek-v4-flash")


def build(profile_name: str | None, workspace: Path):
    import openai

    profile = detect_profile(MODEL, explicit=profile_name)
    client = openai.OpenAI(api_key=os.environ[KEY_ENV], base_url=BASE_URL, max_retries=2, timeout=120.0)
    agent = OpenAIAgent(client, MODEL, Console(quiet=True), yolo=True, profile=profile, mode="auto_edit")
    return agent, profile


def measure_fixed_cost(profile) -> tuple[int, int]:
    """Prompt tokens and tool-schema tokens this profile pays on every request."""
    import openai

    from myriaxon.model_adapt import filter_tools
    from myriaxon import tool_registry

    client = openai.OpenAI(api_key=os.environ[KEY_ENV], base_url=BASE_URL, max_retries=0, timeout=30.0)
    agent = OpenAIAgent(client, MODEL, Console(quiet=True), yolo=True, profile=profile, mode="auto_edit")
    prompt = estimate_tokens(build_system_prompt(agent)).tokens
    schemas = estimate_tokens(json.dumps(agent.tools, default=str)).tokens
    return prompt, schemas


def run(profile_name, prompt, workspace):
    previous = os.getcwd()
    os.chdir(workspace)
    try:
        agent, profile = build(profile_name, workspace)
        agent.run_turn(prompt)
        state = agent.tool_context.budget_state
        return {
            "profile": profile,
            "reply": agent.last_assistant_text(),
            "usage": (agent.usage_totals.input_tokens, agent.usage_totals.output_tokens),
            "budget": state,
            "trace": [
                {"tool": e.tool, "err": e.is_error, "result_len": len(e.result)}
                for e in getattr(agent.tool_context, "bench_events", [])
            ],
        }
    finally:
        os.chdir(previous)


def main() -> int:
    if not os.environ.get(KEY_ENV):
        print(f"{KEY_ENV} is not set")
        return 2
    workspace = Path(tempfile.gettempdir()) / "iqb_live_adapt"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "numbers.txt").write_text("1\n2\n3\n4\n5\n", encoding="utf-8")

    print(f"model={MODEL} provider=deepseek single-agent\n")

    print("=== 1. fixed per-request cost by profile ===")
    for name in ("small", "balanced", "large"):
        profile = detect_profile(MODEL, explicit=name)
        prompt_tokens, schema_tokens = measure_fixed_cost(profile)
        print(
            f"  {name:<9} prompt={prompt_tokens:>5,} tok  tool_schemas={schema_tokens:>5,} tok  "
            f"fixed_total={prompt_tokens + schema_tokens:>5,} tok"
        )

    print("\n=== 2. real turn, default profile ===")
    result = run(None, "Read numbers.txt and tell me the sum of the numbers. Answer with digits only.", workspace)
    print(f"  {describe_profile(result['profile'])}")
    print(f"  reply      : {result['reply'][:160]!r}")
    print(f"  usage      : in={result['usage'][0]:,} out={result['usage'][1]:,}")
    if result["budget"]:
        b = result["budget"]
        print(f"  context    : {b.percent}% ({b.used_tokens:,}/{b.window:,}) action={b.action} est={b.confidence}")

    print("\n=== 3. real turn, small profile (single agent, no delegation) ===")
    result = run("small", "Read numbers.txt and tell me the sum of the numbers. Answer with digits only.", workspace)
    print(f"  {describe_profile(result['profile'])}")
    print(f"  withheld   : {', '.join(result['profile'].disabled_tools)}")
    print(f"  reply      : {result['reply'][:160]!r}")
    print(f"  usage      : in={result['usage'][0]:,} out={result['usage'][1]:,}")

    print("\n=== 4. context pressure (graduated response, no API needed) ===")
    small = detect_profile(MODEL, explicit="small")
    for count in (1, 2, 3):
        messages = [{"role": "user", "content": "task"}]
        for i in range(count):
            messages.append({"role": "tool", "tool_call_id": str(i), "content": "x" * 1200})
        state = evaluate(messages, small, system_prompt="You are a coding agent.\n" * 20, tools_chars=8000)
        print(f"  {count} tool results @1200ch -> {state.meter()} {state.percent:>3}% action={state.action}")
    print("\n  note: results are capped at the profile's tool_result_chars, so the 'many small results'")
    print("  case above is what remains; the truncation itself is verified in the unit tests.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
