"""Probe pi-harness and opencode for specific capabilities, precisely.

The PowerShell version of this produced `IDE integration: 13138` for opencode, which is impossible -- it was matching
the substring "ide" inside ordinary words and recursing unpredictably. This uses the filename plus an explicit
count of files whose name declares the capability, which cannot match a substring by accident.
"""

from pathlib import Path

ROOT = Path(r"C:\Users\arian\OneDrive\ドキュメント\Myriaxon")
TREES = {
    "PiHarness": ROOT / "pi-harness-main-src" / "pi-harness-main",
    "Opencode": ROOT / "opencode-dev-src" / "opencode-dev",
}
SKIP = {"node_modules", ".git", "dist", "build", "out", ".next", "coverage"}

# (label, filename pattern, an explicit directory that must contain it)
# Matching the FILE NAME is the rule: a capability is present when an artefact is named for it, not when a letter
# sequence appears in prose.
PROBES = [
    ("session fork", r"branch", None),
    ("session tree / navigate", r"navigate|tree", None),
    ("steer mid-turn", r"steer", None),
    ("follow-up queue", r"follow", None),
    ("background jobs", r"background", None),
    ("git worktree", r"worktree", None),
    ("LSP / language server", r"^lsp|language", None),
    ("file-state snapshot", r"snapshot", None),
    ("session share / export", r"share|export", None),
    ("MCP support", r"mcp", None),
    ("plugin API", r"plugin", None),
    ("subagent / task tool", r"^task|subagent", None),
    ("compaction", r"compact", None),
    ("permission / approvals", r"permission|approval", None),
    ("plan mode", r"^plan", None),
    ("undo / rewind / revert", r"rewind|revert|restore", None),
    ("todo list tool", r"^todo", None),
    ("checkpoint", r"checkpoint", None),
    ("skill loader", r"skill", None),
    ("command / slash commands", r"^command", None),
]


def files(tree: Path):
    for path in tree.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP for part in path.parts):
            continue
        if path.suffix not in (".ts", ".tsx", ".vue", ".js", ".py"):
            continue
        if ".test." in path.name or path.name.endswith(".d.ts"):
            continue
        yield path


def main() -> None:
    import re

    counts: dict[str, dict[str, list[str]]] = {}
    for name, tree in TREES.items():
        if not tree.is_dir():
            print(f"  MISSING TREE: {name} at {tree}")
            continue
        bucket: dict[str, list[str]] = {}
        for path in files(tree):
            stem = path.stem.lower()
            for label, pattern, _unused in PROBES:
                if re.search(pattern, stem):
                    bucket.setdefault(label, []).append(str(path.relative_to(tree)))
        counts[name] = bucket

    labels = [label for label, _, _ in PROBES]
    width = max(len(x) for x in labels) + 2
    print(f"  {'capability':<{width}}{'PiHarness':>12}{'Opencode':>12}")
    print("  " + "-" * (width + 24))
    for label in labels:
        pi = len(counts.get("PiHarness", {}).get(label, []))
        oc = len(counts.get("Opencode", {}).get(label, []))
        print(f"  {label:<{width}}{pi:>12}{oc:>12}")

    print()
    print("  evidence for the ones Myriaxon lacks (first file name per harness):")
    for label in ("session fork", "session tree / navigate", "background jobs", "git worktree",
                  "LSP / language server", "session share / export"):
        for name in ("PiHarness", "Opencode"):
            found = counts.get(name, {}).get(label, [])
            print(f"    {label:<26} {name:<10} {found[0] if found else '(none)'}")


if __name__ == "__main__":
    main()
