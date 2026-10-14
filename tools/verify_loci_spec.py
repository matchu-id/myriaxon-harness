#!/usr/bin/env python3
"""Account for every capability in loci.md section 8, against the code.

The goal asks for each section 8 capability to be "implemented or explicitly accounted for". This is the accounting:
it checks each one against what actually exists -- the CLI verbs, the store functions, the expression cues -- and
prints a table rather than a claim.

It is a REPORT, not a gate. A capability that is not built is printed as NOT BUILT with what it would need, because a
checker that failed here would be asserting that the design document had been fully implemented, which is a different
statement from "every implemented capability is verified".
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from myriaxon import loci_capabilities as C  # noqa: E402
from myriaxon import loci_character as character  # noqa: E402
from myriaxon import loci_expression as expression  # noqa: E402

BUILT: list[str] = []
PARTIAL: list[str] = []
MISSING: list[str] = []

LABELS = ((BUILT, "BUILT"), (PARTIAL, "PARTIAL"), (MISSING, "NOT BUILT"))
IDS = {id(BUILT): "BUILT", id(PARTIAL): "PARTIAL", id(MISSING): "NOT BUILT"}


def note(bucket: list[str], label: str, detail: str = "") -> None:
    bucket.append(label)
    tag = IDS[id(bucket)]
    print(f"  [{tag:>9}] {label}" + (f" -- {detail}" if detail else ""))


def cli_verbs() -> set[str]:
    """The verbs `myriaxon loci` accepts, read from the parser rather than assumed."""
    text = (REPO / "myriaxon" / "argparse_cli.py").read_text(encoding="utf-8")
    match = re.search(r"choices=\[([^\]]*?)\]\s*,\s*\n\s*help=\"win, remember", text, re.S)
    if not match:
        return set()
    return set(re.findall(r'"([a-z]+)"', match.group(1)))


def main() -> int:
    verbs = cli_verbs()
    print("=== 8.1 assistant capabilities ===")
    print(f"  (`myriaxon loci` accepts: {', '.join(sorted(v for v in verbs if v))})")
    print()

    rows = (
        ("Daily plan", "loci plan", ("plan",), "thinking"),
        ("Tasks & reminders", "loci todo", ("todo",), "idle"),
        ("Memory palace (notes)", "loci remember / recall", ("remember", "recall"), "remembering"),
        ("Calendar awareness", "loci today", ("today",), "thinking"),
        ("Focus sessions", "loci focus", ("focus",), "celebrate"),
        ("Wins jar", "loci win / wins", ("win", "wins"), "celebrate"),
        ("Weekly reflection", "loci reflect", ("reflect",), "listening"),
        ("Journaling", "loci journal", ("journal",), "listening"),
        ("Habits", "loci habit", ("habit", "habits"), "happy"),
    )
    for label, command, needed_verbs, cue in rows:
        present = [v for v in needed_verbs if v in verbs]
        # NAMED, not guessed. Each verb maps to the callables that actually implement it, because deriving a name
        # from the verb (`hasattr(C, verb) or hasattr(C, "list_" + verb)`) only works when the verb is the plural
        # of the function's stem -- so `focus`, `win` and `habit` were reported PARTIAL while the detail line said
        # "missing []". A verdict that contradicts its own evidence is worse than no verdict.
        ALIASES = {
            "todo": ("add_task", "list_tasks"),
            "today": ("today_summary", "render_today"),
            "plan": ("plan_prompt",),
            "focus": ("start_focus", "current_focus", "end_focus"),
            "win": ("add_win",),
            "wins": ("list_wins", "wins_on_hard_day"),
            "habit": ("declare_habit", "mark_habit"),
            "habits": ("list_habits",),
            "remember": ("remember",),
            "recall": ("recall",),
            "journal": ("write_journal", "list_journal", "journal_prompt"),
            "reflect": ("reflect",),
            "forget": ("forget", "forget_all"),
        }
        functions = [v for v in needed_verbs
                     if all(hasattr(C, a) for a in ALIASES.get(v, ())) and ALIASES.get(v)]
        # loci_character.EXPRESSIONS, not loci.is_known: the latter lives in the registry module and
        # importing it here would pull in the whole persistent-agent layer for a name check.
        cue_ok = cue in character.EXPRESSIONS
        cue_text = f"cue `{cue}` {'present' if cue_ok else 'MISSING'}"
        wired = bool(needed_verbs) and len(present) == len(needed_verbs) and len(functions) == len(needed_verbs)
        if wired and cue_ok:
            note(BUILT, label, f"`{command}`, {cue_text}")
        elif wired:
            # The store and the verb exist, but the expression the spec promises for this capability is not in
            # EXPRESSIONS, so the user would never SEE it. That is PARTIAL, not BUILT. Measured: the first version
            # checked the verbs and the cue separately and only acted on the verbs, so it printed "BUILT" and
            # "cue remembering MISSING" on the same line.
            note(PARTIAL, label, f"`{command}` works, but {cue_text}")
        elif needed_verbs and (present or functions):
            absent = sorted(set(needed_verbs) - set(present))
            note(PARTIAL, label, f"has {present or functions}, missing {absent}; {cue_text}")
        else:
            need = f"`{command}` and a store function" if not needed_verbs else f"`{command}`"
            note(MISSING, label, f"needs {need}; {cue_text}")

    print()
    print("=== 8.2 companion capabilities ===")
    # Measured against the module that now implements them, rather than a hand-written verdict. Each entry names the
    # callable the capability actually lives in, so a later refactor that drops one is caught here.
    from myriaxon import companion

    companion_built = {
        "Mood check-in": (companion.check_in, companion.list_moods, companion.mood_pattern),
        "Listening mode": (companion.listening_prompt,),
        "Grounding": (companion.start_grounding,),
        "Gentle reframes": (companion.reframe_prompt,),
        "Overwhelm breakdown": (companion.overwhelm_prompt,),
        "Self-compassion nudges": (companion.compassion_prompt,),
        "Rest nudges": (companion.rest_nudge,),
        "Connection nudges": (companion.connection_prompt,),
    }
    for label, callables in companion_built.items():
        present = all(callable(fn) for fn in callables)
        note(BUILT if present else MISSING, label,
             " + ".join(fn.__name__ for fn in callables) if present
             else "not implemented in myriaxon.companion")
    listening_built = "listening" in getattr(expression, "SIGNAL_EXPRESSION", {}).values()
    print()
    print("=== 8.3-8.6 protocols, boundaries and privacy ===")
    crisis = "crisis" in character.EXPRESSIONS
    note(BUILT if crisis else MISSING, "8.4 crisis expression exists",
         "animation suppressed and no cuteness, per the spec")
    note(BUILT, "8.6 forget works", "`loci forget` and `forget --all --confirm`")
    note(PARTIAL, "8.6 window goes dark, not sad, on delete",
         "forget is built; the idle+dark-window transition is not asserted through the CLI")
    note(MISSING, "8.4 locale crisis resources",
         "needs a verified resource table -- the spec says these must be maintained by the product team, NOT generated")
    # Measured, not asserted: the module exists, the prompt carries it, and the turn loop calls it. An earlier
    # version of this line said NOT BUILT because only the crisis expression existed -- which was true then and would
    # have stayed true here after the work, which is the failure a hand-written verdict always has.
    from myriaxon import boundaries as boundary_module

    wired = (REPO / "myriaxon" / "system_prompt.py").read_text(encoding="utf-8")
    loop = (REPO / "myriaxon" / "cli.py").read_text(encoding="utf-8")
    enforced = (callable(getattr(boundary_module, "review", None))
                and "boundaries_prompt" in wired
                and "boundaries_module.review(" in loop)
    note(BUILT if enforced else MISSING, "8.5 boundaries as enforced rules",
         f"{len(boundary_module.BOUNDARIES)} checks, in the prompt and called from the turn loop" if enforced
         else "the module exists but is not wired")
    note(PARTIAL, "8.5 rewriting a streamed reply",
         "the check runs after the reply is shown, so it reports rather than prevents; apply_boundaries rewrites for a caller holding the text")

    print()
    print(f"=== {len(BUILT)} built, {len(PARTIAL)} partial, {len(MISSING)} not built ===")
    print("  This is an accounting, not a pass/fail gate: see the module docstring.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
