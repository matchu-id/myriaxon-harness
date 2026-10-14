"""Report the current state of the Myriaxon config directory, authoritatively."""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from myriaxon import constants  # noqa: E402


def main() -> int:
    cfg = constants.CONFIG_DIR
    print(f"config dir: {cfg}")
    print(f"exists    : {cfg.exists()}\n")

    if not cfg.exists():
        return 1

    print(f"{'name':26} {'kind':5} {'size':>10}")
    print("-" * 46)
    for entry in sorted(cfg.iterdir(), key=lambda p: (p.is_file(), p.name)):
        if entry.is_dir():
            files = [f for f in entry.rglob("*") if f.is_file()]
            size = sum(f.stat().st_size for f in files)
            print(f"{entry.name:26} {'dir':5} {size:>9,}  ({len(files)} file(s))")
        else:
            print(f"{entry.name:26} {'file':5} {entry.stat().st_size:>9,}")

    for name in ("dots", "history", "skills"):
        path = cfg / name
        if path.is_dir():
            children = sorted(p.name for p in path.iterdir())
            print(f"\n{name}/ -> {children if children else '(empty)'}")
        else:
            print(f"\n{name}/ -> absent")
    return 0


if __name__ == "__main__":
    sys.exit(main())
