"""Both spellings of --mode must survive parsing, including the subparser one.

A subparser does not inherit the parent's options, so `myriaxon web --mode plan` used to be an
argparse usage error while `myriaxon --mode plan web` worked -- an inconsistency no reader would
guess. Adding the flag to the subparser naively then broke the *global* spelling instead, because a
subparser's default is written over the parent's parsed value; the subparser uses `SUPPRESS` so it
only sets the attribute when the flag is actually given.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from myriaxon.argparse_cli import parse_args  # noqa: E402
from myriaxon.constants import DEFAULT_MODE  # noqa: E402

CASES = [
    (["web", "--mode", "plan"], "plan"),
    (["--mode", "auto", "web"], "auto"),
    (["--mode", "plan", "web"], "plan"),
    (["bot", "--mode", "plan"], "plan"),
    (["--mode", "auto", "bot"], "auto"),
    # The global default, untouched. Read from the constant rather than typed: the first version of
    # this test hardcoded auto_edit and was wrong, because DEFAULT_MODE is `manual`.
    (["web"], DEFAULT_MODE),
    (["bot"], DEFAULT_MODE),
]

failures = 0
for argv, expected in CASES:
    parsed = parse_args(argv)
    actual = getattr(parsed, "mode", DEFAULT_MODE)
    ok = actual == expected
    if not ok:
        failures += 1
    print(f"  {'ok  ' if ok else 'FAIL'}  {' '.join(argv):26} -> {actual} (want {expected})")

print()
print("mode flags verified" if not failures else f"{failures} FAILED")
sys.exit(1 if failures else 0)
