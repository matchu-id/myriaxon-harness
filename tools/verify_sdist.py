"""Install the SDIST into a clean venv and assert the result is a working package.

## Why the sdist needs its own install test

The wheel and the sdist are built by different code paths. `MANIFEST.in` governs the sdist, and a source distribution
that omits a file the build needs fails at *install* time, on the user's machine, after they have waited for a
download and a compile. Nothing in the wheel checks exercise that path.

The specific risk here is `MANIFEST.in`: this repository added one to stop the sdist walk from stat'ing ~1 GB of
extracted reference trees. Getting that wrong in the permissive direction ships those trees; getting it wrong in the
restrictive direction produces an sdist that cannot build. This verifies the second failure mode, which is the one a
content listing cannot see.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import venv
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DIST = REPO / "dist"
PY = Path(sys.executable)
# The interpreter that owns the current one. When these checks run inside a virtualenv, `sys.executable` is the venv's
# python, and a venv created FROM it is nested: on Windows with the Store Python it shares the outer venv's
# site-packages, so installing the wheel under test lands in (and is reported as already present in) the outer
# environment. A clean venv has to be made from the base interpreter.
BASE_PY = Path(getattr(sys, "_base_executable", None) or sys.executable)


def _run(*args, **kwargs):
    """`subprocess.run` from a NEUTRAL working directory unless the caller names one.

    Running a venv's python or pip with the checkout as the working directory puts the checkout on `sys.path` (that is
    what `python -m` does), where the `myriaxon.egg-info` left by an earlier build is found, so pip reported
    "myriaxon is already installed" and never installed the wheel being tested. Builds pass `cwd` explicitly.
    """
    kwargs.setdefault("cwd", tempfile.gettempdir())
    try:
        return subprocess.run(*args, **kwargs)
    except OSError as exc:
        # WinError 4551: Windows Application Control (Smart App Control) refused to start a freshly created, unsigned
        # executable such as a console-script launcher. That is the machine's policy, not a defect in the package, so it
        # is reported as a failed command with the reason instead of crashing the whole verifier.
        if getattr(exc, "winerror", None) == 4551:
            return subprocess.CompletedProcess(args[0], 1, "", f"blocked by Windows Application Control: {exc}")
        raise

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _artifacts  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAIL).append(label)
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}" + (f"\n         {detail}" if detail else ""))


def main() -> int:
    # A release in dist/ if there is one, otherwise a fresh build from a clean copy (tools/_artifacts.py).
    try:
        sdists = _artifacts.sdists()
    except Exception as exc:  # noqa: BLE001
        sdists = []
        print("  could not build an sdist:", str(exc)[-300:])
    check("an sdist exists to test", bool(sdists), str([p.name for p in sdists]))
    if not sdists:
        return 1
    sdist = sdists[-1]
    print(f"  sdist: {sdist.name} ({sdist.stat().st_size / 1024:.0f} KB)")

    print()
    print("=== 1. it unpacks to a complete source tree ===")
    work = Path(tempfile.mkdtemp(prefix="myx-sdist-install-"))
    with tarfile.open(sdist) as tar:
        tar.extractall(work, filter="data")
    roots = [p for p in work.iterdir() if p.is_dir()]
    check("it unpacks to exactly one directory", len(roots) == 1, str([p.name for p in roots]))
    if len(roots) != 1:
        return 1
    source = roots[0]
    for needed in ("pyproject.toml", "MANIFEST.in", "README.md", "LICENSE",
                   "myriaxon/__init__.py", "myriaxon/cli.py", "myriaxon/py.typed",
                   "myriaxon/bench/tasks/p01_goal_bugfix_end_to_end/task.json"):
        check(f"  contains {needed}", (source / needed).is_file())

    print()
    print("=== 2. it BUILDS a wheel from the unpacked source ===")
    # This is the real test: a source distribution that cannot build is not a source distribution. Run it in a
    # subprocess so a build-time import error cannot be confused with this script's own imports.
    build_dir = work / "wheelhouse"
    build_dir.mkdir()
    script = (
        "import sys\n"
        f"sys.path.insert(0, r'{source}')\n"
        "from setuptools import build_meta as bm\n"
        f"print(bm.build_wheel(r'{build_dir}'))\n"
    )
    done = _run([str(PY), "-c", script], capture_output=True, text=True, timeout=900,
                          cwd=str(source))
    check("the sdist builds a wheel", done.returncode == 0,
          (done.stderr or done.stdout or "")[-300:])
    built = list(build_dir.glob("*.whl"))
    check("  a wheel was produced", bool(built), str([p.name for p in built]))

    print()
    print("=== 3. that wheel INSTALLS and imports in a clean venv ===")
    clean = work / "venv"
    made = _run([str(BASE_PY), "-m", "venv", str(clean)], capture_output=True, text=True, timeout=300)
    check("a clean venv can be created", made.returncode == 0, (made.stderr or "")[-160:])
    bindir = clean / ("Scripts" if sys.platform == "win32" else "bin")
    py = bindir / ("python.exe" if sys.platform == "win32" else "python")
    script_path = bindir / ("myriaxon.exe" if sys.platform == "win32" else "myriaxon")

    if built:
        installed = _run(
            [str(py), "-m", "pip", "install", "--quiet", "--no-deps", "--no-index", str(built[0])],
            capture_output=True, text=True, timeout=900)
        check("the wheel from the sdist installs", installed.returncode == 0,
              (installed.stderr or "")[-260:])
        check("  the console script exists", script_path.is_file(), str(script_path.name))

        # Import from a directory that is NOT the unpacked source, or `sys.path` would find the source tree and the
        # check would pass even if the install were empty.
        probe = _run(
            [str(py), "-c", "import myriaxon, pathlib; "
                            "print(pathlib.Path(myriaxon.__file__).parent)"],
            capture_output=True, text=True, cwd=str(work), timeout=120)
        check("  myriaxon imports from the venv", probe.returncode == 0, probe.stderr.strip()[:160])
        check("  and NOT from the unpacked source", str(source) not in probe.stdout,
              probe.stdout.strip()[:160])
        check("  the version is still readable", _run(
            [str(py), "-c", "import myriaxon; print(myriaxon.__version__)"],
            capture_output=True, text=True, timeout=120).stdout.strip() == "1.2.0")

        print()
        print("=== 4. package data survived the sdist round trip ===")
        # The failure this catches: MANIFEST.in too restrictive, so the sdist rebuilds a wheel that is missing the
        # skill or the bench tasks. A content listing of the sdist would not reveal it.
        data_probe = _run(
            [str(py), "-c",
             "import myriaxon, pathlib; b = pathlib.Path(myriaxon.__file__).parent; "
             "print(len(list((b/'bench'/'tasks').glob('*/task.json')))); "
             "print(len(list((b/'builtin_skills').glob('*/SKILL.md')))); "
             "print(len(list((b/'brand'/'assets').glob('*.svg'))))"],
            capture_output=True, text=True, cwd=str(work), timeout=120)
        counts = data_probe.stdout.split()
        check("  the bench tasks are present", counts and counts[0] == "14",
              data_probe.stdout.strip() or data_probe.stderr.strip()[:160])
        check("  the shipped skill is present", len(counts) > 1 and counts[1] == "1",
              data_probe.stdout.strip())
        check("  the brand assets are present", len(counts) > 2 and counts[2] == "3",
              data_probe.stdout.strip())

        print()
        print("=== 5. the first command works with no dependencies ===")
        for args in (["--version"], ["--help"]):
            done = _run([str(script_path), *args], capture_output=True, text=True, timeout=120)
            check(f"  myriaxon {' '.join(args)} exits 0", done.returncode == 0 and bool(done.stdout.strip()),
                  f"rc={done.returncode} err={done.stderr.strip()[:80]}")

    print()
    print("=== 5. the build-system requirement is one the build can actually satisfy ===")
    # Measured defect: `requires = ["setuptools>=68", "wheel"]` advertised compatibility with setuptools 68, but the
    # SPDX `license = "MIT"` string needs **77**. A fresh `python -m venv` on this host ships 65.5.0, so building
    # from the sdist failed with
    #     configuration error: `project.license` must be valid exactly by one definition (2 matches found)
    # -- a message naming a licence field rather than a version. The floor now matches what the configuration
    # requires, and `wheel` is gone because setuptools has bundled `bdist_wheel` since 70.1.
    with tarfile.open(sdist) as tar:
        name = next(n for n in tar.getnames() if n.endswith("pyproject.toml"))
        pyproject_text = tar.extractfile(name).read().decode("utf-8")
    requires_line = next((l for l in pyproject_text.splitlines() if l.startswith("requires =")), "")
    check("the build requires setuptools>=77 (what the SPDX licence needs)", "setuptools>=77" in requires_line,
          requires_line)
    check("  it no longer requires 'wheel' (bundled since setuptools 70.1)", "wheel" not in requires_line,
          requires_line)

    print()
    print("=== 6. the sdist's dependency on a build backend is DOCUMENTED ===")
    # A source distribution that silently needs the network is a support burden. The behaviour is inherent to PEP 517
    # and cannot be removed, so the requirement is that it is written down where a user will look.
    install_doc = (REPO / "INSTALL.md").read_text(encoding="utf-8")
    check("INSTALL.md explains the .tar.gz caveat", "tar.gz alone" in install_doc or "The `.tar.gz` alone" in install_doc)
    check("  it names the build-backend requirement", "build backend" in install_doc)
    check("  it gives the offline workaround", "--no-build-isolation" in install_doc)
    check("  and it compares wheel vs sdist in a table", "| `.whl` | `.tar.gz` |" in install_doc,
          "the tradeoff must be scannable, not buried in prose")

    print()
    print("=== 7. the wheel does NOT need a build backend (the reason to prefer it) ===")
    # The whole reason the wheel is the recommended artifact: it is unpacked and copied. Asserted by installing it
    # with `--no-index` and NO build backend available, which is impossible for the sdist.
    # `dist/` holds a wheel only after someone has built one. On a fresh checkout it does not, so the wheel this verifier
    # already built from the sdist in step 2 stands in: it is the same artifact, produced by the same backend.
    wheels = sorted((REPO / "dist").glob("*.whl")) or list(built)
    check("a wheel exists", bool(wheels), str([p.name for p in wheels]))
    if wheels:
        backendless = work / "backendless"
        _run([str(BASE_PY), "-m", "venv", str(backendless)], capture_output=True, text=True, timeout=300)
        bbindir = backendless / ("Scripts" if sys.platform == "win32" else "bin")
        bpy = bbindir / ("python.exe" if sys.platform == "win32" else "python")
        # Remove the build backend, so anything that needs one fails loudly.
        _run([str(bpy), "-m", "pip", "uninstall", "-y", "setuptools", "wheel"],
                       capture_output=True, text=True, timeout=300)
        probe = _run(
            [str(bpy), "-c", "import importlib.util as u; print(u.find_spec('setuptools') is not None)"],
            capture_output=True, text=True, timeout=120)
        check("  the test venv has no setuptools", probe.stdout.strip() == "False", probe.stdout.strip())

        installed = _run(
            [str(bpy), "-m", "pip", "install", "--quiet", "--no-index", "--no-deps", str(wheels[-1])],
            capture_output=True, text=True, timeout=600)
        check("  the wheel installs OFFLINE with no build backend", installed.returncode == 0,
              (installed.stderr or "")[-220:])
        version = _run(
            [str(bpy), "-c", "import myriaxon; print(myriaxon.__version__)"],
            capture_output=True, text=True, cwd=str(work), timeout=120)
        check("  and the installed package works", version.stdout.strip() == "1.2.0",
              version.stdout.strip() or version.stderr.strip()[:160])

        # And prove the sdist CANNOT do the same, which is the caveat being documented rather than assumed.
        sd = _run(
            [str(bpy), "-m", "pip", "install", "--no-index", "--no-deps", str(sdist)],
            capture_output=True, text=True, timeout=600)
        check("  the SDIST cannot install without a backend or a network", sd.returncode != 0,
              "this is the PEP 517 behaviour INSTALL.md documents")
        combined = sd.stdout + sd.stderr
        check("  and it says which requirement is missing", "setuptools" in combined,
              [l.strip() for l in combined.splitlines() if "setuptools" in l][:2])

    shutil.rmtree(work, ignore_errors=True)

    print()
    print(f"=== {len(PASS)} passed, {len(FAIL)} failed ===")
    for label in FAIL:
        print(f"  FAILED: {label}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
