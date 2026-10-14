"""Check that Harbor can import the Myriaxon adapter exactly as it will at run time.

Written as a file because the equivalent inline command cannot survive the trip through PowerShell
into WSL: inner quotes and parentheses get reinterpreted by PowerShell before bash ever sees them,
which produced `Missing expression after ','` rather than a Python result.

Run:
    .venv-harbor-linux/bin/python tools/check_adapter_import.py
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

print(f"  repo on sys.path : {REPO}")
print(f"  tools is package : {(REPO / 'tools' / '__init__.py').is_file()}")

try:
    from tools.harbor_myriaxon import MyriaxonAgent
except Exception as exc:  # noqa: BLE001
    print(f"  IMPORT FAILED    : {type(exc).__name__}: {exc}")
    raise SystemExit(1)

print(f"  import           : OK")
print(f"  name()           : {MyriaxonAgent.name()}")

try:
    from harbor.agents.installed.base import BaseInstalledAgent
    print(f"  is an installed agent: {issubclass(MyriaxonAgent, BaseInstalledAgent)}")
except Exception as exc:  # noqa: BLE001
    print(f"  harbor check failed  : {type(exc).__name__}: {exc}")
    raise SystemExit(1)

# The wheel is what the adapter uploads into the container, so its absence would fail the install
# step at run time rather than here -- worth catching now.
wheel_dir = REPO / ".uv" / "wheel"
wheels = sorted(wheel_dir.glob("myriaxon-*.whl")) if wheel_dir.is_dir() else []
print(f"  wheel present    : {wheels[0].name if wheels else 'NONE -- install will fail'}")

# Editable installs put the package on the path; a build backend that is missing is the other way
# the install step fails, and it fails deep inside a container log.
try:
    import build  # noqa: F401
    print(f"  build backend    : available")
except Exception:  # noqa: BLE001
    try:
        import setuptools  # noqa: F401
        print(f"  build backend    : setuptools only (editable not needed: a wheel exists)")
    except Exception:  # noqa: BLE001
        print(f"  build backend    : MISSING")
