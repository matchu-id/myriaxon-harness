# Loci: Requirements for Level 4 and Level 5 Autonomy

Scoped to the **isolated and safe** environment the owner chose: a confined workspace, a
network-isolated container, a budget ceiling, and a completion gate that keeps final authority with a
human. Every requirement below is written so that it **strengthens** those boundaries rather than
trading them away — an autonomy level bought by removing a boundary is not a level this document
counts.

Status is against the real code, with `file:line` where a thing exists.

| Mark | Meaning |
|---|---|
| **HAVE** | Present and exercised |
| **PART** | Present, incomplete for the requirement |
| **BUILD** | Absent; work required |

---

## Part 1 — What the framework asks for

**Level 4, High Autonomy:** *manages complex workflows, coordinates with other agents, revises its own
plans, and rarely needs a human.*

**Level 5, Full Autonomy:** *sets its own sub-goals and manages context, risk, and uncertainty within
defined boundaries.*

Decoded into testable requirements, Level 4 is five things and Level 5 is four more.

---

## Part 2 — Level 4 requirements (17)

### A. Durability — an agent that loses its work is not managing a workflow

| # | Requirement | Why | Status |
|---|---|---|---|
| 4.1 | **Run checkpoints at every step boundary** | A kill mid-run must not lose completed steps | **BUILD** — `checkpoints.py` is file-edit rollback, not run state |
| 4.2 | **Resume from the last checkpoint** | "Complex workflow" means longer than one process lifetime | **BUILD** |
| 4.3 | **Exactly-once for outbound side effects** | A resume must not re-send an email or re-charge | **BUILD** — no dedupe key anywhere |
| 4.4 | **Lease renewal independent of step progress** | A long tool call must not expire its own lease | **HAVE** — `loci.py:869` `_LeaseHeartbeat` |
| 4.5 | **Crash recovery without human action** | The next wake-up must continue, not restart | **PART** — lease expiry works; run state does not survive |

### B. Coordination — the criterion that actually defines Level 4

| # | Requirement | Why | Status |
|---|---|---|---|
| 4.6 | **Shared substrate** — `shared/`, `members/<id>/`, `board/` | Agents cannot coordinate while mutually invisible | **BUILD** — confinement currently makes them invisible by design |
| 4.7 | **Lease-based task board**, filesystem as the lock | Work must be claimed, not assigned, or a dead agent stalls the queue | **PART** — the lease primitive exists (`loci.py:657`); the board does not |
| 4.8 | **Capability tags** so a designer claims design work | Random assignment wastes the strongest agent on the wrong task | **BUILD** |
| 4.9 | **Collective memory** with provenance | A finding by one must be retrievable by all, and attributable | **PART** — `memory_store.py` is per-project, not per-swarm |
| 4.10 | **Consensus primitive** — N independent attempts, disagreement escalates | The only mechanism that produces a better answer than the best single agent | **BUILD** |
| 4.11 | **Conflict detection on shared artifacts** | Two agents writing one file must be detected, not interleaved | **BUILD** |
| 4.12 | **Per-member mount enforcement** | The boundary must be a mount flag, not a convention | **PART** — confinement is enforced in `_resolve`; per-member mounts do not exist |

### C. Self-revision — "revises its own plans"

| # | Requirement | Why | Status |
|---|---|---|---|
| 4.13 | **Re-plan mid-run when a step fails** | Measured today: a failed step ends the run | **PART** — `continuation_prompt` advances but does not re-plan |
| 4.14 | **Abandon a dead-end objective and record why** | Otherwise it retries a wall until the budget is gone | **PART** — `blocked` exists; choosing it early does not |
| 4.15 | **Add sub-objectives during a run** | A long task discovers work it could not know upfront | **HAVE** — `locus_goal_update` with `add` |

### D. Tooling resilience — "rarely needs a human"

| # | Requirement | Why | Status |
|---|---|---|---|
| 4.16 | **Circuit breaker per provider/tool** | A flapping dependency should be skipped, not retried forever | **BUILD** — `loop_detection.py:33-35` records that it is deliberately absent |
| 4.17 | **Dead-letter store for failed runs** | "What failed and why" must be answerable without reading logs | **PART** — consecutive-failure ledger only |

---

## Part 3 — Level 5 requirements (9)

Level 5 is not "more Level 4". It is a different claim: the agent **chooses what to care about** and
**judges its own risk**.

### A. Self-directed goal formation

| # | Requirement | Why | Status |
|---|---|---|---|
| 5.1 | **Decompose an objective into sub-goals itself** | Level 5 sets its own sub-goals, not just its own objectives | **PART** — can add flat objectives; cannot decompose |
| 5.2 | **Prioritise competing objectives by expected value** | With no human ranking them, something must | **BUILD** — `next_goal()` takes the first active goal |
| 5.3 | **Retire its own obsolete objectives** | Otherwise the list only grows and prioritisation decays | **PART** — cannot delete its own; deliberately |

### B. Risk and uncertainty management

| # | Requirement | Why | Status |
|---|---|---|---|
| 5.4 | **Classify an action's risk before taking it** | "Manages risk" requires knowing which actions are risky | **PART** — a regex heuristic, self-described as *"NOT a security boundary"* (`constants.py:417`) |
| 5.5 | **Detect and declare its own uncertainty** | An agent that cannot say "I am not sure" cannot manage uncertainty | **BUILD** |
| 5.6 | **Escalate on a risk threshold, not only on completion** | Level 3 escalates when done; Level 5 escalates when *unsure* | **PART** — `blocked` exists as a state; no threshold drives it |
| 5.7 | **Recognise when it is out of its depth and stop** | The honest failure mode | **BUILD** — measured today: it reasoned about a broken environment instead of testing it |

### C. Context stewardship

| # | Requirement | Why | Status |
|---|---|---|---|
| 5.8 | **Manage its own context budget deliberately** | Level 5 "manages context" rather than merely surviving it | **PART** — refuse → prune → compact (`model_adapt.py:315-372`) is automatic, not chosen |
| 5.9 | **Decide what to remember and what to forget** | Unbounded memory is a privacy liability and a retrieval tax | **BUILD** — memory is appended, never curated by the agent |

---

## Part 4 — What it can DO at each level

Written as observable capabilities, so a level claim can be tested rather than asserted.

### At Level 4, a locus can:

1. **Run a task that takes hours** and survive a restart mid-way without repeating side effects.
2. **Hand work to a peer locus** through a shared board, and pick up work a peer left when its lease
   expires.
3. **Ask a peer for something** by writing a request artifact rather than a message, so no coordination
   server exists to fail.
4. **Have N loci attempt the same question independently** and escalate only the disagreement —
   consensus as a capability, not a hope.
5. **Re-plan** when a step fails: retry differently, reorder, or abandon with a recorded reason.
6. **Use a peer's finding** without repeating the discovery.
7. **Detect that it and a peer are about to write the same file** and yield.

### At Level 5, a locus can:

8. **Break a goal into sub-goals it defines itself**, ordered by its own judgement of value.
9. **Decide a task is not worth doing** and say so, rather than completing it because it was listed.
10. **Say "I am not confident"** and stop, with the specific uncertainty named.
11. **Escalate because it judged the risk**, not because it finished or crashed.
12. **Curate its own memory** — keep what proved useful, let the rest expire.
13. **Choose when to spend context** on rereading versus proceeding on a summary.

### What it still cannot do at Level 5, by design

The framework itself says constraints remain at Level 5, and this architecture keeps four:

- **Cannot remove its own confinement.** The workspace boundary is a mount flag it cannot edit.
- **Cannot reach the network.** `--network none` is not a policy it can argue with.
- **Cannot mark its own objective done.** Completion authority stays human
  (`goal_authority.py:72`).
- **Cannot raise its own budget.** The ceiling is set outside the agent.

**These are the four that make Level 5 safe.** Removing any of them would raise the measured level
while lowering the thing that makes the level worth having.

---

## Part 5 — Build order, gates, and honest effort

Ordered so each step is verifiable and none is a bridge that must be finished once started.

| Phase | Builds | Gate | Sessions |
|---|---|---|---|
| **A. Durability** | 4.1, 4.2, 4.5 | `kill -9` mid-run, restart, work continues, nothing sent twice | 6–8 |
| **B. Substrate** | 4.6, 4.8, 4.12 | three loci, one objective, no human scheduling, better result than the best single one | 6–8 |
| **C. Board** | 4.7, 4.11 | a killed member's task returns to the queue and is claimed by another | 4–5 |
| **D. Collective memory** | 4.9 | a finding by one is retrieved by another with provenance intact | 3–4 |
| **E. Consensus** | 4.10 | 3 independent attempts; agreement accepted, disagreement escalated | 4–5 |
| **F. Side effects** | 4.3 | an outbound action has a dedupe key; a retry cannot double-send | 4–5 |
| **G. Resilience** | 4.13, 4.14, 4.16, 4.17 | a failing provider is skipped, not retried forever | 4–5 |
| | **→ Level 4** | | **31–40** |
| **H. Goal formation** | 5.1, 5.2, 5.3 | decomposes a goal into sub-goals, orders them, retires one | 5–6 |
| **I. Risk and uncertainty** | 5.4, 5.5, 5.6, 5.7 | declares uncertainty and stops without being told to | 5–7 |
| **J. Context stewardship** | 5.8, 5.9 | chooses what to remember and what to drop, with a reason | 4–5 |
| | **→ Level 5** | | **14–18** |
| | **Total** | | **45–58 sessions** |

### The prerequisite that is not in either list

**Execution must actually work first.** Every Level 4 mechanism assumes agents that can run commands.
Measured today: the sandbox flag never reached sub-agents, so every locus shell command was refused;
that is now fixed but has been exercised **once**. A swarm of agents that cannot run a command is a
swarm of Level 2 agents, multiplied.

**Gate zero: re-run the locus benchmark and confirm execution across all tasks before building Phase
A.** Without it, everything below is built on an untested floor.

---

## Part 6 — The safety envelope, stated explicitly

The owner chose *"full autonomous on its own isolated environment"*. This is what that means at each
level, so autonomy never silently becomes something else:

| Boundary | Level 4 | Level 5 | How it is enforced |
|---|---|---|---|
| Workspace confinement | unchanged | unchanged | mount flags + `tools._resolve` |
| Network isolation | unchanged | unchanged | `--network none` |
| Budget ceiling | swarm-wide, not per member | same | `run_budget.py` |
| Completion authority | human only | human only | `goal_authority.py:72` |
| Member count | hard cap | hard cap | swarm config |
| Kill switch | one control, all members | same | cancellation + leases |
| Audit | per member **and** swarm | same | `audit.py` identity + chain |
| Self-modification | cannot edit confinement, budget or caps | same | those live outside the workspace |

**The swarm rules that must hold from the first line of Phase B**, because retrofitting them is how
multi-agent systems become incidents:

1. No member may spawn a member. The spawning graph is a tree of depth 2.
2. A swarm-wide budget, never N × per-member.
3. One kill switch that halts every member.
4. Every action attributed to member **and** swarm, or the audit cannot answer "which one".
5. Consensus disagreement always escalates. Never auto-pick.

---

## Part 7 — The honest assessment

| | Now | After Level 4 work | After Level 5 work |
|---|---|---|---|
| Autonomy level | 3 (conditional) | **4 (high)** | **4.5–5, domain-limited** |
| Sessions | — | 31–40 | +14–18 |

**Why 4.5 and not a clean 5:** requirements 5.4 and 5.5 — risk classification and declared uncertainty
— depend on model judgement far more than on harness machinery. Every other Level 5 requirement is
engineering; those two are behaviour, and this benchmark already showed a model reasoning confidently
about an environment it had not tested. A harness can *record* uncertainty and *gate* on risk; it
cannot make a model feel doubt.

**What is genuinely achievable and worth having:** a confined swarm that runs for hours, survives
crashes, coordinates through a filesystem board, reaches consensus on hard questions, and escalates
honestly — with every boundary from the table above still in place.

**No shipped product is at Level 4 *with* these boundaries.** That is the open position, and it is
worth more than the level number.
