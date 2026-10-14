import os
import sys
from pathlib import Path

# Where to look for the fallback .env, in order.
#
# Measured, and the reason this is a search rather than one hard-coded path: `__file__` is
# `<install>/.venv/Lib/site-packages/myriaxon/dotenv_support.py` for an installed copy, so
# `parent.parent` is `site-packages` -- where nothing a user would ever write lives. A deployment
# whose `.env` sat beside its source was simply not read, and Myriaxon fell back to the interactive
# provider prompt as though it had never been configured.
#
# A source checkout is unaffected: the first candidate is still the repo root.
_PACKAGE_DIR = Path(__file__).resolve().parent


def _env_candidates() -> list[Path]:
    candidates = [
        # A source checkout: myriaxon/dotenv_support.py -> myriaxon/ -> repo root.
        _PACKAGE_DIR.parent / ".env",
        # The directory the interpreter lives in, and the one above it. For a virtualenv at
        # `<deploy>/.venv` this resolves to `<deploy>/.env` -- the placement a user actually chooses.
        Path(sys.prefix) / ".env",
        Path(sys.prefix).parent / ".env",
    ]
    seen: list[Path] = []
    for candidate in candidates:
        if candidate not in seen:
            seen.append(candidate)
    return seen


def _load_one(file_path: Path) -> None:
    if not file_path.is_file():
        return
    try:
        lines = file_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


def load_dotenv_into_environ(path: str | Path = ".env") -> None:
    """Minimal .env loader -- KEY=value per line, '#' comments and blank lines ignored, quotes
    around the value stripped. Never overrides an already-set environment variable, so whichever
    file is loaded first wins for a given key.

    Confirmed live this was a real bug: a bare ".env" default resolves against the process's
    *current working directory*, not the package's own location -- but Myriaxon is meant to be
    launched from any project directory (that's the entire point of a general-purpose coding
    CLI), so `.env` was silently never found unless you happened to launch from inside the
    myriaxon source checkout itself. This left the whole DEEPSEEK_API_KEY/GEMINI_API_KEY/
    OPENROUTER_API_KEY .env-fallback feature broken for real usage.

    Now checks CWD's .env FIRST (so a per-project .env can still override/add keys for that
    project), then each candidate install-location .env in turn for anything CWD's file did not
    set. Because a key already present in the environment is never overwritten, the first file to
    define a key wins and the order above is the precedence order.
    """
    _load_one(Path(path))
    for candidate in _env_candidates():
        _load_one(candidate)
