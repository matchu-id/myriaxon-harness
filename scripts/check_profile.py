"""Show what a profile costs and withholds, without running a model.

    python scripts/check_profile.py deepseek-v4-flash
    python scripts/check_profile.py gemma-2-2b --compare
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from myriaxon import tool_registry  # noqa: E402
from myriaxon.context_budget import estimate_tokens  # noqa: E402
from myriaxon.model_adapt import filter_tools  # noqa: E402
from myriaxon.model_profiles import PROFILES, describe_profile, detect_profile  # noqa: E402


def _tool_name(tool: dict) -> str:
    """Anthropic/registry shape is {name}, OpenAI shape is {type, function:{name}}."""
    return tool.get("name") or (tool.get("function") or {}).get("name") or "?"


def report(model: str, explicit: str | None) -> None:
    profile = detect_profile(model, explicit=explicit)
    full = tool_registry.build_tool_defs_openai()
    kept = filter_tools(full, profile)
    withdrawn = sorted({_tool_name(t) for t in full} - {_tool_name(t) for t in kept})
    schema_full = estimate_tokens(json.dumps(full, default=str)).tokens
    schema_kept = estimate_tokens(json.dumps(kept, default=str)).tokens
    print(describe_profile(profile))
    print(f"  tools        : {len(kept)}/{len(full)} kept, schemas {schema_kept:,}/{schema_full:,} tokens")
    if withdrawn:
        print(f"  withheld     : {', '.join(withdrawn)}")
    prompt = "You are a coding agent.\n" * 20
    print(f"  prompt budget: <= {profile.system_prompt_chars:,} chars")
    print(f"  fixed cost   : ~{schema_kept + profile.system_prompt_chars // 4:,} tokens before the first word")
    print()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("model", nargs="?")
    parser.add_argument("--explicit", default=None)
    parser.add_argument("--compare", action="store_true", help="show all three named profiles")
    args = parser.parse_args()

    if args.compare or not args.model:
        for name in PROFILES:
            report(args.model or "unknown", name)
        return 0
    report(args.model, args.explicit)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
