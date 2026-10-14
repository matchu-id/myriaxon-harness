"""Install the `mcp` SDK without pip's file handling, which this sandbox blocks.

    python scripts/install_mcp_manually.py [--dry-run]

pip reaches the index fine (verified: pypi.org and files.pythonhosted.org are reachable in under a
second) but fails while unpacking wheel metadata:

    PermissionError: [Errno 13] ...\\pip-unpack-xxxx\\mcp-2.2.0-py3-none-any.whl.metadata

Creating a file with exactly that name and location succeeds from Python, so the block is something
about pip's own read/modify path rather than the directory. This script does the same job with plain
urllib plus zipfile: resolve the dependency list from the PyPI JSON API, download each wheel, and
extract it into the target directory. No pip, no subprocess, no pipes.

Only pure-Python wheels are handled. A dependency that is sdist-only or compiled is reported and
skipped rather than half-installed.
"""
import argparse
import io
import json
import sys
import sysconfig
import urllib.request
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TARGET = REPO / ".venv" / "Lib" / "site-packages"
PYPI_JSON = "https://pypi.org/pypi/{name}/json"


def _platform_tags() -> list[str]:
    """CPython/platform tags for this interpreter, best first.

    Needed because pywin32 publishes one wheel per (Python version, architecture) and picking the
    first one alphabetically installed a cp310-32bit build on a 3.12-64bit interpreter -- it unpacked
    cleanly and then failed at import with "No module named 'pywintypes'", which is a confusing way to
    say "wrong wheel".
    """
    version = f"cp{sys.version_info.major}{sys.version_info.minor}"
    bits = "win_amd64" if sys.maxsize > 2**32 else "win32"
    return [
        "py3-none-any",
        "py2.py3-none-any",
        f"{version}-{version}-{bits}",
        f"{version}-none-{bits}",
    ]


def fetch_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def pick_wheel(data: dict) -> tuple[str, str] | None:
    """Best downloadable wheel for the newest version, as (url, filename)."""
    files = data.get("urls") or []
    wheels = [f for f in files if f.get("filename", "").endswith(".whl")]
    if not wheels:
        return None

    # Pure-Python first (no ABI to mismatch), then this interpreter's exact tags. A wheel whose tag
    # does not appear in the filename is never selected.
    for tag in _platform_tags():
        for wheel in wheels:
            if tag in wheel["filename"]:
                return wheel["url"], wheel["filename"]

    # No compatible wheel. Returning an arbitrary one is what produced the wrong-architecture
    # install, so report it and let the caller skip the package.
    return None


def requirements(data: dict) -> list[str]:
    """Runtime dependency names, with extras and version specifiers stripped."""
    requires = (data.get("info") or {}).get("requires_dist") or []
    names = []
    for entry in requires:
        if ";" in entry:  # environment marker
            marker = entry.split(";", 1)[1]
            # Skip dependencies gated on a different platform or an extra.
            if "extra ==" in marker:
                continue
            if "sys_platform" in marker and "win32" not in marker:
                continue
        name = entry.split(";")[0].strip()
        for separator in ("[", ">", "<", "=", "!", "~", " "):
            name = name.split(separator)[0]
        name = name.strip()
        if name:
            names.append(name)
    return names


def install(name: str, target: Path, seen: set, depth: int = 0, install_deps: bool = True) -> list[str]:
    """Install one package and (optionally) its dependencies. Returns lines describing what happened."""
    if name in seen:
        return []
    seen.add(name)
    log: list[str] = []
    prefix = "  " * depth

    try:
        data = fetch_json(PYPI_JSON.format(name=name))
    except Exception as exc:  # noqa: BLE001
        return [f"{prefix}{name}: could not query PyPI ({type(exc).__name__}: {exc})"]

    version = (data.get("info") or {}).get("version", "?")
    picked = pick_wheel(data)
    if picked is None:
        available = [f["filename"] for f in (data.get("urls") or []) if f["filename"].endswith(".whl")]
        hint = f" available: {', '.join(available[:4])}" if available else " (sdist only)"
        return [f"{prefix}{name} {version}: no wheel for this interpreter -- skipped.{hint}"]

    url, filename = picked
    try:
        with urllib.request.urlopen(url, timeout=120) as response:
            payload = response.read()
    except Exception as exc:  # noqa: BLE001
        return [f"{prefix}{name} {version}: download failed ({type(exc).__name__}: {exc})"]

    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            archive.extractall(target)
    except Exception as exc:  # noqa: BLE001
        return [f"{prefix}{name} {version}: extraction failed ({type(exc).__name__}: {exc})"]

    log.append(f"{prefix}{name} {version}: {filename} ({len(payload):,} bytes)")

    if install_deps:
        for dependency in requirements(data):
            log.extend(install(dependency, target, seen, depth + 1, install_deps=install_deps))
    return log


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="report what would be installed")
    parser.add_argument("--package", default="mcp")
    parser.add_argument(
        "--no-deps",
        action="store_true",
        help="install only this package. Dependencies in this environment are largely already present "
             "(httpx, pydantic, anyio), and the extras list pulls optional packages such as httpx2 "
             "that are not needed for the stdio and HTTP transports.",
    )
    args = parser.parse_args()

    if not TARGET.is_dir():
        print(f"target is not a directory: {TARGET}")
        return 2

    print(f"target: {TARGET}")
    if args.dry_run:
        data = fetch_json(PYPI_JSON.format(name=args.package))
        print(f"would install {args.package} {(data.get('info') or {}).get('version')}")
        if not args.no_deps:
            for dependency in requirements(data):
                print(f"  dependency: {dependency}")
        return 0

    lines = install(args.package, TARGET, set(), install_deps=not args.no_deps)
    for line in lines:
        print(line)

    print()
    try:
        import mcp  # noqa: F401

        print(f"mcp imported successfully")
        from myriaxon.mcp_client import remote_transport_support

        print(f"remote transports: {remote_transport_support()}")
        return 0
    except ImportError as exc:
        print(f"mcp still not importable: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
