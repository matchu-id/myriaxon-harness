"""What would deferring specialised tools actually save?

Measured against the real schemas, so the decision is arithmetic rather than intuition. Reported per
provider-shaped list, because the three builders serialise differently.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


# The set a coding turn genuinely uses: read, write, edit, run, find, look things up, keep a list.
# Everything else is a *capability* that is real but not needed on most turns -- managing other
# agents, drawing posters, rolling files back, delegating.
CORE = {
    "read_file", "write_file", "edit_file",
    "run_bash", "bash_output", "kill_bash",
    "glob_search", "grep_search",
    "git_status", "git_diff",
    "web_search", "web_fetch",
    "todo_write", "ask_user_question",
    "use_skill", "save_skill",
}

DEFERRED_GROUPS = {
    "dots": {"dot_list", "dot_create", "dot_goal_update", "dot_run"},
    "poster": {"design_poster"},
    "checkpoints": {"checkpoint_list", "checkpoint_restore"},
    "schedule": {"schedule_prompt", "list_scheduled", "cancel_scheduled"},
    "notebook": {"edit_notebook"},
    "memory": {"search_memory"},
    "delegation": {"task", "send_to_subagent", "parallel_tasks"},
}


def measure(defs: list[dict]) -> None:
    by_name = {}
    for definition in defs:
        name = definition.get("name") or definition.get("function", {}).get("name")
        by_name[name] = definition

    total = sum(len(json.dumps(d)) for d in defs)
    core = {n: d for n, d in by_name.items() if n in CORE}
    core_chars = sum(len(json.dumps(d)) for d in core.values())

    print(f"  offered now          {len(defs):>3} tools  {total:>8,} chars  ~{total // 4:>6,} tok")
    print(f"  core only            {len(core):>3} tools  {core_chars:>8,} chars  "
          f"~{core_chars // 4:>6,} tok")
    saved = total - core_chars
    print(f"  ------------------------------------------------")
    print(f"  saved per request              {saved:>8,} chars  ~{saved // 4:>6,} tok "
          f"({saved / total * 100:.0f}%)")

    print("\n  by group, most expensive first:")
    rows = []
    for group, names in DEFERRED_GROUPS.items():
        present = [n for n in names if n in by_name]
        if not present:
            continue
        chars = sum(len(json.dumps(by_name[n])) for n in present)
        rows.append((chars, group, present))
    for chars, group, names in sorted(rows, reverse=True):
        print(f"    {group:13} {chars:>7,} chars  ~{chars // 4:>5,} tok   {', '.join(sorted(names))}")

    print("\n  over a 30-turn conversation, at 1 request per turn:")
    print(f"    now      {total * 30:>10,} chars  ~{total * 30 // 4:>9,} tok")
    print(f"    core     {core_chars * 30:>10,} chars  ~{core_chars * 30 // 4:>9,} tok")
    print(f"    saved    {saved * 30:>10,} chars  ~{saved * 30 // 4:>9,} tok")
    print("\n    (cached prefixes make the real cost a fraction of this -- which is exactly why the")
    print("     prefix must stay byte-stable, and why a rarely-unlocked tool is nearly free while a")
    print("     tool that is unlocked mid-session costs one cache miss.)")


def main() -> int:
    from myriaxon import tool_registry

    print("=== Anthropic-shaped (the registry's own) ===")
    measure(tool_registry.build_tool_defs())

    print("\n=== OpenAI-shaped ===")
    measure(tool_registry.build_tool_defs_openai())
    return 0


if __name__ == "__main__":
    sys.exit(main())
