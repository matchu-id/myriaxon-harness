"""Which tools are actually used? Decide the default tool set from evidence, not intuition.

Trimming a tool from the always-sent set is only safe if it is genuinely rare. This counts tool calls
across every recorded session on this machine, so the decision has data behind it.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

CONFIG = Path(r"C:\Users\arian\OneDrive\ドキュメント\Myriaxon\.myriaxon")


def iter_trajectories():
    """Every `trajectory` array in every saved session file, plus the shadow history."""
    for base in (CONFIG / "sessions", CONFIG / "history"):
        if not base.is_dir():
            continue
        for path in base.rglob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
            if isinstance(data, dict):
                for entry in data.get("trajectory") or []:
                    if isinstance(entry, dict) and entry.get("tool"):
                        yield entry["tool"]
            elif isinstance(data, list):
                for entry in data:
                    if isinstance(entry, dict) and entry.get("tool"):
                        yield entry["tool"]


def main() -> int:
    from myriaxon import tool_registry

    counts = Counter(iter_trajectories())
    total = sum(counts.values())

    defs = {d.get("name"): len(json.dumps(d)) for d in tool_registry.build_tool_defs()}
    registered = sum(defs.values())

    print(f"{total} recorded tool call(s) across saved sessions on this machine\n")
    if not total:
        print("no history to learn from")
        return 0

    print(f"  {'tool':26} {'calls':>6} {'share':>7} {'schema':>8}  chars-per-call")
    print("  " + "-" * 66)
    rows = []
    for name, size in sorted(defs.items(), key=lambda kv: -kv[1]):
        calls = counts.get(name, 0)
        rows.append((name, calls, size))

    used, unused = [], []
    for name, calls, size in sorted(rows, key=lambda r: -r[1]):
        share = calls / total * 100
        if calls:
            cost = size / calls
            print(f"  {name:26} {calls:>6} {share:>6.1f}% {size:>8,}  {cost:>8,.0f}")
            used.append((name, calls, size))
        else:
            unused.append((name, size))

    print(f"\n  used: {len(used)} tools, {sum(c for _, c, _ in used)} calls")
    print(f"  NEVER called in recorded history: {len(unused)} tools")

    print("\n  --- never called, by schema cost ---")
    never_chars = 0
    for name, size in sorted(unused, key=lambda r: -r[1]):
        print(f"    {name:26} {size:>8,} chars")
        never_chars += size
    print(f"\n  their combined schema: {never_chars:,} chars of {registered:,} "
          f"({never_chars / registered * 100:.0f}% of all tool bytes)")

    # Cross-check the busy tools: how much does each COST per call?
    print("\n  --- most expensive per call (schema bytes / calls) ---")
    per_call = sorted(((size / calls, name, calls, size) for name, calls, size in used), reverse=True)
    for cost, name, calls, size in per_call[:6]:
        print(f"    {name:26} {cost:>9,.0f} chars per call  ({calls} call(s), {size:,} schema)")

    print("\n  --- best value (most calls per schema byte) ---")
    value = sorted(((calls / size, name, calls, size) for name, calls, size in used), reverse=True)
    for ratio, name, calls, size in value[:6]:
        print(f"    {name:26} {ratio:>9.3f} calls/KB  ({calls} call(s), {size:,} schema)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
