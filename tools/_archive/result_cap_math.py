"""Which tools produce the biggest results, and what does the cap do to them?

A tool result is re-sent on every later request in the same turn, so the cost of one large read
compounds. This measures the real files in this repository against the profile caps, so raising or
lowering a cap is a decision with numbers behind it.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def main() -> int:
    from myriaxon import model_profiles

    print("caps by profile")
    rows = []
    for name in ("small", "balanced", "large"):
        profile = model_profiles.detect_profile("x", explicit=name, agent_mode="single")
        rows.append((name, profile.tool_result_chars, profile.context_window))
        print(f"  {name:9} result cap {profile.tool_result_chars:>7,} chars "
              f"(~{profile.tool_result_chars // 4:>6,} tok)   window {profile.context_window:>9,}")

    print("\nreal files in this repo, as `read_file` would return them")
    files = sorted(
        (p for p in REPO.rglob("*.py") if ".venv" not in p.parts and "node_modules" not in p.parts),
        key=lambda p: p.stat().st_size, reverse=True,
    )[:8]
    large = model_profiles.detect_profile("x", explicit="large", agent_mode="single")
    cap = large.tool_result_chars
    print(f"  {'file':44} {'chars':>9} {'tok':>8}  {'capped to':>10}")
    total_raw = total_capped = 0
    for path in files:
        size = path.stat().st_size
        capped = min(size, cap)
        total_raw += size
        total_capped += capped
        print(f"  {str(path.relative_to(REPO)):44} {size:>9,} {size // 4:>8,}  {capped:>10,}")

    print(f"\n  reading all {len(files)}: {total_raw:,} chars raw, {total_capped:,} after the cap")

    print("\nwhat the cap costs over a turn, per profile")
    print("  a single capped result is re-sent on every subsequent request in that turn:")
    for name, result_cap, _ in rows:
        for further in (1, 5, 10, 20):
            cost = result_cap * further // 4
            print(f"    {name:9} cap {result_cap:>6,} x {further:>2} further requests = "
                  f"{cost:>9,} tok")
        print()

    print("the tradeoff, stated plainly")
    print("  Lower cap  = fewer tokens re-sent, but the model re-reads with offset/limit to get the")
    print("               rest, which costs a round trip and its own tokens.")
    print("  Higher cap = one request is enough, but the whole thing rides along in the history.")
    print()
    print("  `balanced` already runs at 8,000 and `small` at 1,200, so 8,000 is this harness's own")
    print("  established moderate value -- the `large` tier's 24,000 is the outlier, three times the")
    print("  next tier up from the one below it, with no measurement recorded for why.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
