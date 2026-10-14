#!/usr/bin/env python3
"""Test whether the guard accepts a code-only option that is identical to its English source.

The fullstack translator was told the guard treats `unusable: ... (identical to English)` as a failure, and
that is true of the guard as written. But whether that *should* be a failure for an option containing nothing
but backticked code is a separate question, and the answer decides a real authoring rule: if bare code is
rejected, every code-only option needs Indonesian prose bolted on purely to satisfy a checker, which would
make the option text misleading.

This varies one option at a time and reports the guard's exit code, so the rule is established by running the
guard rather than by reasoning about it.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(r"C:\Users\arian\OneDrive\ドキュメント\Myriaxon")
LEVELS = ROOT / "certivo-rework" / "fullstack-developer" / "levels"
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
GUARD = ROOT / "tools" / "guard_localisation.py"


def run_guard(source: Path, translated: Path) -> tuple[int, str]:
    result = subprocess.run(
        [str(PYTHON), str(GUARD), str(source), str(translated)],
        capture_output=True, text=True, encoding="utf-8", timeout=180,
    )
    return result.returncode, result.stdout


def main() -> int:
    source = LEVELS / "02.json"
    translated = LEVELS / "02.id.json"
    original = json.loads(source.read_text(encoding="utf-8"))
    data = json.loads(translated.read_text(encoding="utf-8"))

    q_index, o_index = 2, 0
    english = original["questions"][q_index]["options"][o_index]["text"]
    current = data["questions"][q_index]["options"][o_index]["textId"]

    print(f"  option questions.{q_index}.options.{o_index}")
    print(f"    English  : {english}")
    print(f"    current  : {current}")
    print()

    for label, value in (("current (code + Indonesian comment)", current),
                         ("bare code, identical to English", english)):
        data["questions"][q_index]["options"][o_index]["textId"] = value
        with tempfile.TemporaryDirectory() as workdir:
            scratch = Path(workdir) / "02.id.json"
            scratch.write_text(
                json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8", newline="\n",
            )
            code, output = run_guard(source, scratch)
        print(f"  {label}")
        print(f"    guard exit code: {code}")
        for line in output.splitlines():
            stripped = line.strip()
            if "unusable" in stripped or "identical" in stripped.lower() or "string(s) have" in stripped:
                print(f"      {stripped[:110]}")
        print()

    print("  A guard exit of 1 on bare code means the checker, not the translation, needs the fix.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
