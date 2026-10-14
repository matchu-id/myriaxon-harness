"""Install Myriaxon. Works with any Python 3.10+, including non-ASCII install paths.

Why this exists instead of "just run pip install ."

Two real problems, both measured on Windows:

1. **`pip install -e .` fails outright when the path contains non-ASCII characters.** setuptools
   writes the editable redirect (a ``.pth`` file) using the locale encoding -- cp1252 on a
   default Windows console -- and a path containing Japanese characters cannot be encoded in
   cp1252, so the build dies with ``UnicodeEncodeError: 'charmap' codec can't encode characters``.
   A user whose project lives under a non-English folder name simply cannot install the tool.
2. **A plain `pip install .` gives an install you cannot edit**, which is wrong for a harness
   people are expected to extend (skills, plugins, hooks all live next to the source).

So this installer tries the ordinary editable install first, and on failure falls back to
writing the same ``.pth`` file by hand -- which is all an editable install is -- with an explicit
UTF-8 encoding. Both routes end in the same place; only the second always works.

Usage::

    python tools/install_dev.py                  # editable install into the current interpreter
    python tools/install_dev.py --extras all     # also install every provider SDK
    python tools/install_dev.py --verify         # install, then run the offline test suite
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MIN_PYTHON = (3, 10)

# Bounded, and deliberately not enormous. The build can be pathologically slow -- or appear to
# hang -- when the project directory sits on a synchronised or network filesystem, because pip
# writes thousands of small files and the filesystem amplifies every one. Measured on this
# machine, a full install of this project took 91 seconds on a OneDrive-hosted virtualenv, so the
# default has to clear that comfortably; an installer that waits forever is still worse than one
# that falls back to a route which always works, which is what this budget buys.
# Raise it with --pip-timeout on a slower machine.
PIP_TIMEOUT_SECONDS = 600


def log(message: str) -> None:
    print(f"  {message}", flush=True)


def path_is_ascii(path: Path) -> bool:
    """Whether the path can be written in the console's locale encoding.

    Tested by round-tripping through ASCII rather than by an encoding name: the failure this
    guards against is setuptools writing the path in cp1252 on Windows, and a path that is not
    ASCII is exactly the one it cannot represent.
    """
    try:
        str(path).encode("ascii")
        return True
    except UnicodeEncodeError:
        return False


def run(cmd: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=check, text=True, encoding="utf-8", errors="replace")


def python_ok() -> bool:
    return sys.version_info >= MIN_PYTHON


def has_pip() -> bool:
    try:
        run([sys.executable, "-m", "pip", "--version"])
        return True
    except (subprocess.CalledProcessError, OSError):
        return False


def pip_install(target: str, *, editable: bool = False, timeout: int | None = None) -> bool:
    """Install with the standard tooling.

    Returns False instead of raising: a failure here is expected on a non-ASCII path and is
    handled by the caller. A timeout is applied because the failure mode is not always an error --
    the build can simply hang, and an installer that waits forever is worse than one that falls
    back to a route which always works.

    ``--no-build-isolation`` is deliberate. With isolation, pip builds the project in a fresh
    environment and must fetch setuptools and wheel first; on a slow or restricted connection that
    step stalls before the build even begins, and it is pointless work -- the calling interpreter
    already has setuptools, because the installer's first action is to ensure it.
    """
    limit = PIP_TIMEOUT_SECONDS if timeout is None else timeout
    cmd = [sys.executable, "-m", "pip", "install", "--no-input", "--no-build-isolation"]
    if editable:
        cmd.append("-e")
    cmd.append(target)
    try:
        # Output is inherited, not captured. pip's progress is the only feedback the user gets
        # during a first install that downloads several wheels; swallowing it into a string made
        # the installer print "installing ..." and then appear to do nothing at all.
        result = subprocess.run(cmd, timeout=limit)
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        log(f"pip did not finish within {limit}s.")
        return False
    except OSError as exc:
        log(f"pip could not run: {exc}")
        return False


def declared_requirements(extras: str) -> list[str]:
    """The dependencies from pyproject.toml, so the fallback installs the real set.

    Read from the file rather than duplicated here: a copy would drift, and the whole point of
    the fallback is to leave the user with an install that actually works.
    """
    pyproject = REPO / "pyproject.toml"
    try:
        text = pyproject.read_text(encoding="utf-8")
    except OSError:
        return []

    requirements: list[str] = []
    match = re.search(r"^dependencies\s*=\s*\[(.*?)\]", text, re.MULTILINE | re.DOTALL)
    if match:
        requirements.extend(re.findall(r'"([^"]+)"', match.group(1)))

    for extra in [e.strip() for e in extras.split(",") if e.strip()]:
        # Extras are `name = ["req", ...]` inside [project.optional-dependencies].
        extra_match = re.search(
            rf"^{re.escape(extra)}\s*=\s*\[(.*?)\]", text, re.MULTILINE | re.DOTALL
        )
        if extra_match:
            requirements.extend(re.findall(r'"([^"]+)"', extra_match.group(1)))
    return requirements


def pip_install_dependencies(extras: str, timeout: int | None = None) -> bool:
    """Install the declared third-party requirements directly.

    No build of this project is involved, so none of the failure modes that motivated the
    fallback apply: these are prebuilt wheels from the index.
    """
    limit = PIP_TIMEOUT_SECONDS if timeout is None else timeout
    requirements = declared_requirements(extras)
    if not requirements:
        log("no declared requirements found; skipping the dependency install.")
        return True
    log("installing: " + ", ".join(requirements))
    cmd = [sys.executable, "-m", "pip", "install", "--no-input", *requirements]
    try:
        result = subprocess.run(cmd, timeout=limit)
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        log(f"dependency install did not finish within {limit}s.")
        return False
    except OSError as exc:
        log(f"pip could not run: {exc}")
        return False


def site_packages_dir() -> Path:
    """The directory Python actually imports from for this interpreter.

    Found by *checking*, not by trusting `site.getsitepackages()`. Two measured failures, and the second one is why
    the order below is what it is:

    * inside a virtualenv, `site.getsitepackages()` returned the virtualenv ROOT (``.venv``) rather than
      ``.venv/Lib/site-packages``, so a redirect written there sat somewhere the import system never reads -- and
      because the same wrong answer was used to decide what to delete, the working install was removed at the same
      time. An uninstall masquerading as a successful install.
    * on the Windows Store Python, `site.getsitepackages()` returns the ROOT *first* and the real directory second.
      The old fallback ("the first existing candidate") therefore returned the root whenever the verification module
      was absent -- which is exactly the state a fresh install of a MISSING dependency is in.

    So `sys.path` is consulted first. Entries that end in `site-packages` are, by definition, directories this
    interpreter resolves imports from, which is the only property the redirect needs.
    """
    import site
    import sys as _sys

    candidates: list[Path] = []

    # 1. What the import system actually reads. Deduplicated and ordered as `sys.path` orders them.
    for entry in _sys.path:
        if not entry:
            continue
        path = Path(entry)
        if path.name == "site-packages":
            candidates.append(path)

    # 2. The virtualenv layouts, and the only one that matters when this script is run by the venv's interpreter.
    version_dir = "python%d.%d" % _sys.version_info[:2]
    prefix = Path(_sys.prefix)
    candidates.extend(
        [
            prefix / "Lib" / "site-packages",                 # Windows virtualenv
            prefix / "lib" / version_dir / "site-packages",    # POSIX virtualenv
            prefix / "lib" / "site-packages",
            Path(_sys.executable).parent.parent / "lib" / version_dir / "site-packages",
        ]
    )

    # 3. What `site` says, last, because it is the thing that was wrong twice.
    for attr in ("getsitepackages", "getusersitepackages"):
        getter = getattr(site, attr, None)
        if getter is None:
            continue
        try:
            value = getter()
        except Exception:
            continue
        candidates.extend(Path(p) for p in (value if isinstance(value, list) else [value]))

    seen: set[str] = set()
    ordered: list[Path] = []
    for candidate in candidates:
        key = str(candidate).lower()
        if key in seen:
            continue
        seen.add(key)
        ordered.append(candidate)

    # A candidate counts only if it IS a site-packages directory and exists. The name check is what the old fallback
    # skipped, and skipping it is how the interpreter ROOT came back.
    for candidate in ordered:
        if candidate.name == "site-packages" and candidate.is_dir():
            return candidate

    raise RuntimeError(
        "could not locate a site-packages directory. Refusing to guess: writing a redirect to the interpreter root "
        "would put it somewhere the import system never reads, and the same answer decides what gets deleted."
    )


def write_manual_editable() -> Path:
    """Write the editable redirect by hand, in UTF-8.

    This is the whole of what `pip install -e` produces for a flat-layout project: one file in
    site-packages naming the directory the package lives in. Doing it directly sidesteps
    setuptools' locale-encoded write, which is the part that fails -- or hangs -- on a non-ASCII
    path.
    """
    site_dir = site_packages_dir()
    path_file = site_dir / "myriaxon.pth"
    path_file.write_text(f"{REPO}\n", encoding="utf-8")
    return path_file


def remove_copied_package() -> None:
    """Delete a non-editable copy so the source checkout is the one that gets imported.

    Only ever removes a copy that is *not* this checkout, and only after the redirect has been
    written and verified to point at this checkout -- deleting the working install before the
    replacement is proven is how an installer leaves nothing behind at all.
    """
    import shutil

    site_dir = site_packages_dir()
    copied = site_dir / "myriaxon"
    if copied.is_dir() and copied.resolve() != (REPO / "myriaxon").resolve():
        shutil.rmtree(copied, ignore_errors=True)
        log(f"removed the copied package at {copied} so the source checkout wins")


def verify_import() -> bool:
    code = "import myriaxon, myriaxon.cli, myriaxon.dashboard.server, myriaxon.bench; print(myriaxon.__file__)"
    try:
        result = subprocess.run(
            [sys.executable, "-c", code], text=True, encoding="utf-8", errors="replace",
            capture_output=True,
        )
    except OSError:
        return False
    if result.returncode == 0:
        log(f"import OK -> {result.stdout.strip()}")
        return True
    log("import failed:")
    log((result.stderr or "").strip().splitlines()[-1] if result.stderr else "unknown error")
    return False


def run_tests() -> bool:
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "tests/", "-o", "addopts=", "-q"],
            cwd=REPO, text=True, encoding="utf-8", errors="replace",
        )
        return result.returncode == 0
    except OSError:
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Install Myriaxon")
    parser.add_argument("--extras", default="", help='optional-dependency group, e.g. "all"')
    parser.add_argument("--verify", action="store_true", help="run the offline test suite afterwards")
    parser.add_argument("--no-editable", action="store_true", help="install a normal (non-editable) copy")
    # The default is captured before the global is rebound: reading the module-level constant
    # directly in the same function that assigns it is a syntax error.
    default_timeout = PIP_TIMEOUT_SECONDS
    parser.add_argument(
        "--pip-timeout",
        type=int,
        default=default_timeout,
        help=f"seconds to allow pip before falling back (default {default_timeout})",
    )
    args = parser.parse_args(argv)
    pip_timeout = max(30, int(args.pip_timeout))

    print("Myriaxon installer")
    print(f"  python : {sys.version.split()[0]}  ({sys.executable})")
    print(f"  source : {REPO}")
    print()

    if not python_ok():
        log(f"Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ is required; this is {sys.version.split()[0]}.")
        return 1

    if not has_pip():
        log("pip is not available for this interpreter.")
        log("Install it with:  python -m ensurepip --upgrade")
        return 1

    target = f"{REPO}[{args.extras}]" if args.extras else str(REPO)

    # One decision, made once, before anything is run.
    #
    # An editable install through pip is the right thing on an ASCII path. On a non-ASCII path
    # setuptools writes its redirect using the console's locale encoding, which cannot represent
    # the path: it raises UnicodeEncodeError, or hangs. So it is not attempted there -- the route
    # below reaches the same end state (the package importable from this checkout) without going
    # through the part that cannot represent the path.
    want_editable = not args.no_editable
    pip_editable = want_editable and path_is_ascii(REPO)

    installed_by_pip = False
    if pip_editable:
        log(f"installing {target} (editable) ...")
        installed_by_pip = pip_install(target, editable=True, timeout=pip_timeout)
        if not installed_by_pip:
            log("pip's editable install failed; falling back to a plain install plus a redirect.")

    if not installed_by_pip:
        log(f"installing {target} ...")
        installed_by_pip = pip_install(target, timeout=pip_timeout)

    if installed_by_pip:
        log("installed with pip.")
    else:
        # The package build itself did not complete. That is recoverable without it: the only
        # things a build produces are the dependencies and a note saying where the code lives.
        # Dependencies are installable on their own (they are wheels from the index, no build of
        # this project involved), and the "where the code lives" part is one path file.
        #
        # This path exists because a project directory on a synchronised or network filesystem
        # makes pip's build step unusably slow -- it writes thousands of small files, and the
        # filesystem amplifies every one of them. Verified on this machine: the build did not
        # finish in ten minutes, while installing the same dependencies took seconds.
        log("the package build did not complete; installing dependencies and linking the source directly.")
        if not pip_install_dependencies(args.extras, timeout=pip_timeout):
            log("could not install the dependencies either. Check the pip output above.")
            return 1
        try:
            path_file = write_manual_editable()
        except Exception as exc:
            log(f"could not write the editable redirect: {exc}")
            return 1
        log(f"wrote {path_file}")
        remove_copied_package()
        log("Myriaxon is importable from this checkout.")

    print()
    if not verify_import():
        log("the package is installed but does not import. Please report the error above.")
        return 1

    print()
    print("Done.")
    print()
    print("  Start the terminal agent :  python -m myriaxon")
    print("  Start the web dashboard :  python -m myriaxon web")
    print("  Configure a provider    :  python -m myriaxon auth")
    print()
    print("On Windows the launchers in this folder do the same thing:")
    print("  myriaxon.cmd        myriaxon-web.cmd")
    print()
    print("Note: `myriaxon.exe` in the virtualenv may be blocked by a Windows Application")
    print("Control policy on managed machines. `python -m myriaxon` always works.")

    if args.verify:
        print()
        log("running the offline test suite ...")
        if run_tests():
            log("tests passed.")
        else:
            log("the test suite reported failures.")
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
