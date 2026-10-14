"""Find where Myriaxon can actually persist its config on this machine.

    python scripts/find_writable_config.py

Tests each candidate location by really writing a file (and removing it), rather than trusting
os.access -- on Windows os.access(W_OK) consults the ACL and happily reports True for a file that
cannot be modified but can be deleted, which is exactly the trap that made an earlier diagnosis
wrong.
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

REPO = Path(__file__).resolve().parent.parent
HOME = Path(os.environ.get("USERPROFILE", str(Path.home())))


def can_write(path: Path, *, exists_already: bool = False) -> tuple[bool, str]:
    """Actually attempt a write. Returns (ok, detail)."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return False, f"cannot create parent dir: {exc.__class__.__name__}"
    probe = path if exists_already else path.parent / f".probe_{path.name}"
    try:
        with probe.open("w", encoding="utf-8") as handle:
            handle.write("{}")
        if not exists_already:
            probe.unlink(missing_ok=True)
        return True, "write OK"
    except OSError as exc:
        return False, f"{exc.__class__.__name__}: {exc}"


def main() -> int:
    candidates = [
        ("existing user config (~/.myriaxon/config.json)", HOME / ".myriaxon" / "config.json", True),
        ("new file in ~/.myriaxon", HOME / ".myriaxon" / "newfile.json", False),
        ("new file in home dir", HOME / ".myriaxon_probe_dir" / "config.json", False),
        (".myriaxon inside the repo", REPO / ".myriaxon" / "config.json", False),
        ("repo-local bench-out dir", REPO / "bench-out" / ".myriaxon" / "config.json", False),
        ("OS temp dir", Path(tempfile.gettempdir()) / "myriaxon_config" / "config.json", False),
        ("LOCALAPPDATA", Path(os.environ.get("LOCALAPPDATA", "")) / "myriaxon" / "config.json", False),
        ("APPDATA", Path(os.environ.get("APPDATA", "")) / "myriaxon" / "config.json", False),
    ]

    print("=== real write tests (not os.access) ===\n")
    results = []
    for label, path, existing in candidates:
        ok, detail = can_write(path, exists_already=existing)
        results.append((label, path, ok, detail))
        mark = "OK  " if ok else "FAIL"
        print(f"  [{mark}] {label}\n         {path}\n         {detail}\n")

    winners = [r for r in results if r[2]]
    print("=== usable locations ===")
    if winners:
        for label, path, _, _ in winners:
            print(f"  {label}: {path}")
    else:
        print("  NONE -- Myriaxon cannot persist a config on this machine as the current user.")

    print("\n=== what this means for Myriaxon ===")
    print("  Myriaxon reads its config from ~/.myriaxon/config.json (or $MYRIAXON_CONFIG_DIR if set).")
    print("  There is no --config flag, so either:")
    print("    a) run with environment variables instead of a saved config, or")
    print("    b) grant this user modify rights on the file, e.g. in an elevated shell:")
    print(f'         icacls "{HOME}\\.myriaxon" /grant "%USERNAME%:(OI)(CI)M" /T')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
