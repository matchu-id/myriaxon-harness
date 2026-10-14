"""Fill the PiHarness and Opencode columns of the capability matrix from the two source audits.

Only rows where an audit key maps **unambiguously** to a matrix row are filled. Everything else stays `❓`, which the
scorer counts as `unknown` and excludes from the base — because "not checked" and "does not have it" are different
claims, and this document exists partly because three counts in this repository were invented by blurring that line.

The audit JSONs are in `docs/audits/`. Each filled cell records the verdict as a mark, and the evidence file is
named in the audit document rather than in every cell, which would make the table unreadable.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(r"C:\Users\arian\OneDrive\ドキュメント\Myriaxon")
MATRIX = ROOT / "docs" / "CAPABILITY_MATRIX.md"
AUDITS = {
    "PI": ROOT / "docs" / "audits" / "piharness-capabilities.json",
    "OCD": ROOT / "docs" / "audits" / "opencode-capabilities.json",
}

# Matrix row (as it appears, minus markdown bold) -> audit capability key.
#
# Deliberately conservative: a row is listed only when the matrix row and the audit key describe the SAME thing.
# "Sub-agents" maps; "Sub-agent trees visible" does not, because neither audit asked about visibility of the tree.
MAPPING = {
    "Iterative tool-calling loop": "iterative_tool_loop",
    "Global step cap": "budget_step_cap",
    "Wall-clock budget": "budget_wallclock",
    "Token budget": "budget_token",
    "Transcript rollback on failure": "transcript_rollback",
    "Retry with bounded policy": "auto_retry_toggle",
    "Compaction (summarise old history)": "compaction",
    "Summary rejected if not smaller": "compaction_summary_check",
    "Session search with literal-phrase queries": "session_tree",
    "Persistent memory across sessions": "persistent_agents",
    "Tool count (default visible)": "tool_deferral",
    "Deferred tool groups": "tool_deferral",
    "Discoverable by description": "tool_search",
    "Sandbox escalation ladder": "sandbox",
    "Permission modes": "permission_model",
    "Guardrail rules as data": "guardrails_rules",
    "Secret redaction": "secret_redaction",
    "Persistent agent identities": "persistent_agents",
    "Scheduled / recurring work": "scheduler",
    "Sub-agents": "subagents",
    "Workflow / fan-out orchestration": "parallel_subagents",
    "Skills with frontmatter": "skills",
    "Plugin registration contract": "plugin_api",
    "Health checks with dry-run repair": "diagnostics_service",
    "Benchmark harness": "evaluation_benchmark",
}

MARKS = {"YES": "✅", "PARTIAL": "🔶", "NO": "❌", "UNKNOWN": "❓"}


def normalise(cell: str) -> str:
    """The row label without markdown emphasis or backticks."""
    return cell.replace("**", "").replace("`", "").strip()


def main() -> int:
    audits = {}
    for column, path in AUDITS.items():
        if not path.is_file():
            print(f"  MISSING AUDIT: {path}")
            return 1
        audits[column] = json.loads(path.read_text(encoding="utf-8"))["capabilities"]

    lines = MATRIX.read_text(encoding="utf-8").splitlines()
    out: list[str] = []
    filled = {"PI": 0, "OCD": 0}
    unmatched = dict(MAPPING)

    for line in lines:
        if not line.startswith("|") or line.startswith("| Capability"):
            out.append(line)
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) != 8 or any(set(c) <= set("-: ") and c for c in cells):
            out.append(line)
            continue

        capability, myx, oc, dsh, cc, pi, ocd, notes = cells
        key = MAPPING.get(normalise(capability))
        if key is None:
            out.append(line)
            continue
        unmatched.pop(normalise(capability), None)

        for column, index in (("PI", 5), ("OCD", 6)):
            entry = audits[column].get(key)
            if entry is None:
                continue
            mark = MARKS.get(str(entry.get("verdict", "")).upper(), "❓")
            if mark == "❓":
                continue
            if column == "PI":
                pi = mark
            else:
                ocd = mark
            filled[column] += 1

        out.append(f"| {capability} | {myx} | {oc} | {dsh} | {cc} | {pi} | {ocd} | {notes} |")

    MATRIX.write_text("\n".join(out) + "\n", encoding="utf-8", newline="\n")
    print(f"  cells filled from evidence: PI={filled['PI']}  OCD={filled['OCD']}")
    if unmatched:
        print(f"  mapping entries that matched no row ({len(unmatched)}):")
        for name in sorted(unmatched):
            print(f"    {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
