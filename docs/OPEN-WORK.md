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

## 1. ~~BUG~~ FIXED — the CLI prompt returned on its own, repeatedly

**Reported as:** *"the CLI always auto enter each 5 sec"*.
**Status: FIXED.** Kept here because the mechanism is instructive and the fix has a failure mode worth
knowing about.

### The correction that mattered

My first reading of this was **wrong in an important way**. I framed it as a livelock to be broken by making
the timer smarter. The owner's correction:

> *"there is no wrong with wake up call, just the problem is it is locus feature only. myriaxon shouldn't have
> L4 autonomy. the capability of self decide and plan and do something is Locus only."*

That is the right frame, and it changes the fix from *tuning a timer* to *gating a feature*. Self-direction is
the entire point of a Locus; a Myriaxon session has a **person** driving it. A prompt that returns on its own
in Myriaxon mode is not autonomy, it is an interruption — and it cannot even succeed, because there is nothing
for it to advance.

### What was actually wrong

`_arm_locus_wakeup` armed a timer in **every** session, including a plain Myriaxon one. And the loop was
self-sustaining rather than merely useless:

```
next_wakeup_seconds() returns 0.0 whenever anything is due
  -> delay = max(0.05, 0.0) = 0.05s
  -> app.exit(LOCUS_WAKE_SENTINEL)
  -> the REPL loops back, the pass finds nothing it is ALLOWED to run, the prompt re-arms and exits again
```

Nothing ever settles because a Locus whose preflight refuses stays **permanently due** — a refused run does not
advance `last_run_at`. Measured on this machine: `anker`, due, zero open objectives, preflight refusing.
`due()` and `preflight()` disagree about what "due" means, and that disagreement **is** the bug.

### The fix, in two halves — the second is the one easy to miss

```python
# 1. Myriaxon mode never arms a timer. `active_dot` is the mode flag the prompt already used.
if not getattr(agent.tool_context, "active_dot", "").strip():
    return

# 2. Locus mode does not arm one either when there is nothing to run.
#    `next_wakeup_seconds()` returns the IDLE CAP (30s) in that case, and the cap exists so a
#    deployment notices a Locus enabled in the meantime -- but for an interactive prompt the same
#    value is a timer that fires every 30 seconds to run nothing: the same loop, merely slower.
if not registry.headless():
    return
```

`headless()` is the honest question — enabled, proactive, **and with work it is allowed to do** — so it already
applies the preflight that `due()` skips.

**Both halves are load-bearing.** Gating on "is anything runnable" alone would still arm a timer in Myriaxon
mode whenever some *other* Locus happened to be due.

### Verified

| Situation | Timer | |
|---|---|---|
| Myriaxon mode (`active_dot` empty) | **no timer** | the loop is gone |
| Myriaxon mode, another Locus has work | **no timer** | work elsewhere is not a reason to interrupt |
| Locus mode, nothing runnable | **no timer** | nothing to wake for |
| Locus mode, **work waiting** | **timer at 0.05s** | the feature still works |

`tests/test_wakeup_is_locus_only.py`, 9 tests. **The last row is the one that matters most** — over-gating is
the failure mode of a fix like this, so it is asserted directly rather than assumed.

The trade, stated rather than hidden: a Locus that becomes runnable while you sit at the prompt is picked up
on your **next keypress** instead of immediately. Firing a timer under somebody's hands to discover there is
nothing to do is worse than waiting for them.

### What a future change must not do

Do **not** "fix" a wake-up problem by flooring the delay at one second. That trades a 15-second interruption
loop for a 1-second one. The question is not *how often* to wake, it is *whether there is anything to wake
for* — and *whether this session owns autonomy at all*.

---

## 2. BUG (open) — `MYRIAXON_DOT_WAKEUP` is named after a deleted feature and documented nowhere

Still open. `_dot_wakeup_limit()` reads `MYRIAXON_DOT_WAKEUP`, and **Dots** was this project's earlier name
for persistent agents — **Loci replaced it**. `dots.py` exists only on old branches; the docs carry 87 Loci
references against a handful of passing mentions of Dots.

The variable appears only inside that function: not in `.env.example`, not in the README, not in
`docs/HOW-TO-USE.md`. It is the escape hatch for the wake-up behaviour fixed above, and a user cannot find it.

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

1. **Run the two-process swarm test (§3.1) and the two-agent wall test (§3.2).** These are the two claims the
   whole multi-agent story rests on, and neither has ever been exercised for real. **Start here** — they are
   where the surprises are.
2. **Implement the Gemini policy (§4.2).** Every measurement is already in the repository.
3. **Rename and document the wake-up escape hatch (§2).** Small, and it is the only way a user can turn off
   behaviour they do not want.

The CLI interruption loop (§1) is **fixed** — no action needed, but read the section before touching wake-up
logic again, because the failure mode of that fix is over-gating.
