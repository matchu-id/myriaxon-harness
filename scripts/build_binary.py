"""Builds a single-executable Myriaxon binary via PyInstaller.

Usage:
    pip install -e ".[build,all]"
    python scripts/build_binary.py

Produces ``dist/myriaxon.exe`` (Windows) or ``dist/myriaxon`` -- a self-contained binary that
needs neither a Python install nor a virtualenv on the target machine.

This defers to ``myriaxon.spec`` rather than passing its own command-line flags, so there is
exactly one place that decides what gets bundled. The previous version did the opposite and
passed ``--collect-all`` flags inline, which meant the spec and the script could disagree --
and both were missing the same thing: Myriaxon's own non-Python data (the built-in skill, the
IQB-14 task suite, the brand SVGs). Those are loaded by path at runtime, so nothing in the
build fails; the frozen binary simply starts with no built-in skills and a benchmark suite
containing zero tasks.
"""

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print('PyInstaller is not installed. Run: pip install -e ".[build]"', file=sys.stderr)
        return 1

    spec = ROOT / "myriaxon.spec"
    if not spec.is_file():
        print(f"Spec file not found: {spec}", file=sys.stderr)
        return 1

    for stale in (ROOT / "dist", ROOT / "build"):
        if stale.exists():
            shutil.rmtree(stale)

    result = subprocess.run(
        [sys.executable, "-m", "PyInstaller", str(spec), "--noconfirm", "--clean"],
        cwd=ROOT,
    )
    if result.returncode != 0:
        return result.returncode

    produced = sorted((ROOT / "dist").glob("myriaxon*"))
    if not produced:
        print("PyInstaller reported success but produced nothing in dist/.", file=sys.stderr)
        return 1
    for path in produced:
        print(f"\nBuilt: {path}  ({path.stat().st_size / 1_048_576:.1f} MB)")
    print("\nSmoke-test it with:  dist\\myriaxon --version")
    return 0


if __name__ == "__main__":
    sys.exit(main())
