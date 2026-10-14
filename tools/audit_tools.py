"""Audit `tools/` — classify every script, so W1.1's cleanup is measured rather than guessed.

W1.1 claims tools/ holds many stale one-shot scripts. This proves or disproves it: a file is an ORPHAN when its
module name is referenced nowhere in myriaxon/, tests/ or docs/.
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


# Everything that could plausibly reference a tool script.
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

buckets: dict[str, list[tuple[str, int, bool]]] = {
    "verifier": [], "counter": [], "wired": [], "orphan": [],
}
for path in sorted((ROOT / "tools").glob("*.py")):
    name = path.name
    module = name[:-3]
    referenced = module in blob or f"tools/{name}" in blob or f"tools\\{name}" in blob
    if name.startswith("verify_"):
        buckets["verifier"].append((name, code(path), referenced))
    elif name.startswith(("count_", "inventory", "check_", "probe_")):
        buckets["counter"].append((name, code(path), referenced))
    else:
        buckets["wired" if referenced else "orphan"].append((name, code(path), referenced))

for group in ("verifier", "counter", "wired", "orphan"):
    total = sum(c for _n, c, _r in buckets[group])
    print(f"  {group:<9} {len(buckets[group]):>3} files  {total:>7,} code lines")

print()
print("  === counters / checks (kept, they are the measurement) ===")
for name, lines, referenced in sorted(buckets["counter"], key=lambda x: -x[1]):
    print(f"    {name:<44} {lines:>5}  referenced={referenced}")

print()
print("  === WIRED helper scripts (referenced from the product, tests or docs) ===")
for name, lines, _r in sorted(buckets["wired"], key=lambda x: -x[1]):
    print(f"    {name:<44} {lines:>5}")

print()
print("  === ORPHANS (referenced nowhere) — deletion candidates ===")
orphans = sorted(buckets["orphan"], key=lambda x: -x[1])
for name, lines, _r in orphans:
    print(f"    {name:<44} {lines:>5}")
print(f"    ({len(orphans)} files, {sum(c for _n, c, _r in orphans):,} code lines)")
