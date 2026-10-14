"""Compatibility shim. The instruction layer now lives in `myriaxon.instructions`.

This module previously loaded `<root>/MYRIAXON.md`, `<root>/AGENTS.md` and the user-global file, and returned
the assembled text. Three things were wrong with it, all fixed in the replacement:

* nested `AGENTS.md` files were never read, so a per-package instruction file in a monorepo was invisible;
* there was no size bound and no warning, so an oversized file silently degraded adherence on every turn;
* nothing recorded which files had actually loaded, so "the agent ignored my rule" could not be distinguished
  from "the rule was never loaded".

`load_project_instructions` is kept as the same call signature returning the same thing, so existing callers are
unaffected. New code that wants the provenance report should call `myriaxon.instructions.load_instructions`.
"""

from __future__ import annotations

from pathlib import Path

from .instructions import (  # noqa: F401 -- re-exported for callers that import them from here
    CHARS_PER_TOKEN,
    HARD_CHARS,
    RECOMMENDED_CHARS,
    RECOMMENDED_LINES,
    InstructionSet,
    InstructionSource,
    load_instructions,
)

__all__ = ["load_project_instructions", "load_instructions", "InstructionSet", "InstructionSource"]


def load_project_instructions(project_root: Path | None = None) -> str:
    """The assembled instruction text, unchanged in signature and return type.

    Delegates to the new loader so every caller gets nested discovery and the budget report automatically,
    without having to be updated.
    """
    return load_instructions(project_root=project_root).text
