"""Produce a factual inventory of Myriaxon's own capability surface.

The matrix this feeds must compare *measured* facts, not recall. Every number here is read from the code or
executed, so a claim in the comparison document can be traced back to this output rather than to memory.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(r"C:\Users\arian\OneDrive\ドキュメント\Myriaxon")
sys.path.insert(0, str(ROOT))

out: dict = {}


def section(name: str) -> None:
    out[name] = {}


# ---------------------------------------------------------------- modules
pkgs = sorted(p.stem for p in (ROOT / "myriaxon").glob("*.py"))
out["modules"] = {"count": len(pkgs), "names": pkgs}

# ---------------------------------------------------------------- tools
from myriaxon import tool_registry as TR  # noqa: E402

all_defs = TR.build_tool_defs()
names = [TR._def_name(d) for d in all_defs]
visible = [TR._def_name(d) for d in TR.apply_tool_visibility(all_defs, set())]
deferred = sorted(set(names) - set(visible))
out["tools"] = {
    "total": len(names),
    "visible_by_default": len(visible),
    "deferred": len(deferred),
    "deferred_names": deferred,
    "groups": {g: list(spec["tools"]) for g, spec in TR.DEFERRED_GROUPS.items()},
    "full_payload_chars": len(json.dumps(all_defs)),
    "visible_payload_chars": len(json.dumps(TR.apply_tool_visibility(all_defs, set()))),
}

# ---------------------------------------------------------------- CLI
try:
    from myriaxon.argparse_cli import build_parser

    parser = build_parser()
    commands = []
    for action in parser._actions:
        if hasattr(action, "choices") and isinstance(action.choices, dict):
            commands = sorted(action.choices)
    out["cli"] = {"commands": commands, "count": len(commands)}
except Exception as exc:  # noqa: BLE001
    out["cli"] = {"error": f"{type(exc).__name__}: {exc}"}

# ---------------------------------------------------------------- mechanisms built
mechanisms = [
    "run_capability", "extension_policy", "approvals", "run_budget", "loop_detection",
    "spill", "patching", "deadline", "compaction_audit", "runtime_context", "session_index",
    "sandbox_policy", "snapshots", "perf_gates", "skill_workshop", "skill_usage",
    "tool_catalog", "health", "instructions", "roles", "skill_trust", "skill_tools",
    "benchmark", "capability", "session_store", "dots", "mcp_client", "hooks",
    "compaction", "checkpoints", "memory_store", "model_adapt", "model_profiles",
    "grounding", "attachments", "atomic_io", "todos", "subagents",
]
present = {}
for name in mechanisms:
    path = ROOT / "myriaxon" / f"{name}.py"
    present[name] = {"present": path.is_file(), "kb": round(path.stat().st_size / 1024, 1) if path.is_file() else 0}
out["mechanisms"] = present

# ---------------------------------------------------------------- providers
providers = []
for name in ("agent", "openai_agent", "gemini_agent"):
    path = ROOT / "myriaxon" / f"{name}.py"
    providers.append({"module": name, "present": path.is_file(),
                      "kb": round(path.stat().st_size / 1024, 1) if path.is_file() else 0})
out["providers"] = providers

# ---------------------------------------------------------------- verifiers
verifiers = sorted(p.name for p in (ROOT / "tools").glob("verify_*.py"))
out["verifiers"] = {"count": len(verifiers), "names": verifiers}

# ---------------------------------------------------------------- tests
tests = sorted(p.name for p in (ROOT / "tests").glob("test_*.py"))
out["tests"] = {"count": len(tests), "names": tests}

# ---------------------------------------------------------------- actual measurements
try:
    from myriaxon import benchmark

    out["measured"] = {}
except Exception:  # noqa: BLE001
    out["measured"] = {}

# Tool payload in tokens, the number that decides what every request costs.
out["measured"]["tool_payload_tokens_full"] = len(json.dumps(all_defs)) // 4
out["measured"]["tool_payload_tokens_visible"] = len(json.dumps(
    TR.apply_tool_visibility(all_defs, set()))) // 4
out["measured"]["tool_payload_saving_pct"] = round(
    (1 - len(json.dumps(TR.apply_tool_visibility(all_defs, set()))) / len(json.dumps(all_defs))) * 100, 1)

# Deferred groups actually reachable by search.
try:
    catalog = TR.catalog_for_deferred_tools()
    out["measured"]["catalog_entries"] = len(catalog)
except Exception as exc:  # noqa: BLE001
    out["measured"]["catalog_entries"] = f"error: {exc}"

print(json.dumps(out, indent=2, default=str))
