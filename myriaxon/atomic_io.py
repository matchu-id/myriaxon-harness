import os
import tempfile
import time
from pathlib import Path

# How long to keep retrying the final rename when the target is momentarily locked.
#
# Found by chasing an intermittent test failure rather than by reasoning: one full-suite run failed
# with `Permission denied: ... loci.json`, then passed on every re-run and in six repeats of the
# same file. The cause is Windows sharing semantics -- `os.replace` onto a path another process
# currently has open raises PermissionError (WinError 32, "being used by another process") even
# though the open is only for reading. On POSIX the rename simply succeeds, so this is Windows-only
# and invisible to a POSIX test run.
#
# The consequence was worse than the message suggested. `LocusRegistry.save` catches OSError and
# logs a warning, so a lost rename meant THE CHANGE WAS SILENTLY DISCARDED -- a goal update or a
# status change that the caller believes was persisted and was not. Confirmed as a live hazard: two
# concurrent `cowork` runs were writing this file when the failure appeared.
#
# A bounded retry is the standard remedy because the contention window is milliseconds (the other
# process is mid-read, not holding a lock for the session). 10 attempts over ~0.28s turns a lost
# write into a delayed one; the alternative is a lock file, which would add a second failure mode
# (stale locks after a crash) to solve a milliseconds-long race.
_REPLACE_ATTEMPTS = 10
_REPLACE_BACKOFF_SECONDS = 0.01


def _replace_with_retry(tmp_name: str, path: Path) -> None:
    """`os.replace`, retrying while the target is transiently locked. Re-raises anything else."""
    last: OSError | None = None
    for attempt in range(_REPLACE_ATTEMPTS):
        try:
            os.replace(tmp_name, path)
            return
        except PermissionError as exc:  # Windows: the target is open in another process
            last = exc
            if attempt < _REPLACE_ATTEMPTS - 1:
                time.sleep(_REPLACE_BACKOFF_SECONDS * (attempt + 1))
    raise last if last is not None else PermissionError(f"could not replace {path}")


def atomic_write_text(path: Path, content: str, encoding: str = "utf-8") -> None:
    """Writes content to path without ever leaving a half-written/corrupted file behind on a
    crash or power loss mid-write -- confirmed real gap: config.json/settings.json/session.json
    were all previously written via a plain path.write_text(...), a genuine non-atomic
    read-then-overwrite. Writes to a temp file in the SAME directory as the target (required for
    the final rename to be atomic -- a temp file on a different filesystem/volume would make
    os.replace fall back to a non-atomic copy+delete) then atomically renames it into place;
    os.replace() is atomic on both POSIX and Windows (MoveFileExW with
    MOVEFILE_REPLACE_EXISTING).

    The rename retries briefly on Windows contention; see `_REPLACE_ATTEMPTS`.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding=encoding) as f:
            f.write(content)
        _replace_with_retry(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
