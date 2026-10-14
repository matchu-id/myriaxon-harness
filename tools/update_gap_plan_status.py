"""Update docs/GAP-PLAN.md status markers for the work completed this session.

Replaces the old status line inside each P1 section rather than prepending a new one, so the document does not
carry two contradictory statements about the same item -- which is the exact class of problem the plan exists to
avoid.
"""

import re
from pathlib import Path

PLAN = Path(r"C:\Users\arian\OneDrive\ドキュメント\Myriaxon\docs\GAP-PLAN.md")

UPDATES = {
    "### P1.1 — Trigger state persisted across evaluations": (
        "**Status: ✅ DONE.** `TriggerState` in `myriaxon/conditions.py`, owned by `Dot.trigger_state`, staged by "
        "`is_due` and committed by `Dot.commit_trigger_state`. The `changed` kind compares an `observe` command's "
        "output against the previous observation. A **failed run does not persist the new state**, so the change "
        "can fire again. 16 KB cap, refused rather than truncated. Verified in `tools/verify_always_on.py` §13b–13f.",
        "⏳ Open. **Against:** OpenClaw.",
    ),
    "### P1.2 — `once`: disable the watcher after its first fire": (
        "**Status: ✅ DONE.** `Condition.once`. `Dot.end_run` disables the Dot only when the run **succeeded**, so a "
        "one-shot watch whose attempt failed stays armed and retries. Verified in `verify_always_on.py`.",
        "⏳ Open. **Against:** OpenClaw. **Small — do this second.**",
    ),
    "### P1.3 — A condition command that does not exist reads as quiet": (
        "**Status: ✅ REDUCED — the window is narrowed and the remaining limit is pinned.** "
        "`conditions.unresolvable_program()` resolves the program *before* running it, so a bare command's typo is "
        "reported as an ERROR that wakes the agent. It returns \"\" for anything ambiguous (pipelines, chains, "
        "redirects, env prefixes, builtins) because a false warning would teach the reader to ignore the real one. "
        "A missing program buried in shell syntax is therefore still undetectable — pinned in "
        "`verify_always_on.py` §13h, and reported by `myriaxon doctor` for the resolvable shapes.",
        "⚠️ Known limit — **this is a defect to reduce, not a task to close.**",
    ),
    "### P1.4 — `dots --status` does not show the condition, its verdict, or the watchlist": (
        "**Status: ✅ DONE.** `format_dot` renders the condition's description, kind and target, the last verdict "
        "with its age, the watchlist with an item count, and a **loud** `!!` block for an `ERROR` verdict. Verified "
        "by `tools/verify_locus_status.py` (43 checks), which asserts the loud state specifically and that a healthy "
        "dot gains no clutter.",
        "⏳ Open — **introduced by the always-on work, and it hides P1.3.**",
    ),
}


def main() -> int:
    text = PLAN.read_text(encoding="utf-8")
    for heading, (new_status, old_status) in UPDATES.items():
        if heading not in text:
            print(f"  MISSING heading: {heading}")
            return 1
        start = text.index(heading)
        end = text.find("\n### ", start + 1)
        if end == -1:
            end = len(text)
        section = text[start:end]
        if old_status not in section:
            print(f"  MISSING old status in: {heading[:48]}")
            print(f"    looked for: {old_status[:70]}")
            return 1
        replaced = section.replace(old_status, new_status, 1)
        text = text[:start] + replaced + text[end:]
        print(f"  updated {heading[:52]}")

    PLAN.write_text(text, encoding="utf-8", newline="\n")

    print()
    remaining_open = len(re.findall(r"⏳ Open", text))
    done = len(re.findall(r"Status: ✅", text))
    print(f"  P1 items DONE      : {done}")
    print(f"  '⏳ Open' remaining : {remaining_open} (should be 0 in P1; P2/W rows use ➖)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
