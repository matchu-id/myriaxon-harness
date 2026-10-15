# Myriaxon — Level 4 and Level 5 Status Map

**Date:** 14 October 2026
**Basis:** every status below is against the code, with the commit or `file` that establishes it.
**Scope:** the 17 Level 4 and 9 Level 5 requirements from `docs/LEVEL-4-5-REQUIREMENTS.md`.

> **Every row says how it was established.** A status map that claims capability without a commit or a
> measurement is a wish list. Where something is deliberately *not* built, that is stated as a decision rather
> than left as an absence.

| Mark | Meaning |
|---|---|
| ✅ | Built, tested, and verified by running |
| ⚠️ | Deliberately partial — the limit is stated and asserted by a test |
| ❌ | Not built |

---

## Part 1 — Level 4 (17 requirements)

### A. Durability — an agent that loses its work is not managing a workflow

| # | Requirement | Status | Evidence |
|---|---|---|---|
| 4.1 | Run checkpoints at every step | ✅ | `run_state.record_step`, `97cea8c` |
| 4.2 | Resume from the last checkpoint | ✅ | Verified against a real `kill -9`: step 4 survived, `begin_run` returned 4 |
| 4.3 | Exactly-once for outbound side effects | ✅ | `messaging.send` records before sending; a resumed run skips |
| 4.4 | Lease renewal independent of step progress | ✅ | `_LeaseHeartbeat` (pre-existing) |
| 4.5 | Crash recovery without human action | ✅ | `run_state` + lease expiry; `97cea8c` |

### B. Coordination — the criterion that defines Level 4

| # | Requirement | Status | Evidence |
|---|---|---|---|
| 4.6 | **Shared substrate** | ✅ | `locus_swarm.py` — `board/`, `shared/`, `members/<id>/`; `336cc6b` |
| 4.7 | **Lease-based board**, filesystem as the lock | ✅ | `O_EXCL` compare-and-swap. Verified: `cowork` claims True, `sales` claims False |
| 4.8 | Capability tags | ✅ | `Task.tags`; `available(tags=[...])` |
| 4.9 | **Collective memory with provenance** | ✅ | `locus_memory.py`; unattributed findings refused; `d7620cb` |
| 4.10 | **Consensus** — N attempts, disagreement escalates | ✅ | `consensus.py`; a split never yields an answer; `5b3d171` |
| 4.11 | **Conflict detection on shared artifacts** | ✅ | `artifact_conflict.py`; digest check + advisory intents; `05f9895` |
| 4.12 | Per-member mount enforcement | ✅ | `tools.grant_shared` allowlist; board reachable, peer workspaces and `sessions/`/`audit.jsonl` still refused; `ecf6657` |

### C. Self-revision — "revises its own plans"

| # | Requirement | Status | Evidence |
|---|---|---|---|
| 4.13 | Re-plan mid-run when a step fails | ✅ | `Locus.note_step_failure`; the next prompt is told; `1579c25` |
| 4.14 | Abandon a dead end and record why | ✅ | Two failures produce "STOP AND RE-PLAN … mark it blocked with a note saying what you tried" |
| 4.15 | Add sub-objectives during a run | ✅ | `locus_goal_update` (pre-existing) |

### D. Tooling resilience — "rarely needs a human"

| # | Requirement | Status | Evidence |
|---|---|---|---|
| 4.16 | **Circuit breaker** | ✅ | `FAILURE_BREAKER_THRESHOLD = 2`; `55711a3` |
| 4.17 | **Dead-letter store** | ✅ | `dead_letter.py`; repeats become visible at 3+; `4344cb0` |

**Level 4: 17 of 17 built.**

---

## Part 2 — Level 5 (9 requirements)

### A. Self-directed goal formation

| # | Requirement | Status | Evidence |
|---|---|---|---|
| 5.1 | Decompose into sub-goals | ✅ | `Goal.parent`/`depth`, `MAX_GOAL_DEPTH = 3`, cap flattens rather than raising; `b7b3baa` |
| 5.2 | Prioritise by expected value | ✅ | `Goal.value` recorded; `next_goal` picks highest within the status tier |
| 5.3 | Retire obsolete objectives | ✅ | `GOAL_OBSOLETE` + reason; refuses anything the owner wrote |

### B. Risk and uncertainty management

| # | Requirement | Status | Evidence |
|---|---|---|---|
| 5.4 | Classify risk before acting | ✅ | `action_risk.py`, blast-radius classification; `e989f32` |
| 5.5 | **Declare its own uncertainty** | ⚠️ | `uncertainty.py`: checked/inferred/assumed, evidence required. **Cannot be closed** — see the ceiling |
| 5.6 | Escalate on a risk threshold | ✅ | `Risk.should_ask`; unknown tools escalate by default |
| 5.7 | Recognise being out of depth and stop | ✅ | `agent_discipline.py`; a repeating run ends with a reason; `65ae07e` |

### C. Context stewardship

| # | Requirement | Status | Evidence |
|---|---|---|---|
| 5.8 | Manage the context budget deliberately | ✅ | `check_context` + `spill`; the agent sees the numbers and relocates rather than loses; `6a1986e` |
| 5.9 | Decide what to remember and forget | ✅ | `sweep` by USE not age, dry-run by default, owner entries never swept; `547562f` |

**Level 5: 8 built, 1 permanently capped with its limit stated and asserted (5.5).**

---

## Part 3 — What was built this session

| Commit | Capability | Level |
|---|---|---|
| `336cc6b` | Swarm board — coordination | 4.6, 4.7, 4.8 |
| `d7620cb` | Collective memory with provenance | 4.9 |
| `5b3d171` | Consensus, disagreement escalates | 4.10 |
| `05f9895` | Conflict detection on shared artifacts | 4.11 |
| `ecf6657` | Per-member mount allowlist | 4.12 |
| `1579c25` | Re-plan on failure, abandon dead ends | 4.13, 4.14 |
| `4344cb0` | Dead-letter store | 4.17 |
| `65ae07e` | Repetition detector | 5.7 |
| `b7b3baa` | Decomposition, value ordering, retirement | 5.1, 5.2, 5.3 |
| `e989f32` | Action risk classification | 5.4, 5.6 |
| `57bdee2` | Declared uncertainty | 5.5 |
| `6a1986e` | Agent-chosen context budget | 5.8 |
| `547562f` | Memory curation | 5.9 |
| `c548e0d` | **netra empty-body fix** | defect |
| `f59c8d8` | **`--vendor gemini` fix** | defect |
| `97cea8c` | Durable execution + exactly-once | 4.1, 4.2, 4.3, 4.5 |
| `55711a3` | Failure circuit breaker + answer-first | 4.16 |
| `34d8aff` | Vertex/Gemini, no API key | capability |
| `3e2cc3c` | Context cache + accounting | capability |

**Roughly 300 new tests across 15 files.**

---

## Part 4 — Decisions made, and why

Recorded because each is a judgement a reader may want to revisit, and a decision with no stated reason is
indistinguishable from an oversight.

| Decision | Reason |
|---|---|
| **Board uses expiring leases, not locks** | A lock held by a member that dies turns one crash into a stopped swarm. This harness has already been bitten by that shape. |
| **Conflict detection does not block** | Same reason. It answers two questions and leaves the decision to the caller. The window between check and write is stated and asserted, not hidden. |
| **Consensus never auto-picks a winner** | Majority, longest, most-confident all convert an unresolved question into a decision nobody made. |
| **Retirement is a state, not a deletion** | A deletion cannot answer "why did this stop being pursued", and an agent with a delete button for its instructions fails silently. |
| **The depth cap flattens rather than raises** | Refusing would fail a run because the agent tried something reasonable. The work survives; only the tree is refused. |
| **Value ordering is inside the status tier** | Ordering everything by value would reintroduce a measured bug where a new objective starved work already half-done. |
| **Unknown tools escalate** | The cost of asking is one prompt; the cost of an irreversible mistake is not recoverable. |
| **Dead letters store a pointer, not a transcript** | A store that grows to megabytes gets deleted to save space, taking the operational signal with it. |
| **Similarity grain is a parameter, not a constant** | Measured: 5-grams catch a restatement (52% paraphrase), 2-grams catch agreement (75% vs 15%). No single default suits both. |
| **Memory curation is dry-run by default** | A curation that acts before it can be inspected is one nobody trusts. |
| **Grants are an allowlist, not a widened boundary** | Widening to the config dir would expose `sessions/`, `audit.jsonl` and every peer workspace; mounting the board per-member would share nothing while appearing to share. |

---

## Part 5 — The honest ceiling

**5.5 — declared uncertainty — cannot be fully built, and this is the requirement that caps the level.**

The harness can do the observable half, and does: a claim is recorded as `checked`, `inferred` or `assumed`,
with `checked` and `inferred` required to name their evidence so the label stays meaningful; unverified claims
are reported first; and a run that restates itself without acting is detected, stopped and recorded.

**What no harness can do is make a model feel doubt.** Six differently-worded wrong answers look like progress
to any textual comparison, and that is a limit of the method rather than a tuning problem — asserted by a test
so nobody "fixes" it by lowering a threshold.

---

## Part 6 — The safety envelope, unchanged

Every boundary from `LEVEL-4-5-REQUIREMENTS.md` still holds. Nothing added this session relaxed one:

| Boundary | State |
|---|---|
| Workspace confinement | Unchanged — shared directories are granted by an explicit named allowlist, never by widening the boundary |
| Network isolation | Unchanged — `--network none` |
| Budget ceiling | Unchanged — `run_budget.py`, and every new trigger is bounded |
| Completion authority | Unchanged — a Locus still cannot mark its own goal done |
| Self-modification | Unchanged — a Locus cannot retire an objective its owner wrote, nor sweep an owner's memory |
| Action risk | **New** — `action_risk.py` escalates before a dangerous action, and is explicitly not a sandbox |

---

## Part 7 — What remains

Every Level 4 and Level 5 requirement is now built, or has its limit stated and asserted. The four that were
open when this document was first written — 4.12, 5.5, 5.8, 5.9 — are closed.

What remains is **integration, not capability**:

| # | Item | Level |
|---|---|---|
| 1 | Wire `action_risk` into the dispatch path | 5 |
| 2 | Surface the board, memory and dead letters in the dashboard | 4 |
| 3 | `swarm_consensus` tool — let a Locus call consensus | 4 |
| 4 | Surface `declare_uncertainty` in the final answer | 5 |

### The one requirement that cannot be closed

**5.5 — declared uncertainty — is capped at "the observable half", permanently.** Level 5 therefore tops out
below a perfect score, and that is the honest number.

---

## Part 8 — The ordering flake: RESOLVED, and it was in the test

The failure report contained the answer all along:

```
E  assert ('Ollama' in '')
Captured stdout call
  Ollama is installed but not answering at http://127.0.0.1:9. Start it with: ...
```

The message **was** emitted — pytest captured it at the file-descriptor level and displayed it inside the very
failure that claimed the string was empty. `capsys` reads the object pytest installs as `sys.stdout` for one
test, and a Rich `Console` resolves its stream through state pytest rewrites between tests, so which capture
receives the write depends on ordering. The test now binds its Console to a `StringIO` it owns.

Four hypotheses were tested and **all four were disproven**, recorded so nobody repeats them:

| Hypothesis | Verdict |
|---|---|
| Rich captures `sys.stdout` at construction | **FALSE** — a survivor experiment showed `console.file is sys.stdout` at write time |
| A stale Console survives into the next test | **FALSE** — a Console built in one test writes correctly in the next |
| `sys.stdout` is closed | **FALSE** — the repair fixture never fired once |
| `.env` leaks into `os.environ` | **FALSE** — isolated per test; the flake persisted |

Two guards remain in `tests/conftest.py` because they are worth having independently, **not** because they
fixed this: environment isolation, and a closed-stream repair that turns a cascade into one named warning.

---

## Part 9 — Two live provider bugs found by testing, not reading

Both were found by exercising the providers over real HTTP. Neither would have been found by review, and both
are the same shape: **a permissive `**kwargs` accepting a plausible keyword and silently dropping it.**

### Bug 1 — `netra` chat completions sent an EMPTY BODY

`_complete_chat` called `self._http.post(url, ..., json_body=body)`, and `_Client.post`'s signature took only
`content`. `json_body` fell into `**kwargs`, the body stayed `None`, and an empty POST went out.

```
DirectOpenAI.chat.completions.create(...)  ->  400 "request body must be a JSON object"
the same payload via _Client.request(...)  ->  200, "PONG"
```

**This is the second bug of exactly this shape in that file.** The first was `get()` calling `client.post(...)`
while its own docstring said *"the one call that is not a POST"*, so `GET /models` went out as `POST /models`.

**Blast radius:** the NON-STREAMING path — which `/compact` and the memory-summary feature use. Streaming was
unaffected, which is why it survived: the common path worked. A gateway more forgiving than Netra would have
answered a different question than the one asked, **silently**.

### Bug 2 — `--vendor gemini` was rejected, so it could never have worked

`argparse_cli` advertises four vendors; `config.resolve_config` accepted two:

```
E  --vendor must be 'deepseek' or 'netra', not 'gemini'
```

Root cause is duplication: `switch_vendor` already handled all four, and its own docstring records that this
same duplication caused a dashboard/CLI disagreement before. The `--vendor` block now delegates.

### Verified end to end through the real CLI

| Provider | Result | Time | Credential |
|---|---|---|---|
| `netra` | `PONG` | 2.4s | `NETRA_API_KEY` |
| `gemini` | `PONG` | 4.4s | **gcloud login, no API key** |
| `netra` `/models` | 200, both models | — | — |
| `netra` non-streaming | `PONG` | 0.81s | usage in=12 out=3 |
| `netra` streaming | `PONG`, 5 chunks | 0.76s | — |
| Vertex `generateContent` | `PONG` | 2.23s | 129 thinking tokens |

**Note on Gemini token budget:** with `maxOutputTokens: 32` the reply was *empty* while `thoughtsTokenCount`
was 28 — Gemini 3.8 spends tokens reasoning before it answers. A small output budget produces a blank response
that looks like a failure and is not one.
