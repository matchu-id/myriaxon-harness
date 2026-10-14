"""Measure what every request actually costs before anything is optimised.

"The most token-efficient harness" is a claim, and the only way to make it is to count. This measures
the fixed per-request overhead -- system prompt, tool schemas, injected scaffolding -- and the
per-turn cost, so an optimisation has a before and an after rather than an opinion.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from myriaxon import model_profiles, tool_registry  # noqa: E402


def approx_tokens(text: str) -> int:
    """Rough token count. Four characters per token is the usual English average; it is an estimate
    and is labelled as one everywhere it appears, because a made-up precise number is worse than an
    honest approximate one."""
    return max(1, len(text) // 4)


def main() -> int:
    print("=== fixed per-request overhead ===\n")

    defs = tool_registry.build_tool_defs()
    tools_json = json.dumps(defs)
    print(f"  tool definitions      {len(defs):>3} tools   {len(tools_json):>8,} chars  "
          f"~{approx_tokens(tools_json):>7,} tok")

    # Per-tool breakdown, so it is clear which few dominate.
    rows = []
    for definition in defs:
        blob = json.dumps(definition)
        rows.append((len(blob), definition.get("name", "?")))
    rows.sort(reverse=True)
    total = sum(r[0] for r in rows)
    print(f"\n  the ten largest tool schemas ({total:,} chars total):")
    for size, name in rows[:10]:
        share = size / total * 100
        print(f"    {name:28} {size:>7,} chars  {share:>5.1f}%")

    print("\n=== system prompt by profile ===\n")
    from myriaxon.system_prompt import build_system_prompt

    class Agent:
        def __init__(self, profile):
            self.profile = profile
            self.tools = defs
            self.project_instructions = ""
            self.skills = []
            self.persona_identity = ""

    for name, model in (("small", "claude-3-5-haiku"), ("balanced", "claude-sonnet-4"),
                        ("large", "claude-opus-4")):
        profile = model_profiles.detect_profile(model, agent_mode="single")
        prompt = build_system_prompt(Agent(profile))
        print(f"  {name:9} {profile.tier if hasattr(profile, 'tier') else name:9} "
              f"{len(prompt):>7,} chars  ~{approx_tokens(prompt):>6,} tok "
              f"(cap {profile.system_prompt_chars:,})")

    print("\n=== what one turn costs before any conversation ===\n")
    profile = model_profiles.detect_profile("deepseek/deepseek-v4.1-flash", agent_mode="single")
    prompt = build_system_prompt(Agent(profile))
    offered = model_adapt_names(defs, profile)
    offered_json = json.dumps([d for d in defs if d.get("name") in offered])
    fixed = len(prompt) + len(offered_json)
    print(f"  profile               {profile.name if hasattr(profile, 'name') else 'detected'}")
    print(f"  tools offered         {len(offered)} of {len(defs)}")
    print(f"  system prompt         {len(prompt):>8,} chars  ~{approx_tokens(prompt):>7,} tok")
    print(f"  tool schemas          {len(offered_json):>8,} chars  ~{approx_tokens(offered_json):>7,} tok")
    print(f"  ---------------------------------------------")
    print(f"  fixed overhead        {fixed:>8,} chars  ~{approx_tokens(str(fixed)):>7,} tok"
          if False else
          f"  fixed overhead        {fixed:>8,} chars  ~{approx_tokens('x' * fixed):>7,} tok")

    print("\n=== tool result truncation ===\n")
    from myriaxon import constants
    for attr in dir(constants):
        if "CAP" in attr or "LIMIT" in attr or "TRUNC" in attr:
            print(f"  {attr:38} {getattr(constants, attr):,}")

    return 0


def model_adapt_names(defs, profile):
    from myriaxon import model_adapt

    return {d.get("name") for d in model_adapt.filter_tools(defs, profile)}


if __name__ == "__main__":
    sys.exit(main())
