"""Harbor entry point that makes its temporary-directory handling work under this sandbox.

Harbor stages each downloaded task inside a `tempfile.TemporaryDirectory` and removes it on teardown.
Both halves of that trip over this filesystem sandbox:

  * `tempfile._resetperms` runs `os.chmod(path, 0o700)` on every entry -- a metadata change that is
    denied, so cleanup raised `PermissionError: [WinError 5]` on a path inside Harbor's own temp dir.
  * `shutil.rmtree` then fails at `os.scandir` on the same tree, for the same reason.

Neither is a bug in Harbor: on an ordinary filesystem both succeed. The download itself had already
completed both times -- it was the tidy-up that threw, which is why the failure appeared after the
content was fetched.

Both patches are narrow, and neither is a blanket "ignore permission errors":

  * `_resetperms` is a no-op only when the path is already writable, and re-raises otherwise.
  * temp cleanup is best-effort, and reports nothing when it cannot remove the tree. A leftover
    directory under `.uv/tmp` is harmless; a failed download is not. `shutil.rmtree` keeps its normal
    behaviour everywhere else in the process.

Run Harbor through this module rather than the console script:

    .venv-harbor\\Scripts\\python.exe tools/harbor_main.py download terminal-bench@2.0 --export -o <dir>
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path


def _patch_resetperms() -> bool:
    original = getattr(tempfile, "_resetperms", None)
    if original is None:
        return False

    def safe_resetperms(path, *args, **kwargs):
        try:
            return original(path, *args, **kwargs)
        except (PermissionError, OSError):
            if os.access(path, os.W_OK):
                return None
            raise

    tempfile._resetperms = safe_resetperms
    return True


def _patch_tempdir_cleanup() -> bool:
    """Make `TemporaryDirectory.cleanup()` best-effort.

    `ignore_cleanup_errors` exists on 3.10+ but Harbor does not pass it, and the flag only covers
    `OSError` on the final rmtree -- the `_resetperms` pass raises before that, which the first patch
    handles. This covers the second half.
    """
    original = tempfile.TemporaryDirectory.cleanup

    def tolerant_cleanup(self):
        try:
            return original(self)
        except (PermissionError, OSError):
            # Left behind under our own temp root, which is disposable by construction.
            return None

    tempfile.TemporaryDirectory.cleanup = tolerant_cleanup
    return True


def _redirect_home(here: Path) -> Path:
    """Point home at a workspace directory before Harbor imports anything.

    Harbor resolves two paths from `~`: its task cache (`~/.cache/harbor`) and its credentials
    (`~/.harbor`). Neither is configurable -- both are literal `Path("~/...").expanduser()` calls in
    `harbor/constants.py` -- so on a machine where the real user profile is not writable there is no
    flag to point at. Verified: `Path.home()` follows `USERPROFILE` and `HOME` on Windows, so setting
    both before the import moves both paths at once.

    This must happen BEFORE `harbor` is imported, because the constants are evaluated at module load.
    """
    home = here / ".uv" / "home"
    (home / ".cache" / "harbor").mkdir(parents=True, exist_ok=True)
    (home / ".harbor").mkdir(parents=True, exist_ok=True)
    os.environ["USERPROFILE"] = str(home)
    os.environ["HOME"] = str(home)
    return home


def main() -> int:
    reset = _patch_resetperms()
    cleanup = _patch_tempdir_cleanup()

    here = Path(__file__).resolve().parent.parent
    home = _redirect_home(here)

    # A workspace temp directory: the OS default sits under the user profile, which is also denied.
    workspace_tmp = here / ".uv" / "tmp"
    workspace_tmp.mkdir(parents=True, exist_ok=True)
    for var in ("TEMP", "TMP", "TMPDIR"):
        os.environ[var] = str(workspace_tmp)
    tempfile.tempdir = str(workspace_tmp)

    if os.environ.get("MYRIAXON_HARBOR_VERBOSE"):
        print(f"  tempfile._resetperms patched : {reset}")
        print(f"  TemporaryDirectory.cleanup   : {cleanup}")
        print(f"  temp dir                     : {workspace_tmp}")
        print(f"  home                         : {home}")
        print(f"  harbor cache                 : {home / '.cache' / 'harbor'}")

    from harbor.cli.main import app  # type: ignore

    app()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ImportError as exc:
        print(f"  could not import harbor.cli.main: {exc}", file=sys.stderr)
        print("  run with: .venv-harbor\\Scripts\\python.exe tools/harbor_main.py ...", file=sys.stderr)
        raise SystemExit(2)
