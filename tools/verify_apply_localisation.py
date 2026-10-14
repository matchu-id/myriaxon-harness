#!/usr/bin/env python3
"""Prove `apply_localisation.py` reproduces the one existing translated file byte-for-byte.

`ai-ml-engineer/levels/01.id.json` was produced by a one-off generator. The generalised tool is about to
produce sixty more files, so it has to be shown to agree with the one whose output a human already reviewed.
Agreement is checked at the byte level, not structurally: the key order, the indentation and the trailing
newline all have to match, because a difference in any of them would make the remaining files inconsistent
with the reviewed one in ways nobody would notice.

The translations are read out of the original generator rather than retyped, so this tests the merge and not
anyone's ability to copy 140 strings correctly.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(r"C:\Users\arian\OneDrive\ドキュメント\Myriaxon")
GENERATOR = ROOT / "tools" / "localise_ai_ml_engineer_level01.py"
LEVEL = ROOT / "certivo-rework" / "ai-ml-engineer" / "levels" / "01.json"
REVIEWED = ROOT / "certivo-rework" / "ai-ml-engineer" / "levels" / "01.id.json"
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"


def load_translations() -> dict[str, str]:
    """Import the one-off generator and flatten its tuple keys into dotted paths."""
    spec = importlib.util.spec_from_file_location("generator", GENERATOR)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)

    flat: dict[str, str] = {}
    for key, value in module.TRANSLATIONS.items():
        flat[".".join(str(part) for part in key)] = value
    return flat


def main() -> int:
    if not REVIEWED.exists():
        print("  the reviewed file is missing; nothing to compare against")
        return 1

    translations = load_translations()
    print(f"  read {len(translations)} translation(s) from {GENERATOR.name}")

    with tempfile.TemporaryDirectory() as workdir:
        scratch = Path(workdir) / "01.json"
        scratch.write_text(LEVEL.read_text(encoding="utf-8"), encoding="utf-8", newline="\n")
        map_file = Path(workdir) / "translations.json"
        map_file.write_text(json.dumps(translations, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")

        run = subprocess.run(
            [str(PYTHON), str(ROOT / "tools" / "apply_localisation.py"), str(scratch), str(map_file), "--write"],
            capture_output=True, text=True, encoding="utf-8", timeout=180,
        )
        for line in (run.stdout or "").splitlines():
            print(f"  {line.strip()}")
        if run.returncode != 0:
            print(f"  tool failed: {(run.stderr or '').strip()[:300]}")
            return 1

        produced = Path(workdir) / "01.id.json"
        if not produced.exists():
            print("  the tool wrote nothing")
            return 1

        expected_bytes = REVIEWED.read_bytes()
        actual_bytes = produced.read_bytes()
        if expected_bytes == actual_bytes:
            print(f"  byte-identical to the reviewed file ({len(expected_bytes)} bytes)")
            return 0

        # Report where they diverge rather than only that they do.
        print(f"  DIFFERS: reviewed {len(expected_bytes)} bytes, produced {len(actual_bytes)} bytes")
        expected_json = json.loads(expected_bytes.decode("utf-8"))
        actual_json = json.loads(actual_bytes.decode("utf-8"))
        if expected_json == actual_json:
            print("  the JSON content is the same; the difference is formatting only")
            for index, (a, b) in enumerate(zip(expected_bytes.splitlines(), actual_bytes.splitlines()), start=1):
                if a != b:
                    print(f"    first differing line {index}:")
                    print(f"      reviewed: {a[:120]!r}")
                    print(f"      produced: {b[:120]!r}")
                    break
        else:
            print("  the JSON content differs as well")
        return 1


if __name__ == "__main__":
    sys.exit(main())
