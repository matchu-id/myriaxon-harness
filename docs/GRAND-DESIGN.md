# Myriaxon: Grand Design and Build Plan

**Status:** design document, written after a four-part code audit. Every claim about today's
code carries a `file:line` from that audit.

**Purpose:** a plan that reaches the target **without a runaway** — no unbounded scope, no phase
that cannot be finished, no work that cannot be abandoned cheaply.

---

## 0. Two different "100"s — pick one before reading further

This distinction is the single most important thing in the document, because conflating the two is
how projects die.

| | **Platform 100** | **Product 100** |
|---|---|---|
| What it is | Every capability works, singly-owned, on one machine | Multi-tenant, sellable, compliant |
| Audience | You | Paying customers in Indonesia |
| Includes | Agents, swarm, memory, sandbox, evals, governance | + auth, tenancy, billing, SOC 2, UU PDP, SLAs |
| Effort | **~10–14 focused weeks** | **~18+ months, 3–5 engineers** |
| Reachable by | You, with me | A funded team |

**The plan below targets Platform 100.** Product 100 is Phase 9, explicitly out of scope until you
have a reason to fund it. Writing a plan that pretends otherwise would waste your money.

---

## 1. Where we are: 25% of Platform 100

Measured against the audit, weighted by the target's needs — not by line count.

| # | Module | Now | Platform-100 target | Gap |
|---|---|---|---|---|
| 1 | Agent loop | **85%** | budget on all providers, parallel tools, arg errors | small |
| 2 | Model layer | **60%** | caching on all providers, usage persisted, fallback on outage | medium |
| 3 | Sandbox | **55%** | real image with python/git, configurable, egress allowlist | medium |
| 4 | Tools / MCP | **35%** | OAuth, connector rate limits, vault, useful connectors | **large** |
| 5 | Browser | **0%** | Playwright, sessions, screenshots, takeover | **large** |
| 6 | Memory | **55%** | vectors, delete API, per-swarm namespace | medium |
| 7 | **Swarm** | **5%** | shared substrate, board, consensus, collective memory | **largest** |
| 8 | Planning | **70%** | shared task board visible to all members | small |
| 9 | Skills | **75%** | numbered versions, org scope | small |
| 10 | Triggers | **50%** | cron parser, webhook fan-in, event bus | medium |
| 11 | **Durable execution** | **20%** | run checkpoints, resume mid-run, exactly-once effects | **large** |
| 12 | Approvals | **55%** | async delivery, escalation, hard limits | medium |
| 13 | Identity / secrets | **30%** | vault, short-lived creds, per-agent identity | **large** |
| 14 | Injection defence | **35%** | trust boundary, post-read restriction, egress | **large** |
| 15 | Cost controls | **45%** | persisted usage, $ estimates, alerts, swarm cap | medium |
| 16 | Evals | **80%** | online monitoring, red-team in CI | small |
| 17 | Observability | **55%** | immutable identity-scoped log *(partly done)*, replay | medium |
| 18 | Reliability | **40%** | breakers, dead-letter, degradation | medium |
| 19 | UI / channels | **40%** | takeover, mobile, chat channels | large |
| 20 | Admin | **15%** | policy console, inventory, dashboards | large |
| 21–23 | Tenancy / compliance / billing | **0%** | — | Product 100 only |

**Weighted total: ~44/100 raw, ~25% of Platform 100** once Module 7 (swarm) is weighted by its
centrality to *this* target rather than by inheritance from a generic checklist.

---

## 2. The anti-rabbit-hole contract

Five rules. They are the actual deliverable of this section — the phases are ordinary, these are not.

### Rule 1 — A phase is a walled garden
Work on the current phase **only**. New ideas go into `BACKLOG.md` with one line, not into code.
A backlog item may not be started, however small, however obviously correct.

### Rule 2 — No phase starts until the previous gate is green
A gate is: the phase's acceptance tests pass, `tools/verify_names.py` is 5/5, the full suite passes,
and the phase's stated "prove it" command has been **run**. Not reasoned about. Run.

### Rule 3 — Every change carries a failing test first
Demonstrated repeatedly in this codebase: five bugs were found by *running* rather than reading, and
each fix was proven by reverting it and watching the test fail. A test never seen failing is a test
nobody has checked.

### Rule 4 — Reversible decisions get a default; irreversible ones stop
| Reversible (pick a default, move on) | Irreversible (stop and ask) |
|---|---|
| Which sandbox image | Deleting or migrating user data |
| Queue implementation | Anything touching credentials already on disk |
| Vector store choice | Public API stability promises |
| Log format | Spending money |

### Rule 5 — Timebox, then decide
Each phase has a budget in *sessions*, not hours. On exhaustion: **stop, report what is done and what
is not, and let the owner decide to continue, shrink, or abandon.** No phase silently grows.

---

## 3. Module map and the connection order

The plan's core insight: **build in dependency order, and never build a consumer before its
producer exists.** Most failed agent platforms built the UI first.

```
                    ┌─────────────────────────────────────────┐
   PHASE 1          │  1. Agent loop  ──  2. Model layer       │   make ONE agent dependable
   (foundation)     │  15. Cost  ──  18. Reliability           │
                    └────────────────┬────────────────────────┘
                                     │  needs a dependable single agent
                    ┌────────────────▼────────────────────────┐
   PHASE 2          │  3. Sandbox (real image, egress)         │   give it a computer
   (capability)     │  13. Secrets vault                       │
                    └────────────────┬────────────────────────┘
                                     │  needs somewhere safe to act
                    ┌────────────────▼────────────────────────┐
   PHASE 3          │  4. Tools/MCP  ──  5. Browser            │   let it act in the world
   (reach)          │  14. Injection defence                   │
                    └────────────────┬────────────────────────┘
                                     │  needs real actions to coordinate around
                    ┌────────────────▼────────────────────────┐
   PHASE 4          │  7. SWARM: substrate → board → consensus │   ← the actual target
   (the swarm)      │  6. Memory (shared) ── 8. Planning       │
                    └────────────────┬────────────────────────┘
                                     │  a swarm that cannot survive a restart is a demo
                    ┌────────────────▼────────────────────────┐
   PHASE 5          │  11. Durable execution  ── 10. Triggers  │
   (survive)        │  12. Approvals                          │
                    └────────────────┬────────────────────────┘
                                     │  now make it trustworthy and observable
                    ┌────────────────▼────────────────────────┐
   PHASE 6          │  16. Evals  ── 17. Observability         │
   (trust)          │  19. UI  ── 20. Admin  ── 9. Skills      │
                    └─────────────────────────────────────────┘
                                     │
                    ┌────────────────▼────────────────────────┐
   PHASE 9          │  21–23. Tenancy, compliance, billing     │   ONLY with funding
   (product)        │  ← out of scope until then               │
                    └─────────────────────────────────────────┘
```

**Why this order:** each phase consumes the previous phase's guarantees. A swarm on an unreliable
loop multiplies bugs by N. Durable execution before the swarm is wasted — you would checkpoint a
design that is about to change.

---

## 4. The phases

Estimates are in **focused sessions** (one sitting, gate-driven), not calendar time.

### PHASE 1 — One dependable agent
**Sessions: 6–8. Gate: budget on all providers, parallel tools, no silent arg loss, usage on disk.**

| Task | Evidence it is needed | Done when |
|---|---|---|
| Budget on OpenAI-compatible + Gemini loops | `run_budget` had zero hits in `openai_agent.py` | ✅ done (`51c05ff`) |
| Usage persisted per run | `usage.py:86-88`, in-memory only, lost on exit | a JSONL record per run; a `--usage` report |
| Malformed tool args reported | `openai_agent.py:427-430` coerces to `{}` | a JSON error returns to the model |
| Parallel tool execution | `openai_agent.py:426-446` sequential | 3 calls in one response run concurrently |
| Configurable sandbox image | `tools.py` hardcodes `mcr.microsoft.com/powershell` | `--sandbox-image`, plus a Myriaxon image |
| Prompt caching on non-Anthropic | `agent.py:595-605` is Anthropic-only | markers sent when the provider supports them |

**Explicitly out of scope:** retries redesign, new providers, CLI cosmetics, the dashboard.
**Prove it:** run the same task with a deliberately tiny budget and show it stops cleanly and keeps the partial turn.

---

### PHASE 2 — Give it a computer
**Sessions: 5–7. Gate: a locus builds and runs code inside a confined, network-restricted container.**

| Task | Done when |
|---|---|
| Sandbox image with python, git, node | a confined locus runs `python3 -c "print(1)"` and `git init` |
| Egress allowlist | a domain allowlist permits exactly those hosts; everything else refused **(currently all-or-nothing)** |
| Secrets vault | credentials encrypted at rest, never in a plaintext JSON, never in container env |
| Per-agent identity | each locus has its own credential set, revocable |
| Resource limits | container memory/CPU/time caps; a runaway build cannot take the host |

**Explicitly out of scope:** browser (Phase 3), microVM isolation (a container is enough), Windows
Sandbox path.
**Prove it:** a confined locus clones nothing, writes a Python script, runs it, and its output appears
in the host workspace — with the network refused.

---

### PHASE 3 — Let it act in the world
**Sessions: 8–12. Gate: one real external system is read AND written, safely.**

| Task | Done when |
|---|---|
| OAuth + token refresh | a connector authenticates and refreshes without human help |
| Connector rate limits | 429 / `Retry-After` honoured per server |
| Trust boundary for tool output | untrusted content is marked and cannot issue instructions |
| Post-untrusted-read restriction | after fetching untrusted content, mutating tools are limited |
| First real connector **(pick ONE, finish it)** | e.g. email or a calendar — not five half-built |
| Browser (Playwright) **only if a target needs it** | navigate, read DOM, screenshot |

**Explicitly out of scope:** the full connector catalogue. One connector done properly beats six
started.
**Prove it:** an agent reads an email, drafts a reply, and the reply is NOT sent without approval —
verified by test, not by inspection.

---

### PHASE 4 — The swarm *(the actual target)*
**Sessions: 10–15. Gate: N loci self-organise on a shared board and produce a result no single one did.**

This phase is why the others exist. Its design is specified below in §5.

| Task | Done when |
|---|---|
| Swarm substrate | `shared/`, `members/<id>/`, `board/` zones; every member mounts all three, writes only its own + shared |
| Lease-based task board | work is **claimed**, not assigned; a dead member's lease expires and the task returns |
| Capability tags | a "designer" claims design work, a "coder" claims code work |
| Collective memory | a finding by one member is retrievable by all, with provenance |
| Consensus primitive | N members attempt independently; disagreement escalates to the owner |
| Spawn governance | a hard cap on members; a swarm-wide budget; one kill switch |
| Conflict handling | two members writing one artifact is detected, not silently interleaved |

**Explicitly out of scope:** learned routing, emergent goal invention at swarm level, cross-swarm
communication.
**Prove it:** one objective, three members with different capabilities, no human scheduling — and the
result is better than any single member's, demonstrated side by side.

---

### PHASE 5 — Survive
**Sessions: 8–10. Gate: kill the process mid-run; it resumes from a checkpoint without repeating side effects.**

| Task | Done when |
|---|---|
| Run checkpoints | a run's state is durable at each step boundary |
| Resume mid-run | a killed run continues from its last checkpoint |
| Exactly-once side effects | an outbound action has a dedupe key; a retry cannot double-send |
| Cron parser | `0 9 * * 1-5` works |
| Event triggers | a webhook starts the right agent with the right context |
| Async approvals | a pending approval reaches you (WhatsApp exists already) and escalates on timeout |

**Prove it:** `kill -9` mid-run, restart, and show the work continued and nothing was sent twice.

---

### PHASE 6 — Trust it
**Sessions: 6–8. Gate: a dashboard shows what each agent did and why, and the eval suite runs in CI.**

| Task | Done when |
|---|---|
| Immutable action log | append-only, identity-scoped *(identity + chain done: `3e21db6`)*, rotation |
| Cost in dollars | per-run and per-agent, from a dated price table (`bench/prices.py` already has one) |
| Red-team in CI | the existing `redteam/` suites run on every commit |
| Online monitoring | eval scoring runs against live traffic, not just benches |
| UI: pause / take over | stop and steer from the dashboard mid-turn |
| Admin: inventory, policy console | every agent, its capabilities, its spend, its last action |

**Prove it:** export an audit trail for one run and answer "what did it do, what did it cost, who authorised it".

---

## 5. The swarm design (Phase 4 in detail)

### 5.1 The forbidden shortcut
The obvious swarm design — let every agent share one directory — destroys the isolation that makes
unattended agents safe. The audit found I had already closed a hole where `run_bash` mounted the
**project root** into a confined locus's container. Reopening that to enable coordination would trade
a safety property for a feature.

### 5.2 The three-zone substrate
```
~/.myriaxon/swarms/<swarm-id>/
├── shared/          # read/write by all members: findings, artifacts, messages
├── members/<id>/    # read/write by that member ONLY: scratch, drafts
└── board/           # claims and leases; written only through the board API
```
Every member's container mounts the whole swarm root at `/swarm`, but **the mount is read-write only
for the member's own folder and `shared/`**; `board/` is written through the API, which enforces
lease ownership. Enforced where? The same place confinement is enforced today: the per-call argument
to `docker run`, generated from the member's identity. Not a convention — a mount flag.

### 5.3 The board protocol (stigmergy, not messaging)
A task is a file with a **lease**, reusing the primitive that already exists in `loci.py:657`:

```
board/tasks/<id>.json     {id, objective, capability, state, lease_owner, lease_expires, result}
```

- `state`: `open` → `claimed` → `done` | `blocked`
- **Claiming** is an atomic rename of the task file into `board/claims/<member>.json`. No central
  scheduler, no coordinator process. Whoever renames first wins — the filesystem is the lock.
- **A dead member's lease expires** and the task returns to `open`. This already works for loci leases
  and is the reason `lease` is the right primitive rather than a queue.
- **Communication is by artifact, not by message.** A member needing input writes
  `shared/requests/<id>.md`; another member claims it. No agent-to-agent RPC, no shared context
  window, no coordination server to fail.

### 5.4 Consensus (where "ultra intelligence" actually comes from)
Not one bigger model. **Independent attempts plus adjudication:**

1. A task may declare `quorum: N`, meaning N members attempt it independently.
2. Results land as `shared/attempts/<task>-<member>.md` — nobody sees another's attempt first.
3. Agreement: an exact or semantic match is accepted automatically.
4. Disagreement: escalate to the owner with all attempts side by side. **Never auto-pick.**

This is slower and more expensive than one agent, and it is the only mechanism in the design that
produces a result better than the best single member. It should be reserved for decisions that
deserve it, not used everywhere.

### 5.5 Governance, or this becomes a fork bomb with a credit card
| Control | Why |
|---|---|
| Hard member cap per swarm | spawning is the exponential risk |
| Swarm-wide budget, not per-member | N members × per-member budget is how you discover the limit from a bill |
| One kill switch halting all members | stopping six agents individually is not a control |
| Every action attributed to member **and** swarm | otherwise an audit trail cannot answer "which one" |
| No member may spawn a member | the spawning graph must be a tree of depth 2 |

---

## 6. Effort, honestly

| Phase | Sessions | Deliverable |
|---|---|---|
| 1 — dependable agent | 6–8 | (partly done) |
| 2 — sandbox | 5–7 | a real computer |
| 3 — reach | 8–12 | one real integration, safe |
| **4 — swarm** | **10–15** | **the target** |
| 5 — survive | 8–10 | crash-safe, exactly-once |
| 6 — trust | 6–8 | observable, admin-able |
| | **43–60 sessions** | **Platform 100** |

**If you can only do one thing:** Phase 1. It is small, it is mostly done, and every later phase
assumes it.

**If you want the swarm specifically:** do Phase 1, then go straight to Phase 4 with a *minimal*
Phase 2 (a sandbox image with python, nothing else). Reach and durability can follow the swarm — the
swarm is the differentiator, and the risk of building reach first is that you arrive at the swarm in
a year with no time left.

---

## 7. What this plan deliberately does not do

- **No dates.** Sessions are honest; calendar estimates for this kind of work are fiction.
- **No "AI-powered" anything.** Every mechanism above is deterministic and testable.
- **No phase that cannot be abandoned.** Each one leaves the system working; none is a bridge that
  must be finished once started.
- **No Product 100.** Tenancy, auth, billing and compliance are a separate project with a separate
  budget, and starting them early would consume the resources the swarm needs.
