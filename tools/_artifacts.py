"""Build the wheel and the source distribution from a throw-away COPY of the checkout.

Why a copy, and why not the checkout itself
-------------------------------------------
setuptools builds inside the source tree (`build/`, `*.egg-info/`) and never removes from `build/lib` a module that has
been deleted from the source since the last build. This repository renamed `dots.py` to `loci.py`: a build run in the
checkout after that rename would have packaged BOTH, because `build/lib/myriaxon/dots.py` was still sitting there. A copy
has no history, so what it builds is exactly what the source says.

It also keeps the checkout clean (nothing is written to it), and it means two checks can build at the same time without
clobbering each other.

Only what a build needs is copied (an allow-list), not the working tree. A checkout can hold gigabytes of extracted
reference code, virtualenvs and benchmark output that must never be walked, let alone shipped.

Used by `verify_packaging.py`, `verify_sdist.py` and `verify_ci.py`. Importable from any script in tools/ (a script's own
folder is on sys.path), e.g. `from _artifacts import build_wheel, build_sdist`.
"""

from __future__ import annotations

import atexit
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# The top-level files the build reads (pyproject.toml, MANIFEST.in) or ships (the rest, see MANIFEST.in).
TOP_FILES = ("pyproject.toml", "MANIFEST.in", "README.md", "LICENSE", "CHANGELOG.md", "INSTALL.md")
# The only directory that holds package code.
PACKAGE_DIR = "myriaxon"

_cache: dict[str, Path] = {}
_workdir: Path | None = None


def _root() -> Path:
    """One throw-away folder per process, removed when the process exits. It is the folder mkdtemp made, nothing above it."""
    global _workdir
    if _workdir is None:
        _workdir = Path(tempfile.mkdtemp(prefix="myx-build-"))
        atexit.register(shutil.rmtree, _workdir, True)
    return _workdir


def _copy_tree() -> Path:
    """A clean copy of what a build needs, made once."""
    if "tree" in _cache:
        return _cache["tree"]
    tree = _root() / "src"
    tree.mkdir()
    for name in TOP_FILES:
        source = REPO / name
        if source.is_file():
            shutil.copyfile(source, tree / name)
    shutil.copytree(
        REPO / PACKAGE_DIR, tree / PACKAGE_DIR,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo", ".pytest_cache"),
    )
    _cache["tree"] = tree
    return tree


def _backend_call(call: str, out: Path) -> str:
    """Run one PEP 517 backend hook (`build_wheel` or `build_sdist`) in the copy and return the file name it reports."""
    tree = _copy_tree()
    script = (
        "import sys\n"
        f"sys.path.insert(0, {str(tree)!r})\n"
        "from setuptools import build_meta as bm\n"
        f"print('BUILT=' + bm.{call}({str(out)!r}))\n"
    )
    # No PYTHONPATH: it can contain the checkout, and then the build would see the very tree this avoids.
    env = {k: v for k, v in os.environ.items() if k.upper() != "PYTHONPATH"}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    done = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, cwd=str(tree),
                          env=env, timeout=900)
    if done.returncode != 0:
        raise RuntimeError(f"{call} failed:\n" + (done.stderr or done.stdout or "no output")[-600:])
    for line in done.stdout.splitlines():
        if line.startswith("BUILT="):
            return line[len("BUILT="):].strip()
    raise RuntimeError(f"{call} printed no file name:\n" + done.stdout[-300:])


def build_wheel(out: Path | None = None) -> Path:
    """Build the wheel and return its path. Without `out`, it goes to a folder that is removed when the process exits."""
    key = f"wheel:{out}"
    if key not in _cache:
        target = Path(out) if out is not None else _root() / "wheel"
        target.mkdir(parents=True, exist_ok=True)
        _cache[key] = target / _backend_call("build_wheel", target)
    return _cache[key]


def build_sdist(out: Path | None = None) -> Path:
    """Build the source distribution and return its path. Same rules as `build_wheel`."""
    key = f"sdist:{out}"
    if key not in _cache:
        target = Path(out) if out is not None else _root() / "sdist"
        target.mkdir(parents=True, exist_ok=True)
        _cache[key] = target / _backend_call("build_sdist", target)
    return _cache[key]


def wheels() -> list[Path]:
    """The wheels in `dist/` if someone has built a release, otherwise one freshly built from a clean copy."""
    existing = sorted((REPO / "dist").glob("*.whl"))
    return existing or [build_wheel()]


def sdists() -> list[Path]:
    """The source distributions in `dist/` if someone has built a release, otherwise one freshly built from a clean copy."""
    existing = sorted((REPO / "dist").glob("*.tar.gz"))
    return existing or [build_sdist()]


if __name__ == "__main__":  # python tools/_artifacts.py  -> builds both and says where they went (then removes them)
    print("wheel:", build_wheel())
    print("sdist:", build_sdist())
