# OPEN WORK: bugs, unverified capabilities, and gaps

**For:** the engineering agent working in this repository.
**Owner:** Thariq Arian Khalfani — ariankhalfani@gmail.com
**Written:** 2026-10-15, at commit `9782be8`.
**Companion to:** [`task.md`](../task.md) — the IQB-14 scoring playbook. **Read that one too**; this file is
the open-work register, not a replacement.

---

## How to read the labels

Every item is marked with how it is known. The distinction is the whole point of this file.

| Label | Means |
|---|---|
| **BUG — reproduced** | Observed, with the mechanism traced. Someone must fix it. |
| **BUG — reasoned** | The mechanism is identified by reading the code and the failure follows from it, but it has **not been watched happening**. Confirm before you fix. |
| **UNVERIFIED** | Code exists and its unit tests pass; **nobody has proved it end to end**. Treat the capability as unproven. |
| **OPEN** | Known missing work. |
| **PROVEN DEAD** | Established by measurement. Do not spend time here. |

**The rule that matters most here:** a green test is not evidence a feature works. Both of this project's
worst defects were **green in the suite and broken in production** —

* the swarm board passed its tests while being **unreachable** from a confined Locus, because the board lives
  outside the workspace and `_resolve` refused it;
* per-member isolation passed while the **run loop granted every peer's workspace**, because the test granted
  a narrower set than production did.

Neither was a missing test. Both were a check that existed **on one path and not another doing the same
thing**. Drive the **real entry point** before you mark anything done.

---

## 1. BUG — the CLI prompt returns on its own, repeatedly, forever

**Reported as:** *"the CLI always auto enter each 5 sec"*.
**Status: mechanism traced, NOT fixed.**

### The mechanism

`myriaxon/cli.py` arms an idle timer from prompt_toolkit's `pre_run` so a due Locus runs without anyone
pressing Enter. **That design decision is correct** — without it, "autonomous" meant "autonomous once you
interact", the exact failure the Loci design exists to remove. The defect is in what happens when the wake-up
finds nothing it is **allowed** to run.

```
cli.py:1757   pre_run = lambda: _arm_locus_wakeup(session, agent)

_arm_locus_wakeup:
    limit = _dot_wakeup_limit()                               # default 15.0 s
    delay = min(get_registry().next_wakeup_seconds(), limit)
    loop.call_later(max(0.05, delay), _wake)

_wake:   app.exit(result=LOCUS_WAKE_SENTINEL)
cli.py:1780   if line == LOCUS_WAKE_SENTINEL: continue         # loop straight back, no input
```

And in `loci.py`:

```python
soonest = min(d.seconds_until_due(now) for d in candidates)
if soonest <= 0:
    return 0.0
```

So when anything is due, `delay` = `max(0.05, 0.0)` = **0.05 s**. The prompt exits almost immediately, the
REPL loops back, the proactive pass finds **nothing it is allowed to run**, the prompt is re-armed, and it
exits again.

### Why it never settles

A Locus becomes **permanently due and permanently unable to run** when its preflight refuses. Measured on this
machine:

```
anker   proactive=True   due=True   open objectives=0
preflight -> ['it has no open objectives, so a run would have nothing to work on']
```

A **refused run does not advance `last_run_at`**, so `is_due()` stays true forever, so
`next_wakeup_seconds()` keeps returning `0.0`, so the prompt keeps self-waking — and every wake produces no
output. Indistinguishable from a hang, with nothing on screen to explain it.

The reason it is seconds and not milliseconds is `_dot_wakeup_limit()`'s **15-second idle cap**, which is the
only thing standing between this and a spinning core. The reported "5 seconds" is the right family; **15 s is
the value in the code** — confirm which you observe, because it says whether the cap is what is pacing it.

The supervisor path already solved the *reporting* half: `dashboard/server.py` `start_supervisor()` prints a
refusal **once per distinct reason**. The REPL path says nothing.

### The fix, and the trap in the obvious version

**Do not just floor the delay at 1 second** — that trades a 15-second livelock for a 1-second one.

1. `next_wakeup_seconds()` should return a **minimum re-check interval** rather than `0.0` when the only due
   candidates are ones preflight **refuses**. A due-but-unrunnable Locus is a reason to stop asking, not a
   reason to wake.
2. The wake decision must consult **the same preflight the run would**. `registry.due()` and `Locus.preflight`
   disagree about what "due" means, and that disagreement **is** the bug.
3. Back off geometrically up to the cap, rather than re-arming at the cap value every time.
4. Print the refusal **once** in the REPL, as the supervisor does.

### Verification

```
1. Have a Locus with zero open objectives (anker is already in this state).
2. Start `myriaxon` and type nothing.
3. Before: the caret resets repeatedly, forever, with no output.
   After:  the prompt sits still; next_wakeup_seconds() returns the idle cap, not 0.0.
```

---

## 2. BUG — `MYRIAXON_DOT_WAKEUP` is named after a deleted feature and documented nowhere

`_dot_wakeup_limit()` reads `MYRIAXON_DOT_WAKEUP`. **Dots** was this project's earlier name for persistent
agents and **Loci replaced it** — `dots.py` exists only on old branches, and the docs carry 87 Loci references
against a handful of passing mentions of Dots.

Consequences: a user hitting §1 has an escape hatch they cannot find, and it is named after something that no
longer exists. The variable appears only inside that function — not in `.env.example`, not in the README, not
in `docs/HOW-TO-USE.md`.

**Fix:** rename to `MYRIAXON_PROMPT_WAKEUP`, keep the old name working with a deprecation note, and document
it in `.env.example` beside `MYRIAXON_TRANSPARENCY`.

---

## 3. UNVERIFIED — passes its tests, never driven end to end

Each has passing unit tests. **None has been proved through its real entry point with real inputs.** That is
precisely the shape of both defects in the introduction, so treat all of them as unproven.

| # | Capability | What is unproven | How to prove it |
|---|---|---|---|
| 3.1 | **Swarm board under real concurrency** | Only ever tested **in one process**. Two processes claiming one task, and a member dying so its lease expires, are untested. | Two `myriaxon loci --run` processes on one swarm dir; assert exactly one claims the task. |
| 3.2 | **Two agents actually communicating** | The wall renders cross-agent lines from the transparency feed, but **no two real agents have ever been observed exchanging anything**. The join has only been tested with a **synthetic injected event**. | Run two Loci that exchange a board task; confirm the `⇄` lines land in the right panes, attributed to the right agent. |
| 3.3 | **A Locus advancing a multi-step objective unattended** | `anker` completed one objective, but no run has been watched start-to-finish across **multiple** supervisor wake-ups with the goal tree advancing. | `loci --watch` with a 3-sub-goal objective; confirm the tree advances with no intervention. |
| 3.4 | **Sandboxed shell inside a confined Locus** | The confinement **refusal** is well tested. The **container path** — Docker mounted, command run, output returned — has never executed on this machine. | Install Docker, set `sandbox_enabled`, run a Locus that shells out. |
| 3.5 | **Collective memory across processes** | `locus_memory.share()` / `recall()` are tested in **one** process. Whether a second process sees a finding is unproven. | Share in one process, recall in another. |
| 3.6 | **`verify_binary_identity` off Windows** | Written for Windows. On Linux/macOS it should skip cleanly — untested. | Run it under WSL. |
| 3.7 | **The wall under real multi-agent load** | 54 checks pass in real Chrome, but only ever with **one** agent sending a message. Nine panes with several agents working at once is untested. | Two Loci running, nine panes, watch the shared poll and the rendering. |

---

## 4. OPEN — known gaps, not started

| # | Gap | Notes |
|---|---|---|
| 4.1 | **The CI workflow is not published** | `.github/workflows/ci.yml` exists and is correct, but publishing under that path needs the **`workflow` OAuth scope**, which the token does not have. **The blocker is the owner's, not code:** `gh auth refresh -h github.com -s workflow`. The file is safe in three places — branch `ci-workflow-ready`, on disk, and `C:\Users\arian\dev\mx-keep-ci.yml.txt`. One push after the scope is granted. |
| 4.2 | **Gemini 3.x multi-tool turns fail on the second turn** | Root cause **pinned to Vertex, not to us**: the failing payload was captured and `write_file` arrives with **no `thought_signature`** in a turn separate from the signed one. The fix is a **policy** — do not replay an unsigned `functionCall`; write that turn as text instead — and it is **not implemented**. Measurements: `docs/GEMINI-MULTITOOL-STATUS.md`. |
| 4.3 | **`docs/Level4-5-STATUS.md` predates this pass** | It was written before the security fixes, the transparency dial and the wall. The L4/L5 claims are still true, but the document does not mention them. Refresh it, or the status map understates the project. |
| 4.4 | **Level 5.5 is permanently capped** | A harness can record `checked` / `inferred` / `assumed` and demand evidence for the first two. It **cannot make a model feel doubt**, and the module says so. Do not try to close this — document it. |

---

## 5. PROVEN DEAD — established by measurement, do not re-investigate

1. **`myriaxon.exe` is not written by this project.** pip generates it from a template; `git ls-files` finds
   no `.exe`. It is `NotSigned` and **Windows reports no version information at all** — which is why a
   scanner may flag it. Eleven sibling launchers in one `Scripts` directory, 108,396–108,425 bytes, are the
   proof it is the template.
2. **The version-resource stamping path does not work.** `UpdateResource` + `EndUpdateResource` return
   cleanly, the commit succeeds, the file's SHA-256 changes — and **Windows still sees nothing**. distlib's
   launcher already occupies the `RT_VERSION` slot with a blob the loader will not parse, and the
   resource-update API adds alongside it. **Do not retry it.** What works: code signing, a PyInstaller build
   (`myriaxon.spec` now wires in `version_file_text()`), or `python -m myriaxon`. See
   `docs/FALSE-POSITIVE.md`.
3. **The pip launcher is not misbranded as `t64.exe`.** A hand-written PE parser in
   `myriaxon/launcher_identity.py` (`_read_version_strings`) says so and is **wrong** — it read distlib's
   raw unicode string blob, not a VERSIONINFO structure. **Windows is the only authority**, and `describe()`
   asks Windows. `_read_version_strings` is now unused for reporting: **delete it** rather than leaving a
   parser that produces confident wrong answers.
4. **The ollama test flake is not your bug.** It fails 4–6 runs in ~8, always a different test, passes alone.
   Four hypotheses were disproved by measurement. Do not chase it.
5. **`glob_search` is fixed.** It walked the whole filesystem (measured **132,353 ms**) and its `fnmatch`
   matcher could not match `**/*.py` at all. The time budget **could not fire**, because `Path.glob`
   enumerates every directory before yielding the first result. Now `os.walk` with in-place pruning: 21 ms.
6. **A Locus cannot mark its own objective done.** Deliberate — an agent that grades its own homework is one
   you cannot trust the grade from. Do not "fix" it.
7. **Usage figures are per-process.** `mission_control` reports tokens and requests for the **running
   server**; a restart honestly reports zero. There is no persisted ledger, and nothing is extrapolated.

---

## 6. Tools built for you — use them, do not reinvent

```bash
python tools/run_checks.py               # every verifier + the pytest suite
python tools/verify_l4_l5.py             # 79 functional L4/L5 checks, evidence printed per line
python tools/bench_l4_l5.py              # the same bench, readable, with --level 4|5|explain
python tools/browser/wall_check.py       # drives the real dashboard in real Chrome
python tools/verify_binary_identity.py   # is the installed launcher genuine?
python tools/ui_check.py                 # dangling ids, required panels, JS syntax
```

**`tools/browser/` is the one to reach for.** It starts Chrome, drives the real page over the DevTools
Protocol and asserts on the DOM. No mocking, nothing to install — Chrome ships on Windows, and node 22+ has
`WebSocket` built in. **This is how you catch what a unit test cannot:** the wall was *completely dead*
(`renderWall` called a function that did not exist, the exception escaped a `forEach`, no pane was ever
appended) while every static check passed and the room merely looked empty.

`wall_check.py` **skips** rather than fails when Chrome or node is absent. Keep that: a check that cannot run
is not a check that failed, and reporting it as one teaches people to ignore the suite.

---

## 7. Habits that caught the real defects

1. **Drive the real entry point.** `dispatch_tool`, the run loop's own grants, the condition evaluator, the
   live dial — not a stub that agrees with your assumption.
2. **State the mechanism, then the evidence.** "It does not work" is not a finding. "It returns success and
   Windows sees nothing" is.
3. **Write the check so it cannot pass for the wrong reason.** Three wall checks passed wrongly at first: a
   drag test compared names with **no empty slot to move into**, so it could never fail; an agent-reply test
   searched for a word **its own echoed message contained**; a rejection test created the promise in the
   *evaluation* context rather than the page's. **All three looked green.**
4. **Assert the reason, not only the verdict.** `changed == True` is worth less than *why* it changed.
5. **Delete dead code rather than leaving it reporting success.** A PE writer here claimed success for a day
   while doing nothing. Dead code that lies is worse than no code.
6. **Record the wrong turn.** The corrections in §5 cost hours; the next agent should not pay again.
7. **Review the staged diff before committing.** `git add -A` committed generated Locus output into this
   repository **twice**. Both times the diff review is what caught it.

---

## 8. The first hour, concretely

```bash
python tools/run_checks.py     # confirm the baseline is green before changing anything
```

Then, in priority order:

1. **Fix the CLI livelock (§1).** It makes the terminal unusable and it is one function. Watch it happen first
   so you fix the real thing, then confirm `next_wakeup_seconds()` stops returning `0.0`.
2. **Run the two-process swarm test (§3.1) and the two-agent wall test (§3.2).** These are the two claims the
   whole multi-agent story rests on and neither has ever been exercised for real.
3. **Implement the Gemini policy (§4.2).** Every measurement is already in the repository.

Do not start with §4. The items in §1 and §3 are what stand between this project and being trustworthy, and
§3 is where the surprises are.
