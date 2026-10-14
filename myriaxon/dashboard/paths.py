"""Resolve a writable directory for dashboard uploads.

Uploads are the dashboard's own scratch data, not something belonging to the user's project, so they
belong in the config directory. But that is not always writable: measured on a managed Windows
machine, `~/.myriaxon` denies both creating a new file and modifying an existing one, and starting the
Harness with no arguments (workspace = the home directory) crashed on startup with
`PermissionError: [WinError 5] ... ~/.myriaxon/uploads`.

The same reasoning that made the config directory fall back automatically applies here, so this is a
chain rather than a single path, tried in order of preference and probed by actually writing:

  1. <config dir>/uploads       the right home for dashboard scratch data
  2. <workspace>/.myriaxon/uploads   the workspace is guaranteed usable, since the agent writes there
  3. <temp>/myriaxon-uploads     last resort, always writable
"""
from __future__ import annotations

import tempfile
import threading
from pathlib import Path


def is_writable(directory: Path) -> bool:
    """True only if a file can actually be created and removed there.

    A real write, not os.access: on Windows os.access(W_OK) consults the ACL and reports True for a
    file that cannot be written but can be deleted, which is the trap that made an earlier diagnosis
    wrong.
    """
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / ".myriaxon_upload_probe"
        probe.write_text("", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return True
    except OSError:
        return False


def resolve_uploads_dir(config_dir: Path, workspace: Path) -> tuple[Path, list[str]]:
    """Return (directory, notes) where notes explain every fallback that was taken."""
    notes: list[str] = []

    preferred = config_dir / "uploads"
    if is_writable(preferred):
        return preferred, notes
    notes.append(f"config directory is not writable; uploads moved ({preferred})")

    in_workspace = workspace / ".myriaxon" / "uploads"
    if is_writable(in_workspace):
        notes.append(f"using {in_workspace}")
        return in_workspace, notes

    fallback = Path(tempfile.gettempdir()) / "myriaxon-uploads"
    if is_writable(fallback):
        notes.append(f"workspace is not writable either; using {fallback}")
        return fallback, notes

    notes.append("no writable location found; uploads will fail")
    return preferred, notes


class UploadStore:
    """Lazily ensures the uploads directory exists, so a read-only config cannot break startup."""

    def __init__(self, config_dir: Path, workspace: Path) -> None:
        self._config_dir = config_dir
        self._workspace = workspace
        self._lock = threading.Lock()
        self._resolved: Path | None = None
        self.notes: list[str] = []

    def path(self) -> Path:
        with self._lock:
            if self._resolved is None:
                self._resolved, self.notes = resolve_uploads_dir(self._config_dir, self._workspace)
            return self._resolved

    def rework(self, workspace: Path) -> None:
        """Re-resolve after the workspace changed."""
        with self._lock:
            self._workspace = workspace
            self._resolved = None
