"""Rigorous 6-harness gap scan.

A loose grep has already produced two false negatives in this session (`sessionFork` matched only a comment; "LSP"
matched `SignalSpinner`). This script therefore distinguishes three states per candidate rather than two:

  DEFINING FILE  -- an artefact whose name or declared purpose is the capability
  MENTION ONLY   -- the token appears, but only in prose, a docstring or an unrelated identifier
  ABSENT         -- no trace

That distinction is the whole point: "the token appears somewhere" is not evidence a mechanism exists, and treating
it as evidence is how a capability matrix ends up claiming things nobody built.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(r"C:\Users\arian\OneDrive\ドキュメント\Myriaxon")
MYX = ROOT / "myriaxon"

# (label, regex for the defining artefact, where to look)
# The regex names a FILE or a def/class, not a keyword, so a mention cannot satisfy it.
CANDIDATES = [
    ("LSP / language-server diagnostics", r"lsp", "file"),
    ("Session fork (branch a session)", r"session_fork|fork_session|branched_session|create_branch", "any"),
    ("Session tree / navigate ancestors", r"session_tree|navigate_tree|parent_session_id|session_graph", "any"),
    ("steer() — redirect mid-turn", r"def steer|\.steer\(", "any"),
    ("followUp() — queued next message", r"follow_up|followup", "any"),
    ("Session stats (tokens, cost, turns)", r"def .*session_stats|session_stats\(|get_session_stats", "any"),
    ("Active-tool selection per session", r"set_active_tools|active_tool_names|active_tools\b", "any"),
    ("File-state undo / revert to any point", r"def rewind_to|def restore|revert", "any"),
    ("Git worktree isolation", r"worktree", "def"),
    ("Background job service (list/wait/promote/cancel)", r"class .*BackgroundJob|def promote|wait_for_promotion", "any"),
    ("LSP diagnostics injected into context", r"diagnostic", "any"),
    ("Permission evaluate/arity rules", r"permission_evaluate|class Permission|def evaluate_permission", "any"),
    ("MCP catalogue + dynamic tools", r"def .*mcp_catalog|mcp_catalog|dynamic_tool", "any"),
    ("Session sharing / export", r"share_session|session_share|export_session", "any"),
    ("IDE integration", r"\bide_integration|class Ide\b|ide_client", "any"),
    ("Conflict detection between concurrent agents", r"conflict", "any"),
    ("Dependency resolution between tasks", r"dependency_resolver|resolve_dependencies|dependency_graph", "any"),
    ("Handoff between agents", r"handoff", "any"),
    ("Task scheduler with dependencies", r"task_scheduler|class Scheduler\b", "any"),
    ("Team / role-based multi-agent", r"team_service|class Team\b", "any"),
    ("Auto-retry toggle", r"auto_retry|autoRetry|set_auto_retry", "any"),
    ("Secret store (OS keychain)", r"keyring|keychain|credential_store|secret_store", "any"),
    ("Backup / restore of harness state", r"def backup|class Backup", "any"),
    ("Updater / self-update", r"self_update|def update_harness|class Updater", "any"),
    ("Compaction abort mid-flight", r"abort_compaction|cancel_compaction", "any"),
    ("Thinking-level control", r"thinking_level|reasoning_effort|\beffort\b", "any"),
    ("Run export (share a run's transcript)", r"export_run|run_export|def export", "any"),
    ("Artifacts service (produced files tracked)", r"artifact", "any"),
    ("Evaluation service (score a run)", r"evaluation_service|def evaluate_run|score_run", "any"),
    ("Compare two runs", r"compare_run|def compare", "any"),
    ("Checkpoints + rewind", r"def rewind_to", "any"),
    ("Plan mode (enter/exit)", r"plan_mode|plan_enter|plan_exit", "any"),
    ("Image input", r"image_block|image_input|vision", "any"),
]


def scan() -> None:
    sources = {}
    for path in MYX.glob("*.py"):
        sources[path.name] = path.read_text(encoding="utf-8", errors="replace")
    filenames = {p.stem.lower() for p in MYX.glob("*.py")}

    print(f"  {'capability':<52}{'verdict':<16}{'evidence'}")
    print("  " + "-" * 100)
    absent, mention, present = [], [], []
    for label, pattern, mode in CANDIDATES:
        rx = re.compile(pattern, re.I)
        evidence = ""
        verdict = "ABSENT"

        # A file whose NAME declares it, or a def/class line that does.
        named = [f for f in filenames if rx.search(f)]
        if named:
            verdict, evidence = "DEFINING FILE", ", ".join(sorted(named)[:3]) + ".py"
        else:
            hits = []
            for name, text in sources.items():
                for match in rx.finditer(text):
                    line_start = text.rfind("\n", 0, match.start()) + 1
                    line = text[line_start: text.find("\n", match.start())].strip()
                    if line.startswith(("def ", "class ", "    def ", "    class ")):
                        hits.append(f"{name}: {line[:58]}")
            if hits:
                verdict, evidence = "DEFINED", hits[0]
            else:
                # Token present at all, in any context?
                for name, text in sources.items():
                    if rx.search(text):
                        evidence = f"(prose only, e.g. {name}.py)"
                        break
                if evidence:
                    verdict = "MENTION ONLY"

        (present if verdict in ("DEFINING FILE", "DEFINED") else
         mention if verdict == "MENTION ONLY" else absent).append(label)
        print(f"  {label:<52}{verdict:<16}{evidence[:46]}")

    print()
    print(f"  PRESENT      : {len(present)}")
    print(f"  MENTION ONLY : {len(mention)}  {mention}")
    print(f"  ABSENT       : {len(absent)}  {absent}")


if __name__ == "__main__":
    scan()
