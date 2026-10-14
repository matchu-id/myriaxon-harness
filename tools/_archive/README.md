# tools/_archive — finished one-shot scripts

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
