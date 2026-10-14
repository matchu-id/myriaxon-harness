"""Declared uncertainty: separating what was CHECKED from what was REASONED.

Level 5 requirement 5.5, and the one requirement in this set that cannot be fully built. This module is the
honest half of it.

THE CEILING, STATED FIRST

No harness can make a model feel doubt. A model that is wrong with confidence produces text indistinguishable
from a model that is right with confidence, and no amount of machinery inside the harness reaches the process
that generated it. Six differently-worded wrong answers look like progress to any textual comparison -- that
limit is asserted in `test_agent_discipline.py` so nobody tries to fix it by lowering a threshold.

WHAT A HARNESS *CAN* DO, AND WHAT THIS DOES

It can refuse to let `verified` and `assumed` be the same word. A model reporting a conclusion will write both
in the same register: "the global endpoint is the bare host" reads identically whether it was run and observed
or inferred from a doc. If the distinction is never recorded, a reader cannot recover it -- and neither can the
model on its next turn.

So every claim carries a BASIS:

    CHECKED  something was run or read, and the result is reported. Names what was run.
    INFERRED derived by reasoning from something else. The reasoning is named.
    ASSUMED  not established at all. The reason for proceeding anyway is named.

`ASSUMED` is not a failure and is not discouraged. An agent that cannot proceed without certainty cannot
proceed, and one that pretends to have checked is worse than one that says it has not. The value is that a
reader -- a person, or the agent on a later turn -- can tell which of the three it is holding.

WHY THIS IS NOT AUTOMATIC

The harness cannot tell which basis is true. Nothing in the text reveals whether a command was run, and a
mechanism that guessed would be inventing the very distinction it exists to preserve. So the agent declares it,
and the harness does what it can with what it is given: it records the declaration, refuses to accept an empty
one, and makes the unverified claims visible in one place.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .atomic_io import atomic_write_text

# The three bases. `CHECKED` is the only one that claims observation, and the only one a reader may treat as
# evidence.
CHECKED = "checked"
INFERRED = "inferred"
ASSUMED = "assumed"
BASES = (CHECKED, INFERRED, ASSUMED)

# How many claims to keep per run. Bounded because this is a working record, not an audit trail.
MAX_CLAIMS = 200
STORE_FILENAME = "uncertainty.jsonl"


def _store_path(root: Path | str | None = None) -> Path:
    if root is not None:
        return Path(root) / STORE_FILENAME
    from .constants import CONFIG_DIR

    return Path(CONFIG_DIR) / STORE_FILENAME


@dataclass
class Claim:
    """One thing the agent is asserting, and how it knows."""

    id: str = ""
    at: float = 0.0
    run_id: str = ""
    text: str = ""
    basis: str = ASSUMED
    # What was run, read, or reasoned from. Required for CHECKED and INFERRED: a claim that something was
    # verified without saying what was verified is not a check, it is an assertion with a label on it.
    evidence: str = ""
    # Set when an ASSUMED claim could be settled by something specific and cheap. This is the part that makes
    # the record actionable rather than merely honest.
    would_confirm: str = ""

    @property
    def is_evidence(self) -> bool:
        return self.basis == CHECKED

    @property
    def unverified(self) -> bool:
        return self.basis != CHECKED


def declare(text: str, basis: str = ASSUMED, *, evidence: str = "", would_confirm: str = "",
            run_id: str = "", root: Path | str | None = None) -> tuple[Claim | None, str]:
    """Record what is being claimed and how it is known. Returns (claim, problem)."""
    cleaned = " ".join((text or "").split()).strip()
    if not cleaned:
        return None, "nothing declared: the claim is empty"
    if basis not in BASES:
        return None, f"basis must be one of {', '.join(BASES)} (got {basis!r})"
    proof = " ".join((evidence or "").split()).strip()
    if basis in (CHECKED, INFERRED) and not proof:
        # The refusal that keeps the label meaningful. "Checked" with nothing named is an assertion wearing
        # the word, and accepting it would make the whole distinction worthless.
        what = "what was run or read" if basis == CHECKED else "what it was inferred from"
        return None, (f"a {basis} claim must name {what}. Without that, `{basis}` is an assertion with a "
                      f"label on it -- use basis='assumed' if it was not established.")

    claim = Claim(
        id=f"uc-{int(time.time() * 1000):x}",
        at=time.time(),
        run_id=(run_id or "").strip()[:80],
        text=cleaned[:2000],
        basis=basis,
        evidence=proof[:600],
        would_confirm=" ".join((would_confirm or "").split())[:400],
    )
    try:
        existing = load(root=root)
        existing.append(claim)
        atomic_write_text(
            _store_path(root),
            "".join(json.dumps(asdict(c), ensure_ascii=False) + "\n" for c in existing[-MAX_CLAIMS:]),
        )
    except OSError:
        # Losing a declaration must not fail the work it was about.
        pass
    return claim, ""


def load(*, run_id: str = "", root: Path | str | None = None) -> list[Claim]:
    """Read declarations, newest first unless a run is named."""
    out: list[Claim] = []
    try:
        raw = _store_path(root).read_text(encoding="utf-8")
    except OSError:
        return out
    known = {f for f in Claim.__dataclass_fields__}
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except ValueError:
            continue  # a torn line must not cost the others
        if isinstance(data, dict):
            out.append(Claim(**{k: v for k, v in data.items() if k in known}))
    if run_id:
        out = [c for c in out if c.run_id == run_id]
        return out
    return list(reversed(out))


def summary(*, run_id: str = "", root: Path | str | None = None) -> dict:
    """Counts by basis. The number that matters is `unverified`."""
    claims = load(run_id=run_id, root=root)
    by_basis: dict[str, int] = {}
    for claim in claims:
        by_basis[claim.basis] = by_basis.get(claim.basis, 0) + 1
    return {
        "total": len(claims),
        "by_basis": by_basis,
        "checked": by_basis.get(CHECKED, 0),
        "unverified": sum(1 for c in claims if c.unverified),
    }


def format_declaration(claim: Claim) -> str:
    """One line, so a reader can scan for the unverified ones."""
    mark = {"checked": "verified", "inferred": "inferred", "assumed": "ASSUMED"}[claim.basis]
    line = f"[{mark}] {claim.text}"
    if claim.evidence:
        line += f"  ({claim.evidence})"
    if claim.would_confirm:
        line += f"  -- would be settled by: {claim.would_confirm}"
    return line


def format_report(*, run_id: str = "", root: Path | str | None = None) -> str:
    """What is settled and what is not, with the unverified ones first.

    Ordered that way deliberately: a reader scanning a report is looking for what ISN'T established, and
    burying it under a list of successful checks is how it gets missed.
    """
    claims = load(run_id=run_id, root=root)
    if not claims:
        return "Nothing declared: no claim has been recorded with its basis yet."

    data = summary(run_id=run_id, root=root)
    lines = [
        f"{data['total']} claim(s) declared: {data['checked']} verified, "
        f"{data['unverified']} not verified."
    ]
    unverified = [c for c in claims if c.unverified]
    if unverified:
        lines.append("")
        lines.append("NOT VERIFIED -- treat these as provisional:")
        for claim in unverified:
            lines.append(f"  {format_declaration(claim)}")
    verified = [c for c in claims if c.is_evidence]
    if verified:
        lines.append("")
        lines.append("Verified:")
        for claim in verified:
            lines.append(f"  {format_declaration(claim)}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------------------------
# Agent-facing tool
# ---------------------------------------------------------------------------------------------

DECLARE_UNCERTAINTY_TOOL_DEF = {
    "name": "declare_uncertainty",
    "description": (
        "Record something you are asserting AND how you know it, so a reader can tell a verified fact from a "
        "reasonable guess. Use basis='checked' when you ran or read something and are reporting the result, "
        "naming what you ran; 'inferred' when you reasoned it out, naming what from; 'assumed' when you did "
        "not establish it. Being wrong about which one it is makes the record worse than useless, and "
        "'assumed' is a perfectly good answer -- an agent that will not proceed without certainty cannot "
        "proceed."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "What you are claiming."},
            "basis": {"type": "string", "enum": list(BASES),
                      "description": "checked, inferred, or assumed."},
            "evidence": {"type": "string",
                         "description": "What was run/read (checked) or what it was inferred from."},
            "would_confirm": {"type": "string",
                              "description": "What would settle it, if anything cheap would."},
        },
        "required": ["text", "basis"],
    },
}


def execute_declare_uncertainty(tool_input: dict, *, run_id: str = "") -> tuple[str, bool]:
    claim, problem = declare(
        str(tool_input.get("text") or ""),
        str(tool_input.get("basis") or ASSUMED),
        evidence=str(tool_input.get("evidence") or ""),
        would_confirm=str(tool_input.get("would_confirm") or ""),
        run_id=run_id,
    )
    if claim is None:
        return problem, True
    return f"Recorded as {claim.basis}: {claim.text[:200]}", False


# The tool-registration machinery expects a tuple of defs, and this group has exactly one. Defined here rather
# than wrapped at the registration site so the module stays the single place that knows its own surface --
# the same reason the other modules name their tuples.
DECLARE_UNCERTAINTY_TOOL_DEFS = (DECLARE_UNCERTAINTY_TOOL_DEF,)
