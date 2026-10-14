"""Every provider builder must ship the unlock tool, or the deferred groups are unreachable.

This is the check that would have caught the bug the audit found: `request_tools` was added to two of
the four builders, so on OpenAI-compatible, DeepSeek, OpenRouter, Netra and Gemini the 12 deferred
tools were withheld with no way to load them. A token saving that removes a capability is not a
saving, so this asserts reachability per builder rather than once.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from myriaxon import tool_registry  # noqa: E402

failures = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global failures
    if not ok:
        failures += 1
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}{('  -- ' + detail) if detail and not ok else ''}")


def names(tools) -> set[str]:
    out = set()
    for tool in tools or []:
        if tool.get("name"):
            out.add(tool["name"])
        elif isinstance(tool.get("function"), dict):
            out.add(tool["function"].get("name", ""))
    return out


def main() -> int:
    deferred = {n for spec in tool_registry.DEFERRED_GROUPS.values() for n in spec["tools"]}

    builders = (
        ("anthropic (registry shape)", tool_registry.build_tool_defs),
        ("openai-compatible", tool_registry.build_tool_defs_openai),
        ("gemini", tool_registry.build_tool_defs_gemini),
        ("builtin index", tool_registry._builtin_tool_defs),
    )

    print("each builder, with and without an unlock\n")
    for label, build in builders:
        full = build()
        offered = tool_registry.apply_tool_visibility(full, set())
        full_names, offered_names = names(full), names(offered)

        print(f"  {label}")
        print(f"    built {len(full_names):>3} tools, offered by default {len(offered_names):>3}, "
              f"withheld {len(full_names & deferred):>2}")

        # 1. the unlock tool must be offered, or nothing deferred is reachable
        check(f"{label}: request_tools is offered",
              "request_tools" in offered_names,
              "the deferred tools would be unreachable on this provider")

        # 2. the deferred groups really are withheld (the saving is real)
        leaked = offered_names & deferred
        check(f"{label}: no deferred tool leaks into the default set", not leaked,
              f"leaked: {sorted(leaked)}")

        # 3. and every one of them comes back when its group is unlocked
        unlocked = tool_registry.apply_tool_visibility(full, set(tool_registry.DEFERRED_GROUPS))
        missing = (full_names & deferred) - names(unlocked)
        check(f"{label}: every deferred tool returns when unlocked", not missing,
              f"unreachable: {sorted(missing)}")

        # 4. the saving, measured per builder
        before, after = len(json.dumps(full)), len(json.dumps(offered))
        saving = before - after
        print(f"    {before:,} chars -> {after:,} chars  (saved {saving:,}, "
              f"{saving / before * 100:.0f}%, ~{saving // 4:,} tok/request)")
        check(f"{label}: the saving is more than half", saving / before > 0.5)

    print()
    print("deferral reachable on every provider" if not failures else f"{failures} check(s) FAILED")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
