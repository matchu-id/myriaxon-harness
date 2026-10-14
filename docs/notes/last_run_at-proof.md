# Objective [18481a61] — prove `last_run_at` is stamped

**Conclusion: proven.** `last_run_at` is stamped on every run, in memory by
`Dot.end_run` (dots.py:987) and on disk by `DotRegistry.touch` (dots.py:1463, which
also re-stamps `updated_at`). Every run path calls `touch` afterwards, so the stamp
always reaches `dots.json`.

## Evidence

### 1. Direct script — `.myriaxon/dots/cd474cc6/prove_last_run_at.py` (runs green)
Runs against a throwaway registry file, real `dots.json` untouched:

```
[fresh]   last_run_at=0.0  is_due=True                     # new dot = never ran
[end_run] last_run_at=1790824459.346  in [..,..]           # stamped to ~now
          updated_at==last_run_at: True
[pre-touch] on-disk last_run_at=0.0 (end_run does not write; touch does)
[on-disk] last_run_at=1790824459.346  reloaded.last_run_at=1790824459.346
[after]   is_due=False  seconds_until_due=900              # no longer due
[touch]   submit-time last_run_at=1790824459.348 (>0: True)
PROVEN
```

### 2. Unit test — tests/test_dots_reliability.py
`test_end_run_clears_the_flag_and_the_lease` asserts `dot.last_run_at > 0` after
`end_run` (in-memory stamp). Passes.

### 3. Integration test — tests/test_dots_panel.py
`test_a_run_records_that_the_dot_has_run` drives `/api/dots` action=run and asserts
`seconds_until_due > 0` and `due_now == False` afterwards — i.e. the stamp survived
to the surface that schedules. Passes.

### 4. Coverage of the run paths (grep)
`registry.touch(dot)` is called by every caller that finishes a run:
- `myriaxon/tool_registry.py:568,572,579` (dot_run tool: error, failure, success)
- `myriaxon/dashboard/server.py:381` (panel, stamped at *submit* time)
- `myriaxon/cli.py:1394` (terminal proactive pass)
`end_run`/`run_scope` are only reached from those paths, so no run loses the stamp.

## Note (division of labour)
`end_run` stamps in memory only; it deliberately does **not** persist (its docstring:
everything after clearing the flag is "bookkeeping allowed to be imperfect").
Persistence is the caller's `registry.touch`. A hypothetical future caller of
`run_scope` that never calls `touch` would leave disk at 0.0 — worth keeping in mind,
but no current path does this.

## Unrelated observation
`tests/test_dots_panel.py::test_goal_errors` fails when the panel tests run together
(404 vs 400) but passes in isolation — pre-existing, order-dependent, unrelated to
`last_run_at`. Source tree is clean (`git status` empty).

## Blocker found while closing out (needs owner)
This dot (`duecheck`, id `cd474cc6`) is **not present in `.myriaxon/dots.json`** —
the file holds exactly one dot, `cowork` (`a1d987bb`). `dot_list` agrees, and
`dot_goal_update` therefore returns *"No dot named 'duecheck'"*. `dots.json` mtime
(1790824309) predates every action of this session, so nothing in this session wrote
it; the dot's registry entry was lost (or never persisted) around its creation at
10:11. Consequence: objective [18481a61] cannot be marked done through the dot tools
even though it is proven, and this dot's objectives are untrackable by the framework
until the entry is restored. Likely cause is the last-writer-wins window in
`DotRegistry.save`/`_reload_locked` (dots.py:1339-1360) that the code comments already
flag as "not the fully correct answer".
