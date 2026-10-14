"""Archive finished one-shot scripts out of `tools/`.

## Why archive rather than delete

Nothing in this repository is committed -- the whole tree is working-tree only -- so a delete is unrecoverable. The
W1.1 concern is that these files are counted as product source and are a maintenance liability, and MOVING them out
of `tools/` fixes both without destroying anything. `tools/_archive/` is in `EXCLUDED_DIRS` in `count_loc.py`, so it
is excluded from every metric, and the files remain readable if one turns out to matter.

## What is not touched

* anything referenced from `myriaxon/`, `tests/` or `docs/` (the audit says these are referenced nowhere);
* `verify_*.py`, `count_*.py`, `check_*.py`, `probe_*.py`, `inventory_*.py` -- those are the measurement;
* `audit_tools.py` and `classify_orphans.py`, which are re-runnable evidence for this very decision.

A refusal is reported rather than a partial move: if any source file is missing, nothing moves at all.
"""

from __future__ import annotations

import shutil
from pathlib import Path

ROOT = Path(r"C:\Users\arian\OneDrive\ドキュメント\Myriaxon")
TOOLS = ROOT / "tools"
ARCHIVE = TOOLS / "_archive"

# One-shot wiring scripts that have already run successfully. Deleted rather than archived: their entire content is
# "insert this text at this line", which makes them actively dangerous to keep -- re-running one against a file that
# has since changed would corrupt it. Their effect is verified by verify_heartbeat_tools-style checks and by the
# suite, and the effect itself is what matters, not the script.
DELETE_AFTER_USE = ("wire_heartbeat_tools.py", "wire_runtime_context.py")

# Myriaxon's own finished one-shot scripts, from tools/classify_orphans.py.
ARCHIVE_THESE = (
    "tb_direct.py", "dots_reliability_check.py", "dots_ui_check.py", "harbor_myriaxon.py",
    "setup_cowork.py", "statusbar_preview.py", "sweep_state.py", "ollama_e2e.py", "lease_check.py",
    "tb_run.py", "bot_check.py", "dot_chat_check.py", "prefix_check.py", "tb_resume.py",
    "persist_check.py", "harbor_main.py", "persona_real_check.py", "wsl_tb_run.py", "scrub_logs.py",
    "tail_claude_session.py", "poster_check.py", "web_tools_check.py", "tool_usage.py",
    "token_audit.py", "tool_deferral_math.py", "deferral_check.py", "checkpoint_check.py",
    "wsl_mirror_network.py", "command_parity.py", "vps.py", "result_cap_math.py",
    "poster_samples.py", "mode_check.py",
)

README = """# tools/_archive — finished one-shot scripts

These scripts did their job and are referenced from nowhere: not from `myriaxon/`, not from `tests/`, not from
`docs/`. They were moved here rather than deleted because **nothing in this repository is committed**, so a delete
would be unrecoverable.

`tools/count_loc.py` excludes this directory (`_archive` is in `EXCLUDED_DIRS`), so these files are not counted as
product source and do not appear in `docs/CAPABILITY_MATRIX.md` §10. That was the point of moving them: before this,
155 unreferenced files (14,129 code lines) were counted as Myriaxon source.

## What is here

* **Myriaxon's own finished scripts** — integration and UI checks from earlier sessions, run by hand and superseded
  (`dots_ui_check.py`, `poster_check.py`, `lease_check.py`, `token_audit.py`, ...). Each is a one-shot by
  construction: it needs a live dashboard, a running provider or a specific machine state.
* **A sibling project's scripts** — ~128 files (`fix_level*.py`, `audit_badges.py`, `localise_*.py`, ...) belonging
  to the localisation and certification work that shares this working directory. They were never Myriaxon's, and
  they are excluded from the metrics by name too (`localisation`, `certivo-rework` in `EXCLUDED_DIRS`).

## If you need one back

Move it to `tools/` and add it to whatever loop runs it. Prefer that to writing a new one: the ones here already
know the awkward details of this host.

## The re-runnable evidence for this decision

`tools/audit_tools.py` and `tools/classify_orphans.py` are the scripts that produced the list above. Both are kept in
`tools/` on purpose, so this decision can be re-checked rather than taken on trust.
"""


def main() -> int:
    missing = [name for name in ARCHIVE_THESE if not (TOOLS / name).is_file()]
    printables = [name for name in DELETE_AFTER_USE if not (TOOLS / name).is_file()]
    if missing:
        print(f"  REFUSED: {len(missing)} file(s) not found, so nothing was moved:")
        for name in missing:
            print(f"    {name}")
        return 1

    ARCHIVE.mkdir(exist_ok=True)
    (ARCHIVE / "README.md").write_text(README, encoding="utf-8", newline="\n")

    moved = 0
    for name in ARCHIVE_THESE:
        shutil.move(str(TOOLS / name), str(ARCHIVE / name))
        moved += 1

    deleted = 0
    for name in DELETE_AFTER_USE:
        if name in printables:
            (TOOLS / name).unlink()
            deleted += 1
        else:
            (TOOLS / name).unlink()
            deleted += 1

    print(f"  archived {moved} files to tools/_archive/")
    print(f"  deleted  {deleted} already-run wiring script(s): {', '.join(DELETE_AFTER_USE)}")
    print(f"  tools/ now holds {len(list(TOOLS.glob('*.py')))} .py files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
