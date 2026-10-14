"""Fetch the external tooling `myriaxon/diagnostics.py` can use, into the gitignored `.tooling/`.

## Why the harness fetches anything at all

`myriaxon/diagnostics.py` runs a post-edit check after every `write_file` and `edit_file`. Its **tier 1** needs
nothing — `ast.parse` and `compile` are standard library, so a bare host still gets syntax checking. **Tier 2** uses
`pyright` for real type diagnostics when it is present, which is what catches a wrong argument type or a missing
attribute.

That makes pyright genuinely part of the harness's value rather than a development convenience, so fetching it is a
harness concern, not a dev-setup one. It lands in `.tooling/`, which is gitignored: it is a 4 MB download,
reproducible from this script, and not something to version.

## Why the download is here and not in PowerShell

Measured on this host: `Invoke-WebRequest` fails against the same URLs with "the underlying connection was closed",
while Python's `urllib` succeeds. So the fetch lives where the TLS stack works.

## Usage

    python tools/fetch_tooling.py            # fetch what is missing
    python tools/fetch_tooling.py --check    # report what is present, fetch nothing

Both are safe to re-run: an already-present tool is left alone.
"""

from __future__ import annotations

import argparse
import io
import json
import shutil
import subprocess
import sys
import tarfile
import urllib.request
from pathlib import Path

ROOT = Path(r"C:\Users\arian\OneDrive\ドキュメント\Myriaxon")
TOOLING = ROOT / ".tooling"

# npm registry packages. Each is fetched as its tarball and unpacked under `.tooling/<name>/`.
NPM_PACKAGES = {
    "pyright": {
        "registry": "https://registry.npmjs.org/pyright/latest",
        "why": "type diagnostics after an edit (diagnostics.py tier 2)",
        "probe": ("package", "index.js"),
        "runs_via": "node",
    },
}


def _get(url: str, timeout: float) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "myriaxon-tooling"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def fetch_npm(name: str, spec: dict, *, check_only: bool) -> bool:
    """Fetch one npm package. Returns whether it is present afterwards."""
    marker = TOOLING / name / Path(*spec["probe"])
    if marker.is_file():
        print(f"  {name:<10} present  ({spec['why']})")
        return True
    if check_only:
        print(f"  {name:<10} MISSING  ({spec['why']})")
        return False

    node = shutil.which("node")
    if node is None:
        print(f"  {name:<10} cannot fetch -- node is not on PATH, and {spec['runs_via']} is needed to run it")
        return False

    try:
        meta = json.loads(_get(spec["registry"], 60))
    except Exception as exc:  # noqa: BLE001
        print(f"  {name:<10} registry lookup failed: {type(exc).__name__}: {str(exc)[:70]}")
        return False

    url = (meta.get("dist") or {}).get("tarball")
    if not url:
        print(f"  {name:<10} no tarball in the registry response")
        return False
    print(f"  {name:<10} fetching {meta.get('version', '?')} ...")

    try:
        payload = _get(url, 300)
    except Exception as exc:  # noqa: BLE001
        print(f"  {name:<10} download failed: {type(exc).__name__}: {str(exc)[:70]}")
        return False

    target = TOOLING / name
    target.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
            # `filter="data"` refuses absolute paths and traversal. This is an archive from the network.
            archive.extractall(target, filter="data")
    except (tarfile.TarError, TypeError) as exc:
        print(f"  {name:<10} not a usable tarball: {exc}")
        return False

    if not marker.is_file():
        print(f"  {name:<10} unpacked but {marker.relative_to(TOOLING)} is missing")
        return False
    print(f"  {name:<10} ready: {marker.relative_to(ROOT)}")
    return True


def report_tiers() -> int:
    """Ask the diagnostics module which tiers it can actually run, rather than duplicating its logic here."""
    sys.path.insert(0, str(ROOT))
    try:
        from myriaxon import diagnostics
    except Exception as exc:  # noqa: BLE001
        print(f"  could not import diagnostics: {exc}")
        return 1
    print()
    print("  diagnostics tiers available:")
    for tier in diagnostics.available_tiers():
        print(f"    {tier}")
    usable, entry, note = diagnostics._probe_checker("pyright")
    print()
    if usable:
        print(f"  pyright entry that PASSED the probe: {Path(entry).relative_to(ROOT)}")
    else:
        print(f"  pyright not usable: {note}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch optional external tooling into .tooling/")
    parser.add_argument("--check", action="store_true", help="Report what is present; fetch nothing.")
    args = parser.parse_args()

    print(f"  target: {TOOLING.relative_to(ROOT)}  (gitignored)")
    print()
    results = {name: fetch_npm(name, spec, check_only=args.check)
               for name, spec in NPM_PACKAGES.items()}
    missing = [name for name, ok in results.items() if not ok]
    if missing and not args.check:
        print()
        print(f"  {len(missing)} tool(s) unavailable: {', '.join(missing)}")
        print("  The harness still works: diagnostics fall back to the standard-library tier, and it says so.")
    return report_tiers()


if __name__ == "__main__":
    raise SystemExit(main())
