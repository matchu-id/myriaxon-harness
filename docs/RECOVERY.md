# RECOVERY — what was lost, what survived, and how

> Written the moment the damage was found. Read this before doing anything else in this repository.

## What happened

The working tree was wiped. `myriaxon/` — the entire package — was **deleted**, the virtual environment was gutted
down to seven binary fragments, and `.git` was reduced to `objects/` with `HEAD`, `config`, `refs/` and the index all
gone. Nothing was committed, so there was no commit to fall back to and nothing in git had ever been pushed.

Found by running the verifier sweep and getting `No pyvenv.cfg file` for all 48 files.

## What was recovered, and from where

The object database itself survived: **547 blobs, 171 trees, 35 commits**, plus three pack files (~1.6 MB). The
spelling-out of that recovery, so it can be repeated or extended:

```powershell
# 1. The objects are there but git cannot read them without a skeleton. Rebuild the minimum:
New-Item -ItemType Directory -Force .git\refs\heads, .git\refs\tags
Set-Content .git\HEAD "ref: refs/heads/main" -NoNewline
Set-Content .git\config "[core]`n`trepositoryformatversion = 0`n`tbare = false"

# 2. Inspect what is actually in there.
git cat-file --batch-all-objects --batch-check | Group-Object { ($_ -split ' ')[1] }

# 3. Give the newest commit a ref so it can never be garbage-collected. THIS STEP FIRST — a `git gc` on an
#    unreferenced object store deletes the entire history.
git update-ref refs/heads/recovered <sha>
git update-ref refs/heads/main <sha>

# 4. Restore the tree from it.
git checkout -f refs/heads/main
```

The newest commit was **`7caf8c65` (2026-10-02 11:42)**, restored to `refs/heads/main` and preserved as
`refs/heads/recovered`. Both refs point at it.

**Recovered: 107 files in `myriaxon/` including 61 Python modules**, plus `tests/`, `tools/`, `docs/`, `README.md`,
`pyproject.toml`, `CHANGELOG.md`, `INSTALL.md`, `install.ps1`, `install.sh` and the launchers.

### The overwrite this caused, and it was avoidable

`git checkout -f` replaced `tools/` and `tests/` with the **2026-10-02** versions, discarding the newer versions that
were in the working tree. That is a real second loss caused by the recovery itself.

What survived it: the four Loci verifiers written afterwards were listed as untracked and were **not** overwritten —
`tools/verify_loci_character.py`, `verify_loci_e2e.py`, `verify_loci_autonomy.py`, `verify_loci_capabilities.py` are
present. The next step should have been `git stash` or a copy aside before `checkout -f`.

## What is LOST, precisely

All of it was written after 2026-10-02 and never committed. Listed by module so the loss is countable rather than
vague:

### The product code — nine modules, all of it gone

| Module | What it did |
|---|---|
| `myriaxon/loci_character.py` | The character: 14 expressions, 7 moods, palette, four terminal tiers, glyph-width guard |
| `myriaxon/loci_expression.py` | The rules: signal→expression table, crisis stickiness, celebrate rationing, anger-never-mirrored |
| `myriaxon/loci_surface.py` | The sheet, prompt prefix, status fragment |
| `myriaxon/loci_svg.py` | The dashboard SVG, generated from the shared manifest |
| `myriaxon/character.py` | The live wiring: `Character`, `after_turn`, autonomy tick |
| `myriaxon/loci_autonomy.py` | The autonomy engine: five motivations, six personalities, circadian, grid-locked simulation |
| `myriaxon/loci_expressions_more.py` | 19 extra expressions plus the spec fallback map |
| `myriaxon/loci_capabilities.py` | Wins jar, memory palace, habits, journal, focus, reflection |
| `myriaxon/loci.py` | The renamed registry — `dots.py` is back, so this is the Dot era again |

Also lost: the agentic modules built across earlier rounds — `diagnostics.py` (LSP tiering), `jobs.py` (background
job service), `plan_mode.py` (enforced plan mode), `worktree.py` (git isolation), `session_lineage.py` (session
forking), `run_export.py` (sanitised export), `steering.py` (mid-turn redirect), `interpreter.py` (Python discovery).

### The packaging work

`MANIFEST.in`, the `pyproject.toml` licence/entry-point/build-system fixes, and `dist/`.

### `loci.md`

The character's design specification, supplied by the user. **Not in the repository and never committed**, so it
cannot be recovered from here. Ask for it again.

### Verified counts were lost with the code

Recorded here so the numbers are not silently re-invented later, and flagged as **unverifiable until the code is
restored**: 48 verifiers totalling ~2,900 checks, 470 passing tests, 41/41 end-to-end checks.

## The one lesson worth carrying

**Nothing was committed, for the whole project.** Every round ended with "nothing is committed" and it was treated
as a status line rather than a risk. The risk was this. A working tree with no commit is one deletion away from being
gone, and it was one deletion away for the entire project.

The repair is not "commit more carefully next time" — it is that the first commit should have happened before the
first feature.

## Immediate next steps

1. **`git commit` the recovered tree right now**, so the 2026-10-02 state is durable.
2. Ask the user for `loci.md` again, and re-state what was lost so they can decide what to rebuild first.
3. Rebuild in this order, committing after each: packaging (`MANIFEST.in`, `pyproject.toml`) → the Loci character →
   the expression layer → autonomy → capabilities. The verifiers for all four **already exist**, so each rebuild has
   its own test waiting.
