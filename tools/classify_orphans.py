"""Classify the orphaned `tools/` scripts precisely, so a cleanup is safe.

The audit proved tools/ holds 155 files referenced nowhere (14,129 code lines). Deleting on a name pattern would be
reckless: some are Myriaxon's own finished one-shot scripts, and most appear to belong to a different project that
happens to share this directory. This separates them by evidence — what the script's own docstring says it is
about — and prints the two lists for a human decision.
"""

import importlib.util
from pathlib import Path

ROOT = Path(r"C:\Users\arian\OneDrive\ドキュメント\Myriaxon")
spec = importlib.util.spec_from_file_location("cl", str(ROOT / "tools" / "count_loc.py"))
counter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(counter)


def code(path: Path) -> int:
    try:
        return counter.count_file(path)[1]
    except OSError:
        return 0


def read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


haystack: list[str] = []
for group in ("myriaxon", "tests", "docs"):
    base = ROOT / group
    if base.is_dir():
        for path in base.rglob("*"):
            if path.is_file() and path.suffix in (".py", ".md", ".json", ".toml", ".cmd", ".ps1", ".sh"):
                haystack.append(read(path))
for extra in ("README.md", "CHANGELOG.md", "INSTALL.md", "task.md", "pyproject.toml"):
    path = ROOT / extra
    if path.is_file():
        haystack.append(read(path))
blob = "\n".join(haystack)

# Markers that identify the OTHER project occupying this directory. Each is a word that appears in those scripts
# and has nothing to do with the Myriaxon harness.
OTHER_PROJECT_MARKERS = (
    "level", "badge", "localisation", "localis", "questions", "track", "certivo", "seed", "essay",
    "translat", "curricul", "criteri", "span_query", "sql",
)
# Markers that mean the script is about THIS harness.
MYRIAXON_MARKERS = (
    "dot", "heartbeat", "guardrail", "deferral", "tool", "poster", "checkpoint", "lease", "bot",
    "web_tools", "persist", "mode", "dashboard", "bench", "ollama", "sandbox", "provider",
)

myriaxon_orphans: list[tuple[str, int, str]] = []
other_orphans: list[tuple[str, int, str]] = []
for path in sorted((ROOT / "tools").glob("*.py")):
    name = path.name
    module = name[:-3]
    if name.startswith("verify_"):
        continue
    if module in blob or f"tools/{name}" in blob or f"tools\\{name}" in blob:
        continue
    body = read(path).lower()
    stem = module.lower()
    other = sum(1 for m in OTHER_PROJECT_MARKERS if m in stem or m in body[:400])
    mine = sum(1 for m in MYRIAXON_MARKERS if m in stem or m in body[:400])
    if mine > other:
        myriaxon_orphans.append((name, code(path), module))
    else:
        other_orphans.append((name, code(path), module))

print(f"  MYRIAXON one-shot scripts, referenced nowhere: {len(myriaxon_orphans)} files, "
      f"{sum(c for _n, c, _m in myriaxon_orphans):,} code lines")
for name, lines, _m in sorted(myriaxon_orphans, key=lambda x: -x[1]):
    print(f"    {name:<46} {lines:>5}")

print()
print(f"  OTHER PROJECT's scripts, referenced nowhere: {len(other_orphans)} files, "
      f"{sum(c for _n, c, _m in other_orphans):,} code lines")
print("    (first 15 by size — not Myriaxon's to keep in tools/, but not clearly safe to delete either)")
for name, lines, _m in sorted(other_orphans, key=lambda x: -x[1])[:15]:
    print(f"    {name:<46} {lines:>5}")
