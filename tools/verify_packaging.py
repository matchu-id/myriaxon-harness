#!/usr/bin/env python3
"""Verify the installation package: the wheel builds, installs clean, and the first command a user runs works.

## The defects this closes

The user's requirement was `"prepare for installation package too so everyone can install easily"`. The packaging
looked complete — `pyproject.toml`, `MANIFEST.in`, extras, a console script — and had **never been built into a
wheel and installed**. An editable install keeps the whole repository on `sys.path`, which hides exactly the class of
bug that only appears in a wheel.

Building it once found four, none of which any existing test could have caught:

 1. **`myriaxon --version` died with `ModuleNotFoundError: No module named 'rich'`.** The console script pointed at
    `myriaxon.cli:main`, bypassing the fast path in `__main__.py` that exists precisely so `--help`/`--version` cost
    nothing. So the very first command a new user runs paid the full ~150-module agent import and failed with a bare
    traceback. Fixed by pointing the entry point at `myriaxon.__main__:main`.
 2. **`myriaxon.__version__` raised `AttributeError`.** Only the non-standard `VERSION` existed, and the convention
    callers actually probe is the dunder — usually via `getattr(module, "__version__", "unknown")`, which degrades
    silently rather than raising.
 3. **A missing dependency produced a raw traceback**, naming an internal module several frames down. It now prints
    what is missing, the two install commands, and the interpreter it checked.
 4. **`python -m build` did not finish in ten minutes.** Two causes, both real: no `MANIFEST.in`, so the sdist walk
    stat'ed the ~1 GB of extracted rival trees; and `wheel` was genuinely absent, while `pip install wheel` stalled on
    the network. The build now goes through the PEP 517 backend directly and completes in about a second.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PY = Path(sys.executable)
# The interpreter that owns the current one. When these checks run inside a virtualenv, `sys.executable` is the venv's
# python, and a venv created FROM it is nested: on Windows with the Store Python it shares the outer venv's
# site-packages, so installing the wheel under test lands in (and is reported as already present in) the outer
# environment. A clean venv has to be made from the base interpreter.
BASE_PY = Path(getattr(sys, "_base_executable", None) or sys.executable)
PYPROJECT = REPO / "pyproject.toml"
MANIFEST = REPO / "MANIFEST.in"


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

# Directories that must NEVER be in a release. Each is huge, and two are actively sensitive.
FORBIDDEN_IN_WHEEL = (
    "openclaw-src", "opencode-dev-src", "pi-harness-main-src", "claude-code-main-src",
    "deepseek-harness-master-src", ".venv", ".myriaxon", "node_modules", "tests/",
)
# Files a wheel MUST carry, or the installed package is quietly incomplete.
REQUIRED_IN_WHEEL = ("myriaxon/py.typed", "entry_points.txt")


def check(label: str, condition: bool, detail: str = "") -> None:
    # Flushed per line, and plain rather than rich-rendered. Two reasons, both measured:
    #   * the summary below must be the LAST line and must match `=== N passed, M failed ===` exactly, because the
    #     aggregate runner in docs/GAP-PLAN.md §0 and the CI verifier both parse that shape;
    #   * an unflushed line is lost if the process is killed mid-run, which turns a completed prefix of checks into
    #     "NO SUMMARY" and reads as a crash.
    (PASS if condition else FAIL).append(label)
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}" + (f"\n         {detail}" if detail else ""), flush=True)


def build_wheel(outdir: Path) -> tuple[bool, str, Path | None]:
    """Build through the PEP 517 backend directly, from a clean COPY of the checkout (tools/_artifacts.py).

    NOT `python -m build`: that needs the `wheel` package in `--no-isolation`, and `pip install wheel` stalled on the
    network here for over ten minutes. `setuptools.build_meta` is what `python -m build` would call anyway, so this
    removes a dependency rather than working around one. The copy matters: a build in the checkout leaves `build/lib`
    behind, and setuptools never removes a module that was since deleted from the source.
    """
    try:
        return True, "", _artifacts.build_wheel(outdir)
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)[-300:], None


def main() -> int:
    print("=== 1. the packaging metadata declares what a user needs ===")
    text = PYPROJECT.read_text(encoding="utf-8")
    check("a build backend is declared", "[build-system]" in text and "build-backend" in text)
    # The DISTRIBUTION name is `myriaxon-harness` and the IMPORT package is `myriaxon`. That split is normal
    # (PEP 503 normalises the distribution name), so the check asserts both halves rather than one string --
    # asserting `name = "myriaxon"` exactly is what failed the moment the public name was chosen.
    check("the package name is right",
          'name = "myriaxon-harness"' in text or 'name = "myriaxon"' in text,
          "expected a distribution name of myriaxon-harness")
    check("a console script is declared", "[project.scripts]" in text)
    check("  and it routes through __main__ (the fast path)",
          "myriaxon.__main__:main" in text,
          "cli:main bypasses the fast path and dies without rich")
    # Scoped to the DECLARATION, not the whole file. The first version asserted `"myriaxon.cli:main" not in text` and
    # failed against correct code, because the comment explaining the fix necessarily quotes the wrong value it
    # replaced. A negative substring test over a file that documents the mistake will always match the documentation.
    script_lines = [line for line in text.splitlines()
                    if line.strip().startswith("myriaxon")]
    check("  the declaration itself does not use cli:main",
          bool(script_lines) and all("cli:main" not in line for line in script_lines),
          str(script_lines))
    check("the version is derived, not duplicated", 'version = { attr = "myriaxon.constants.VERSION" }' in text)
    check("the hard dependencies are declared", "rich>=13.7" in text and "httpx>=0.27" in text)
    check("the optional extras exist",
          all(x in text for x in ("openai = [", "gemini = [", "mcp = [", "poster = [", "keychain = [", "all = [")))
    check("subpackages are found, not listed by hand",
          "[tool.setuptools.packages.find]" in text and 'include = ["myriaxon*"]' in text)
    check("a MANIFEST.in exists to bound the sdist", MANIFEST.is_file())

    print()
    print("=== 2. the sdist excludes the trees that made the build hang ===")
    manifest = MANIFEST.read_text(encoding="utf-8") if MANIFEST.is_file() else ""
    for entry in ("openclaw-src", "opencode-dev-src", "pi-harness-main-src", "claude-code-main-src",
                  "deepseek-harness-master-src", ".venv", ".myriaxon", ".tooling"):
        check(f"excludes {entry}", f"prune {entry}" in manifest)
    # Every large locus-directory at the repo root must be pruned, checked against the DIRECTORY LISTING rather than
    # against a remembered list. `.uv` (0.41 GB: uv's cache plus a downloaded CPython) was missing while
    # `.npmcache` and `.pnpm-store` were present, because those two were added when their own problem was noticed
    # and the sweep was never made general. A release is not the place to discover the next one.
    #
    # The size walk tolerates an unreadable entry. Measured: `.venv-harbor-linux/lib64` is a Linux venv's symlink,
    # which on Windows is dangling, and `Path.stat()` raises `OSError [WinError 1920]` on it -- so a size check that
    # did not catch that crashed the whole verifier rather than reporting anything.
    def _tree_bytes(root: Path) -> int:
        total = 0
        for dirpath, _dirnames, filenames in os.walk(root, onerror=lambda _e: None):
            for name in filenames:
                try:
                    total += os.path.getsize(os.path.join(dirpath, name))
                except OSError:
                    # Dangling symlink, permission denial, or a Windows reparse point. The exact size does not
                    # matter; the 5 MB threshold does.
                    continue
        return total

    big_dot_dirs = []
    for path in sorted(REPO.iterdir()):
        if not path.is_dir() or not path.name.startswith(".") or path.name in (".git", ".github"):
            continue
        try:
            if _tree_bytes(path) > 5 * 1024 * 1024:
                big_dot_dirs.append(path.name)
        except OSError as exc:
            check(f"could measure {path.name}", False, f"{type(exc).__name__}: {exc}")
    unpruned = [name for name in big_dot_dirs if f"prune {name}" not in manifest]
    check("every large locus-directory is pruned", not unpruned,
          f"unpruned: {unpruned} (of {len(big_dot_dirs)} large locus-dirs: {big_dot_dirs})")
    check("excludes extracted trees generally (the rule, not one entry per drop)",
          "prune *-src" in manifest)
    check("INCLUDES the shipped skill", "builtin_skills" in manifest)
    check("INCLUDES the bench tasks", "bench/tasks" in manifest)
    check("INCLUDES the brand assets", "brand/assets" in manifest)

    # A rule that silently matches nothing is worse than no rule: it reads as protection. Measured defect -- the
    # first `MANIFEST.in` used `global-exclude *.py[cod]`, and setuptools reported
    # `warning: no previously-included files matching '*.py[cod]' found anywhere in distribution`. `[]` is fnmatch
    # syntax, not glob. The build's own warning is the signal, so it is asserted here rather than eyeballed.
    #
    # Scoped to DIRECTIVE lines, not the whole file. Asserting `"*.py[cod]" not in manifest` failed against correct
    # code, because the comment explaining the fix necessarily quotes the broken pattern -- the same trap the
    # `cli:main` check above fell into. A negative substring test over a file that documents a mistake always matches
    # the documentation.
    directives = [line.strip() for line in manifest.splitlines()
                  if line.strip() and not line.strip().startswith("#")]
    check("no ineffective glob patterns remain (`[]` is not glob syntax)",
          not any("[" in line for line in directives),
          str([line for line in directives if "[" in line]))
    check("  and the suffixes are listed explicitly",
          all(line in directives for line in ("global-exclude *.pyc", "global-exclude *.pyo")),
          "one line per compiled suffix")
    check("no redundant prune+exclude of the same path",
          not any(line == f"exclude {name}" for line in directives
                  for name in ("openclaw-src", "opencode-dev-src", ".myriaxon", ".venv", ".tooling")),
          "prune already removes the directory and its contents")

    print()
    print("=== 3. the wheel BUILDS ===")
    outdir = Path(tempfile.mkdtemp(prefix="myx-verify-wheel-"))
    ok, error, wheel = build_wheel(outdir)
    check("the wheel builds", ok, error)
    if not ok or wheel is None:
        print()
        print(f"=== {len(PASS)} passed, {len(FAIL)} failed ===")
        for label in FAIL:
            print(f"  FAILED: {label}")
        return 1
    # Derived from the metadata rather than hardcoded. The literal `myriaxon-1.2.0` was here and it broke
    # twice over on the 0.0.1 public release: the name gained a `-harness` suffix and the version changed. A
    # check that has to be edited every release is a check that will be edited WRONG, so it now reads what the
    # package declares and asserts the wheel agrees with it.
    #
    # `version` is DYNAMIC here -- `version = { attr = "myriaxon.constants.VERSION" }` under
    # `[tool.setuptools.dynamic]` -- so the naive `line.startswith("version = ")` picked up THAT line and
    # produced the expected prefix `myriaxon_harness-{ attr = ... }`. The parse has to respect the dynamic
    # declaration and follow it to the attribute, which is the single source of truth anyway.
    declared_name = ""
    declared_version = ""
    for line in text.splitlines():
        if line.startswith("name = ") and not declared_name:
            declared_name = line.split("=", 1)[1].strip().strip('"')
        if line.startswith("version = ") and not declared_version:
            candidate = line.split("=", 1)[1].strip()
            # A dynamic declaration is a table, not a version.
            if not candidate.startswith("{"):
                declared_version = candidate.strip('"')
    if not declared_version:
        import importlib  # noqa: PLC0415

        constants = importlib.import_module("myriaxon.constants")
        declared_version = constants.VERSION

    normalised = declared_name.replace("-", "_")
    check("  it is named for the package and version",
          wheel.name.startswith(f"{normalised}-{declared_version}"),
          f"{wheel.name} (expected {normalised}-{declared_version}...)")
    check("  it is a pure-python wheel (py3-none-any)", "py3-none-any" in wheel.name, wheel.name)

    print()
    print("=== 4. the wheel CONTAINS what an installed copy needs ===")
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        entry_points = ""
        for name in names:
            if name.endswith("entry_points.txt"):
                entry_points = archive.read(name).decode("utf-8", "replace")
        for required in REQUIRED_IN_WHEEL:
            check(f"contains {required}", any(required in n for n in names))
        check("contains the skill", any(n.endswith("SKILL.md") for n in names))
        check("contains the bench tasks", any("/bench/tasks/" in n for n in names))
        check("contains the brand SVGs", any(n.endswith(".svg") for n in names))
        check("contains the CLI module", "myriaxon/cli.py" in names)
        check("contains the dashboard subpackage", any(n.startswith("myriaxon/dashboard/") for n in names))
        check("contains a good number of modules", sum(1 for n in names if n.endswith(".py")) > 100,
              f"{sum(1 for n in names if n.endswith('.py'))} .py files")
        # The exclusions are the point of MANIFEST.in, so they are asserted on the ARTIFACT and not on the file.
        leaked = [n for n in names if any(bad in n for bad in FORBIDDEN_IN_WHEEL)]
        check("contains NONE of the reference trees, venvs or session state", not leaked,
              f"{len(leaked)} leaked: {leaked[:3]}")
        check("the entry point routes through __main__",
              "myriaxon.__main__:main" in entry_points,
              entry_points.strip().replace("\n", " | "))
        check("  and declares the myriaxon command", "myriaxon =" in entry_points)

    print()
    print("=== 5. it INSTALLS into a clean virtualenv ===")
    clean = Path(tempfile.mkdtemp(prefix="myx-verify-venv-")) / "venv"
    made = _run([str(BASE_PY), "-m", "venv", str(clean)], capture_output=True, text=True, timeout=300)
    check("a clean venv can be created", made.returncode == 0, (made.stderr or "")[-160:])
    if made.returncode != 0:
        print(f"\n=== {len(PASS)} passed, {len(FAIL)} failed ===")
        return 1
    venv_py = clean / ("Scripts" if sys.platform == "win32" else "bin") / (
        "python.exe" if sys.platform == "win32" else "python")
    # `--no-deps --no-index` so this check is OFFLINE and deterministic. It proves the wheel's own contents and entry
    # points are sound; whether PyPI is reachable is not this verifier's business.
    installed = _run(
        [str(venv_py), "-m", "pip", "install", "--quiet", "--no-deps", "--no-index", str(wheel)],
        capture_output=True, text=True, timeout=600)
    check("the wheel installs offline", installed.returncode == 0, (installed.stderr or "")[-200:])

    script = clean / ("Scripts" if sys.platform == "win32" else "bin") / (
        "myriaxon.exe" if sys.platform == "win32" else "myriaxon")
    if sys.platform != "win32":
        script = clean / "bin" / "myriaxon"
    check("the console script exists", script.is_file(), str(script.name))

    # A no-dependency venv is DELIBERATE: it is the state a user is in if an install half-finished, and it is where
    # the `rich` traceback appeared. These checks assert the first commands still work there.
    if script.is_file():
        for args, want_zero in ((["--version"], True), (["--help"], True), (["-h"], True)):
            done = _run([str(script), *args], capture_output=True, text=True, timeout=120)
            label = " ".join(args)
            check(f"`myriaxon {label}` exits 0 with NO dependencies",
                  done.returncode == 0 and bool(done.stdout.strip()),
                  f"rc={done.returncode} stdout={len(done.stdout)}B stderr={done.stderr.strip()[:80]}")
        done = _run([str(script), "--version"], capture_output=True, text=True, timeout=120)
        check(f"  and prints the version ({declared_version})",
                        declared_version in done.stdout, done.stdout.strip())
        check("  without a traceback", "Traceback" not in (done.stdout + done.stderr))
        check("  and writes it to stdout, not stderr", not done.stderr.strip(), done.stderr.strip()[:80])

    print()
    print("=== 6. a missing dependency EXPLAINS itself instead of tracebacking ===")
    # The state a user reaches when an install half-completes. A bare ModuleNotFoundError reads as "broken"; the
    # message must name the module and both install commands.
    if script.is_file():
        done = _run([str(script), "doctor"], capture_output=True, text=True, timeout=120)
        combined = done.stdout + done.stderr
        check("a real command does not traceback", "Traceback" not in combined, combined[:100])
        check("  it names the missing module", "rich" in combined, combined[:120])
        check("  it gives the install command", "pip install" in combined, combined[:200])
        check("  it mentions the [all] extra", "[all]" in combined, combined[:200])
        check("  it names the interpreter checked", "python" in combined.lower(), combined[-200:])
        check("  and it fails with a non-zero code", done.returncode != 0, f"rc={done.returncode}")
        check("  a clear code, not a crash code", done.returncode == 2, f"rc={done.returncode}")

    print()
    print("=== 7. the version is readable the conventional way ===")
    done = _run([str(venv_py), "-c",
                           "import myriaxon; print(myriaxon.__version__)"],
                          capture_output=True, text=True, timeout=120)
    check("myriaxon.__version__ exists", done.returncode == 0, done.stderr.strip()[:120])
    check(f"  and equals VERSION ({declared_version})",
                    done.stdout.strip() == declared_version, done.stdout.strip())
    done = _run([str(venv_py), "-c",
                           "import myriaxon; print(myriaxon.VERSION == myriaxon.__version__)"],
                          capture_output=True, text=True, timeout=120)
    check("  the two cannot drift", done.stdout.strip() == "True", done.stdout.strip())
    done = _run([str(venv_py), "-c",
                           "from importlib.metadata import version; print(version('myriaxon'))"],
                          capture_output=True, text=True, timeout=120)
    check(f"importlib.metadata agrees ({declared_version})",
                    done.stdout.strip() == declared_version, done.stdout.strip() or done.stderr[:120])

    print()
    print("=== 8. the entry point is the one the fast path is on ===")
    # `main` must be importable from __main__ without touching the heavy CLI, or the fix is cosmetic.
    done = _run(
        [str(venv_py), "-c",
         "import sys; import myriaxon.__main__ as m; "
         "print('rich' in sys.modules, 'myriaxon.cli' in sys.modules)"],
        capture_output=True, text=True, timeout=120)
    check("importing __main__ does NOT import rich", done.stdout.strip().startswith("False"),
          done.stdout.strip() or done.stderr[:120])
    check("  nor the heavy cli module", done.stdout.strip().endswith("False"), done.stdout.strip())
    done = _run([str(venv_py), "-c",
                           "import myriaxon.__main__ as m; print(callable(m.main))"],
                          capture_output=True, text=True, timeout=120)
    check("__main__.main is the callable the script needs", done.stdout.strip() == "True",
          done.stdout.strip() or done.stderr[:120])

    print()
    print("=== 9. the repository's own copy still imports ===")
    # The packaging change must not break running from the checkout, which is how the tests and verifiers work.
    done = _run([str(PY), "-c",
                           "import sys; sys.path.insert(0, r'%s'); import myriaxon; "
                           "print(myriaxon.__version__)" % REPO],
                          capture_output=True, text=True, timeout=120)
    check("the checkout's package imports", done.returncode == 0, done.stderr.strip()[:120])
    check(f"  with the conventional version attribute ({declared_version})",
                    done.stdout.strip() == declared_version, done.stdout.strip())

    # Tidy up: a verifier should not leave hundreds of megabytes in temp.
    # outdir IS the folder mkdtemp created. Deleting outdir.parent deleted the whole temp directory (and, where the
    # temp directory fell back to the project folder, the project itself).
    shutil.rmtree(outdir, ignore_errors=True)
    shutil.rmtree(clean.parent, ignore_errors=True)

    print()
    print(f"=== {len(PASS)} passed, {len(FAIL)} failed ===")
    for label in FAIL:
        print(f"  FAILED: {label}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
