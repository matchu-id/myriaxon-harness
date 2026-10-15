# PlanToL5 — Myriaxon: Gap Closure to Level 5

**Target:** `C:\Users\arian\OneDrive\ドキュメント\Sandbox\Myriaxon-Harness-Latest`
**Version at time of writing:** 1.2.0
**Date:** 13 October 2026
**Basis:** a submitted gap inventory (23 claims) **verified against the source**, plus capabilities
shipped and measured on 13 October 2026.

> **Every claim in this document is marked with how it was established.** A plan that asserts a gap
> that does not exist wastes the same sessions as a plan that misses one, and the first is harder to
> notice. Six of the twenty-three original claims were wrong or overstated; they are recorded as
> corrections rather than quietly dropped, because the corrections are the most useful part of the
> audit.

---

## 0. How to read this

| Mark | Meaning |
|---|---|
| **VERIFIED** | Established by reading the source or running the code, with a `file:line` or a measurement |
| **CLAIMED** | Asserted in the submitted inventory; not independently confirmed |
| **CORRECTED** | The claim was checked and found wrong, or narrower than stated |
| ✅ | Already built and demonstrated |
| ⬜ | To be built |

---

## 1. Executive summary

### 1.1 What the audit found

The submitted inventory listed **23 gaps**. Checking each against the code:

| Outcome | Count | Meaning |
|---|---|---|
| **Already built** | **3** | The feature exists and works |
| **Overstated** | **3** | Something exists; the gap is narrower than claimed |
| **Confirmed** | **17** | Genuinely absent |

**And the inventory omitted six capabilities shipped the same day** — three of which address
higher-severity findings than anything on the list.

### 1.2 The finding that matters most

> **Myriaxon's system prompt measures 1,532 tokens. Vertex AI will not cache a prefix below 4,096.**

Every competing harness ships 10,000–40,000 tokens of prompt scaffolding. Myriaxon is **2.5× under the
caching floor** — it has an unusually lean prompt *and* cannot use the caching that would reward it.
That is not a feature gap; it is a **positioning gap**. *Cheap, fast, cacheable* is a claim the others
cannot make, and this harness is already built for it.

### 1.3 What the plan changes about the submitted inventory

| Change | Reason |
|---|---|
| **Document extraction promoted to Phase 1** | `pandoc` is already in the sandbox; highest payoff per session |
| **Event triggers narrowed** | Poll-based file conditions already exist; the gap is *event-driven*, not *triggered* |
| **Failover narrowed** | One-hop same-provider failover is implemented; the gap is *cross-provider chains* |
| **Browser re-scoped to a Node sidecar** | Do not rewrite 57,000 lines of Python for one subsystem |
| **A spend guardrail added to every trigger** | Schedulers, webhooks and watchers all start paid turns |
| **A measurement phase added before any building** | Six of 23 claims were wrong; measure first |

---

## 2. Current state — measured, not assumed

### 2.1 Harness size

| | |
|---|---|
| Python source | **57,252 lines**, 141 files, 2,981 KB |
| Tests | **9,475 lines**, 50 files |
| Test status | full suite green |
| Dependencies (core) | `anthropic`, `rich`, `pygments`, `prompt_toolkit` — **HTTP is stdlib** |

### 2.2 Timing — where a turn's time actually goes

| Operation | Measured |
|---|---|
| Model call (Gemini/Vertex) | **4.9 s – 12.4 s** |
| Full agent turn | 20 – 25 s |
| File read | 0.2 s |
| Agent startup | 0.66 s |
| Dashboard resident | 40 MB |
| `import openai` (**avoided**) | 43.9 s |
| `import httpx` (**avoided**) | 1.0 s |

**Conclusion, and the plan is built on it:** the harness is **I/O-bound on the model API**. Local
operations are already sub-second. No language or runtime change alters the 95 %. Optimisation effort
belongs in *context efficiency* and *round-trip count*, not in throughput.

### 2.3 Sandbox capabilities — probed live inside the container

**Present:** `pandoc` · `python3` · `node` · `npm` · `sqlite3` · `dot` · `convert` · `git` · `gcc/g++` ·
`make` · `PIL` · `pandas` · `numpy` · `matplotlib`

**Missing:** `pdftotext` · `pdftoppm` · `soffice` · `rg` · `python-docx` · `openpyxl` · `python-pptx` ·
`pypdf`

**Python stdlib confirmed usable for Office parsing:** `zipfile` · `xml.etree.ElementTree` · `csv` ·
`json` · `sqlite3` · `html.parser`

> A `.docx`, `.xlsx` or `.pptx` is a ZIP of XML. **Reading them needs no third-party library** — this was
> demonstrated on 13 October by reading a 586 KB assessment report with `zipfile` alone.

### 2.4 Shipped on 13 October 2026

| Capability | Commit | Evidence |
|---|---|---|
| **Failure circuit breaker** | `55711a3` | A repeated identical failure stops the turn at attempt **2** (was: invisible until 10) |
| **Answer-first discipline** | `55711a3` | System prompt forbids investigating an answerable question |
| **Actionable timeout message** | `55711a3` | Distinguishes "needs longer" from "never finishes" |
| **Durable execution** | `97cea8c` | Real `kill -9` → `step 4` → resume proven |
| **Exactly-once side effects** | `97cea8c` | `messaging.send` skips a repeat within a run |
| **Pure-Python self-scheduling** | `1ff5cde` | Supervisor thread; no OS scheduler |
| **Environment manifest** | `0b8eeb9` | Agent is told its own sandbox OS, shell, network state |
| **Sandbox image enrichment** | `0b8eeb9` | 32/51 host programs (was 22/51); 10/10 capabilities verified by *doing the work* |
| **Vertex/Gemini, no API key** | `34d8aff` | `gemini-3.8-flash` answering via the gcloud login |
| **Context cache + accounting** | `3e2cc3c` | **100 % hit** verified; `cache_fields` now reads Gemini's field |
| **`/models` GET fix** | `753723c` | Was POSTing; netra 0 → 2 models, ollama 0 → 4 |

---

## 3. Verified gap register

### 3.1 CORRECTED — claimed absent, actually present

| # | Claim | Verdict | Evidence |
|---|---|---|---|
| C1 | *No inbound webhook listener* | ❌ **False** | `myriaxon/bot.py`; `dashboard/server.py:1281` routes `/api/inbound`; shared secret, sender allowlist, Twilio/Meta/generic payload parsing |
| C2 | *No model cost / token hard-cap* | ❌ **False** | `myriaxon/run_budget.py` — `DEFAULT_MAX_STEPS=120`, `DEFAULT_MAX_SECONDS=3600.0`, `DEFAULT_MAX_TOKENS=2_000_000`; enforced on every provider |
| C3 | *No Linux namespaces / container jail* | ❌ **False** | `docker/Dockerfile`; `--network none`; `tools.py:622 sandbox_image()`; WSL discovery. Verified: workspace shared, network **blocked**, host filesystem invisible |
| C4 | *No filesystem file-watch trigger* | ⚠️ **Overstated** | `conditions.py:498` `KIND_FILE_NEWER` compares `st_mtime`. **Poll-based, not event-based** |
| C5 | *No automatic runtime provider failover* | ⚠️ **Overstated** | `cli.py:1031 _try_fallback` — swaps model on rate-limit, discards partial messages, retries. **One hop, same provider only** |
| C6 | *No native PDF / Office parsing tool* | ⚠️ **Narrower** | No *tool*, but `pandoc` **is** in the sandbox and `zipfile` reads Office natively. The gap is **`pdftotext` is missing from the image** |

### 3.2 CONFIRMED — genuinely absent

| # | Gap | Area | Severity |
|---|---|---|---|
| G1 | `pdftotext` / `pdftoppm` not in the sandbox image | Documents | **High** |
| G2 | No `doc_read` tool — `.docx/.xlsx/.pptx` → markdown | Documents | **High** |
| G3 | No LSP client (stdio JSON-RPC) | Code intelligence | **High** |
| G4 | No `lsp_diagnostics` — errors before running tests | Code intelligence | **High** |
| G5 | No `lsp_goto_definition` | Code intelligence | Medium |
| G6 | No `lsp_find_references` | Code intelligence | Medium |
| G7 | No `lsp_hover` | Code intelligence | Low |
| G8 | No ACP server (`--acp`) — cannot be an IDE backend | Interop | **High** |
| G9 | No ACP client bridge — cannot delegate to ACP agents | Interop | Medium |
| G10 | No A2A agent-card federation | Interop | Low |
| G11 | No event-driven file watcher (only polling) | Reactive | Medium |
| G12 | No OS / system-event triggers | Reactive | Low |
| G13 | Cross-provider failover chains | Resilience | Medium |
| G14 | No interactive PTY — pipes only, no ANSI/TUI | Terminal | Medium |
| G15 | No named multi-session terminal multiplexing | Terminal | Medium |
| G16 | No headless browser (CDP/Playwright) | Web | **High** |
| G17 | No Windows ACL sandbox isolation | Isolation | Medium |

### 3.3 NOT IN THE INVENTORY — omitted but higher-severity

| # | Item | Status |
|---|---|---|
| N1 | **No spend guardrail on autonomous triggers** — schedulers, webhooks and watchers all start *paid* turns | ⬜ **build** |
| N2 | **1,532-token prompt is 2.5× under Vertex's 4,096 caching floor** | ⬜ investigate |
| N3 | **No measured capability probe** — the plan itself was built on unverified claims | ⬜ **build first** |
| N4 | Rewind history grows unbounded (2.9 MB across 2 sessions, found by measurement) | ⬜ retention |
| N5 | No browser → blocks every "operate a real website" capability | = G16 |
| N6 | Test-suite flake: `test_ollama_wiring.py` fails 1–2 tests per full run, order-dependent | ⬜ known, open |

---

## 4. Guiding constraints

These are **not negotiable**, because each has already been paid for:

1. **Pure Python, zero build step.** The standard library is the HTTP client, the ZIP reader, the XML
   parser, the JSON log. Adding a dependency requires an argument that a stdlib path cannot make.
2. **Cross-platform, no OS lock-in.** Self-scheduling is a Python thread *because* `schtasks` is
   Windows-only. Any new subsystem must work on Windows, macOS and Linux, or be explicitly optional.
3. **Isolation is not traded for capability.** An autonomy level bought by removing a boundary is not
   counted. Network stays off; confinement stays enforced.
4. **Every autonomous trigger carries a budget.** Time, tokens, and a rate cap.
5. **Measure before building.** See N3.

---

## 5. Priorities — capability per session

The submitted plan optimises for *"match DeepSeek Harness."* This plan optimises for **capability
delivered per session**, because the phases are budgeted in sessions and the ordering is free.

| Rank | Item | Sessions | Why here |
|---|---|---|---|
| 0 | **Measurement probe** | 1 | 6 of 23 claims were wrong. Verify the next 17 the same way |
| 1 | **Document extraction** (G1, G2) | 2 | `pandoc` present; Office needs only `zipfile`; proven need |
| 2 | **LSP diagnostics** (G3, G4) | 3 | Knowing code is broken before running it changes agent behaviour |
| 3 | **LSP full suite** (G5–G7) | 2 | Completes the code-intelligence story |
| 4 | **Webhook → locus wake** | 1 | `/api/inbound` exists; wiring only |
| 5 | **Event file watcher** (G11) + guardrail | 2 | Polling exists; event-driven is the gap |
| 6 | **Spend guardrail on all triggers** (N1) | 1 | Prerequisite for 4, 5 and 7 |
| 7 | **Cross-provider failover** (G13) | 3 | One-hop exists; chains are the gap |
| 8 | **ACP server** (G8) | 3 | IDE backend; self-contained |
| 9 | **Multi-session PTY** (G14, G15) | 5 | Real work (ConPTY on Windows) |
| 10 | **Browser via Node sidecar** (G16) | 4 | Do **not** rewrite Python for this |
| 11 | **Windows ACL sandbox** (G17) | 4 | Isolation depth |
| 12 | **ACP client, A2A, system events** (G9, G10, G12) | 4 | Lower value; do last |

**Total: ≈ 35 sessions.**

---

## 6. Phase 0 — Measure first (1 session)

### Why

The inventory that prompted this plan contained **six wrong claims**. Each would have cost sessions
building something that already existed. The correction took minutes of reading. **Make that the first
step, not an afterthought.**

### Checklist

- [ ] **P0.1** Write `tools/capability_probe.py` — for each claimed gap, a check that decides
      present/absent **by executing**, not by grepping. Emit JSON.
- [ ] **P0.2** Re-probe all 17 confirmed gaps (G1–G17) and record the verdict with evidence.
- [ ] **P0.3** Probe the sandbox for each proposed new dependency (`pdftotext`, `bubblewrap`,
      `playwright`, language servers).
- [ ] **P0.4** Record the prompt token count and compare against Vertex's 4,096 caching floor (N2).
- [ ] **P0.5** Commit the probe and its output as `docs/CAPABILITY-PROBE.md`.
- [ ] **P0.6** Re-run the probe in CI so a regression that *removes* a capability is caught.

**Acceptance:** a single command produces a pass/fail table for all 17 gaps with the evidence inline,
and at least one claim is found wrong (proving the probe discriminates).

---

## 7. Phase 1 — Document extraction (2 sessions) ⬅ *highest payoff*

### Why first

Measured need, on 13 October: reading a 586 KB `.docx` took **five attempts** — `zipfile`, a
hand-written PDF extractor that produced `8L3DWK` instead of `UiPath` (a **+29 subset-font glyph
offset**), and a Word COM conversion that hung past ten minutes. Meanwhile `pandoc` sits unused in the
sandbox.

### 7.1 Bake the missing binaries

- [ ] **P1.1** Add to `docker/Dockerfile`: `poppler-utils` (provides `pdftotext`, `pdftoppm`)
- [ ] **P1.2** Add `tesseract-ocr` **only if** scanned-PDF OCR is required — it is large; decide first
- [ ] **P1.3** Rebuild `myriaxon-sandbox:latest`; re-run **Phase 0's probe** to confirm the additions
- [ ] **P1.4** Record the image size delta (the image is pulled on first use; size is a real cost)

### 7.2 The `doc_read` tool

- [ ] **P1.5** `myriaxon/doc_tools.py` — a single entry point: `doc_read(path, format="markdown")`
- [ ] **P1.6** **`.docx`** via `zipfile` + `xml.etree` → headings, paragraphs, **and tables as tables**
      (the table structure is what makes a finding record readable; flattening loses it)
- [ ] **P1.7** **`.xlsx`** via `zipfile` + `xml.etree` — shared strings, sheet names, cell grid
- [ ] **P1.8** **`.pptx`** via `zipfile` + `xml.etree` — slide order, plus **speaker notes** (often where
      the real content is)
- [ ] **P1.9** **`.pdf`** — prefer `pdftotext` **in the sandbox**; fall back to `pandoc`
- [ ] **P1.10** **`.csv` / `.tsv` / `.json` / `.md`** — read directly, no conversion
- [ ] **P1.11** Register in `tool_registry.py` with a description that names the supported extensions
- [ ] **P1.12** Bound the output: a 586 KB report is 2,886 lines. Truncate with a stated limit and a
      pointer to the spill file, never silently.

### 7.3 Address the root cause of the PDF failure

- [ ] **P1.13** Document **why** text extraction failed: subset fonts renumber glyphs and rely on a
      `/Differences` array. Record the `8L3DWK` measurement so nobody re-writes the broken extractor.
- [ ] **P1.14** Where `pdftotext` is unavailable, **fail with the diagnosis** rather than returning
      plausible-looking wrong text. *A checker that cannot find anything is evidence about the checker.*

### Acceptance criteria

- [ ] Reads the real 586 KB `UiPath Platform Assessment Report.docx` and returns its 31 findings'
      text with **table structure intact**
- [ ] Reads a `.xlsx` and a `.pptx` from the same workspace
- [ ] Each path has a test with a small fixture committed to the repo
- [ ] A missing tool produces an actionable message, not wrong text

---

## 8. Phase 2 — Code intelligence: LSP (5 sessions)

### Why

This is the plan's strongest original point and the largest genuine gap against DeepSeek Harness.
Regex search cannot answer *"where is this defined"* or *"what is the type here"*.

**Scope decision:** `lsp_diagnostics` alone delivers most of the value. Build it first and ship it,
rather than landing all five tools together.

### 8.1 The client

- [ ] **P2.1** `myriaxon/lsp/client.py` — stdio **JSON-RPC 2.0** over `subprocess` pipes, **zero
      dependency**
- [ ] **P2.2** Lifecycle: `initialize` → `initialized` → `textDocument/didOpen` → … → `shutdown`
- [ ] **P2.3** Content-Length framing — headers and body are separate reads; a naive `readline()` loop
      breaks on multi-byte content
- [ ] **P2.4** Request/response correlation by `id`; a timeout per request
- [ ] **P2.5** One server process **per workspace root**, cached, reused across turns
- [ ] **P2.6** Reap the child on exit — a leaked language server outlives the session
- [ ] **P2.7** **Never fail a turn** because LSP is unavailable: degrade to today's grep, and say so

### 8.2 Detection

- [ ] **P2.8** `myriaxon/lsp/detect.py` — map workspace markers to a server:
      `pyproject.toml`/`.py` → `pyright-langserver`; `package.json`/`tsconfig.json` →
      `typescript-language-server`; `Cargo.toml` → `rust-analyzer`; `go.mod` → `gopls`
- [ ] **P2.9** Probe `PATH` **before** spawning; report the install command when absent
- [ ] **P2.10** Honour a `MYRIAXON_LSP_DISABLE` escape hatch (the same pattern as the supervisor)

### 8.3 The tools

- [ ] **P2.11** `lsp_diagnostics(path)` — **build this first**. Returns real errors/warnings without
      compiling or running tests
- [ ] **P2.12** `lsp_goto_definition(path, line, character)` → file + position
- [ ] **P2.13** `lsp_find_references(path, line, character)` → all usages
- [ ] **P2.14** `lsp_hover(path, line, character)` → signature + docstring
- [ ] **P2.15** Positions are **0-based lines, UTF-16 code units** in LSP. Getting this wrong is a
      silent off-by-one that reads as "no results". Convert at the boundary and test it.
- [ ] **P2.16** Register all four in `tool_registry.py`; consider a deferred tool group so the
      catalogue does not grow for users who never open a code file

### Acceptance criteria

- [ ] `lsp_diagnostics` on a file with a deliberate type error returns it **without** running the code
- [ ] Works on Python and TypeScript in the same session
- [ ] With no language server installed: grep-based behaviour and a clear message
- [ ] No orphaned language-server processes after the session ends

---

## 9. Phase 3 — Reactive triggers (4 sessions)

### Why

Self-scheduling shipped (`1ff5cde`), so Loci wake on **time**. The gap is waking on **events**. Note
`conditions.py:498` already polls `st_mtime` — the missing part is event-driven latency, not triggering
itself (correction **C4**).

### 9.1 Spend guardrail — **do this first**

- [ ] **P3.1** Every autonomous trigger passes through one gate: time budget, token budget, **hourly
      rate cap**
- [ ] **P3.2** A trigger that exceeds its cap **disables itself and says so** in `loci --status`
- [ ] **P3.3** Read the existing `run_budget.py` limits rather than inventing a second scheme
- [ ] **P3.4** Log every trigger firing with its cost, into the existing `usage.jsonl` ledger

> **A file watcher that fires an agent turn on every save is unbounded spend.** Without P3.1–P3.4,
> Phases 3 and 4 build a machine that spends money unattended.

### 9.2 Webhook into a locus

- [ ] **P3.5** `POST /api/webhook/<locus-id>` on the existing dashboard server — wake one named locus
- [ ] **P3.6** Require a shared secret (**reuse** the `/api/inbound` pattern; do not invent a second)
- [ ] **P3.7** Map a payload to a goal text through a small template, with a size limit
- [ ] **P3.8** Always answer 200 with a JSON body — a provider retries a 5xx, which double-fires

### 9.3 Event file watcher

- [ ] **P3.9** `myriaxon/watchers/fs_watcher.py` — `watchdog` if available, **polling fallback
      otherwise** (keeps the zero-dependency rule intact)
- [ ] **P3.10** Debounce: one trigger per file per N seconds
- [ ] **P3.11** Glob include/exclude; ignore `.git/`, `node_modules/`, `__pycache__/`, editor temp files
- [ ] **P3.12** Opt-in, off by default, with the same `MYRIAXON_*` escape-hatch naming
- [ ] **P3.13** Routed through the P3.1 gate

### Acceptance criteria

- [ ] A webhook wakes the named locus and is visible in Mission Control
- [ ] A file save triggers **one** turn, not five (debounce proven by test)
- [ ] Exceeding the hourly cap disables the trigger and reports why
- [ ] A wrong shared secret is refused

---

## 10. Phase 4 — Resilience (3 sessions)

### Why

Correction **C5**: one-hop, same-provider failover exists (`cli.py:1031`). The gap is **cross-provider
chains** — `anthropic → deepseek → ollama` — which means swapping endpoint *and* credential, not just a
model name.

### Checklist

- [ ] **P4.1** `fallback_chain: list[str]` in config, replacing the single `fallback_model` (keep the
      old key working — a config change that breaks an existing file is a regression)
- [ ] **P4.2** Each hop resolves to a **complete** `Config`: provider, base_url, model, api_key
- [ ] **P4.3** Trigger on `429`, `503`, and connection errors — **reuse** `_rate_limit_errors()` rather
      than matching on strings
- [ ] **P4.4** Discard partial messages before retrying (**the existing implementation already does
      this — preserve it**)
- [ ] **P4.5** Bound the chain; a cycle (`a → b → a`) must terminate
- [ ] **P4.6** Report which hop answered, in the turn footer
- [ ] **P4.7** Per-hop cost attribution into the usage ledger
- [ ] **P4.8** Test each trigger class with a stubbed transport; no live calls in the suite

### Acceptance criteria

- [ ] A stubbed 429 on the primary reaches the secondary and the turn **succeeds**
- [ ] A chain that exhausts every hop reports the **original** error, not the last one
- [ ] A cyclic chain terminates

---

## 11. Phase 5 — Interop: ACP (3 sessions)

### Checklist

- [ ] **P5.1** `myriaxon --acp` — ACP over stdio JSON-RPC
- [ ] **P5.2** Expose: capabilities, session create, prompt, cancel, tool approval
- [ ] **P5.3** Map Myriaxon's permission modes onto ACP's approval flow — **do not** bypass the
      existing gate
- [ ] **P5.4** Streaming responses so an IDE shows tokens as they arrive
- [ ] **P5.5** Reject a method not supported, rather than hanging
- [ ] **P5.6** Verify against a real client (Zed or Neovim), not only a unit test
- [ ] **P5.7** `tool-acp-delegate` — the client side (lower priority)

### Acceptance criteria

- [ ] An external editor drives a Myriaxon turn end to end
- [ ] Approval prompts appear **in the editor**, and a refusal is honoured
- [ ] `--acp` never writes to stdout except protocol frames (a stray print corrupts the stream)

---

## 12. Phase 6 — Terminal multiplexing (5 sessions)

### Checklist

- [ ] **P6.1** `myriaxon/terminal/pty_session.py` — a real PTY
- [ ] **P6.2** Windows: **ConPTY** (`pywinpty` or the Win32 API). POSIX: `pty`/`os.openpty`
- [ ] **P6.3** Tools: `terminal_create`, `terminal_write`, `terminal_read`, `terminal_close`
- [ ] **P6.4** Named sessions — `term-1`, `term-2` — addressable across turns
- [ ] **P6.5** ANSI escape parsing: strip for the transcript, **preserve raw** for curses apps
- [ ] **P6.6** Resize support (`TIOCSWINSZ` / `ResizePseudoConsole`)
- [ ] **P6.7** Session lifetime tied to the agent run; a leaked PTY holds a shell open
- [ ] **P6.8** Sandbox interaction: a PTY **inside** the container, or refused with a clear reason
- [ ] **P6.9** Keep `run_bash` — a PTY is not a replacement for a one-shot command

### Acceptance criteria

- [ ] Drives an interactive `python` REPL and reads its output
- [ ] Runs a TUI app and captures a sane rendering
- [ ] Two named sessions are independent
- [ ] No orphaned shells after exit

---

## 13. Phase 7 — Browser via Node sidecar (4 sessions)

### Why a sidecar, not a rewrite

Verified earlier: the harness is I/O-bound, 57,252 lines, and Playwright's maturity is in Node.
`node` v24 is **already in the sandbox**. A sidecar closes the gap in a week; a rewrite costs a year and
risks the verified surface.

### Checklist

- [ ] **P7.1** `myriaxon/browser/sidecar.mjs` — a small Playwright driver speaking JSON over stdio
- [ ] **P7.2** Optional extra: `pip install -e ".[browser]"` (the extras pattern is already used)
- [ ] **P7.3** Tools: `browser_open`, `browser_click`, `browser_fill`, `browser_screenshot`,
      `browser_read`
- [ ] **P7.4** The sidecar lives **in the sandbox**; note that `--network none` makes a browser
      useless — decide explicitly whether browser work runs outside it, and record the trade
- [ ] **P7.5** Screenshot → a **file path** the model can read, not raw base64 in the transcript
- [ ] **P7.6** Bound page text; a news site is megabytes
- [ ] **P7.7** Refuse gracefully when Node or Playwright is absent

### Acceptance criteria

- [ ] Loads a page, clicks, fills, screenshots
- [ ] Executes page JavaScript and returns a value
- [ ] Absent Playwright → actionable message, no hang
- [ ] No orphaned browser processes

---

## 14. Phase 8 — Isolation depth (4 sessions)

- [ ] **P8.1** Windows ACL isolation (`dsh`'s `sandbox-windows-acl` equivalent), or an explicit written
      decision not to build it
- [ ] **P8.2** Linux: optional `bwrap` path alongside Docker
- [ ] **P8.3** Record **which** isolation is active in the environment manifest the agent receives
- [ ] **P8.4** A probe proving the jail holds: host fs invisible, network refused

---

## 15. Cross-cutting requirements

Applies to **every** phase above.

### 15.1 Safety

- [ ] **X1** Every autonomous trigger carries a budget and a rate cap (see P3.1)
- [ ] **X2** Every new subsystem has an environment escape hatch (`MYRIAXON_*_DISABLE`)
- [ ] **X3** No new subsystem may weaken confinement — mount flags and `--network none` stand
- [ ] **X4** One kill switch halts every autonomous mechanism

### 15.2 Verification

- [ ] **X5** Each deliverable has a test that **fails without the change** (verify by reverting)
- [ ] **X6** Prefer a test that *runs* the thing over a test that asserts a string is present
- [ ] **X7** Every new tool call has a real-run demonstration recorded in the commit message
- [ ] **X8** `tools/verify_names.py` and the full suite stay green
- [ ] **X9** Re-run the Phase 0 probe after every phase

### 15.3 Housekeeping

- [ ] **X10** Fix the `test_ollama_wiring.py` order-dependent flake (N6) — it has produced 1–2 failures
      per full run for three runs, each a different test
- [ ] **X11** Add retention for rewind history (N4) — 2.9 MB across two sessions, unbounded
- [ ] **X12** Decide the prompt-size question (N2): grow the prompt into cacheable territory, or record
      the decision to stay lean

---

## 16. Sequencing summary

| Phase | Item | Sessions | Depends on |
|---|---|---|---|
| **0** | Measurement probe | 1 | — |
| **1** | Document extraction | 2 | 0 |
| **2** | LSP client + diagnostics | 3 | 0 |
| **2b** | LSP full suite | 2 | 2 |
| **3** | Spend guardrail | 1 | 0 |
| **3b** | Webhook → locus | 1 | 3 |
| **3c** | Event file watcher | 2 | 3 |
| **4** | Cross-provider failover | 3 | — |
| **5** | ACP server | 3 | — |
| **6** | PTY multiplexing | 5 | — |
| **7** | Browser sidecar | 4 | — |
| **8** | Isolation depth | 4 | — |
| — | ACP client, A2A, system events | 4 | 5 |
| | **Total** | **≈ 35** | |

### Critical path

```
Phase 0 ──► Phase 1 ──► Phase 2 ──► Phase 2b
    └─────► Phase 3 ──► Phase 3b
                     └─► Phase 3c
```

Phases 4–8 are **independent** and can run in any order, or in parallel if more than one person is
available.

---

## 17. What this plan deliberately does NOT do

| Rejected | Reason |
|---|---|
| **Rewrite in TypeScript** | 57,252 lines and 9,475 lines of tests, to optimise <5 % of a turn that is I/O-bound on a model API. Measured: local ops are already 0.2 s against a 4.9–12.4 s model call |
| **Chase Level 4 by removing boundaries** | Every competitor that reaches Level 4 does so partly by dropping the constraints that make unattended agents safe. The open position is Level 4 **with** them |
| **Add a dependency for what stdlib does** | `.docx` is a ZIP; the HTTP client is `http.client`; the JSON log is `json`. Removing the 43.9 s `import openai` was an architectural fix, not a language one |
| **Build all five LSP tools at once** | `lsp_diagnostics` carries most of the value. Ship it, then decide |
| **Replace `run_bash` with a PTY** | A PTY is for interactive sessions. One-shot commands should stay one-shot |

---

## 18. Definition of done — Level 5 readiness

A gap is closed only when **all** of these hold:

- [ ] The capability exists and is reachable from a real agent turn
- [ ] A test fails when the capability is removed (verified by reverting)
- [ ] It behaves correctly with the dependency absent
- [ ] It has an escape hatch
- [ ] Its cost is attributed in the usage ledger
- [ ] It appears in the environment manifest the agent reads
- [ ] Phase 0's probe reports it as present, with evidence

### The honest ceiling, restated

Requirements **5.4 (risk classification)** and **5.5 (declared uncertainty)** from the Level 5
requirements depend on **model judgement, not harness machinery**. A harness can *record* uncertainty
and *gate* on risk; it cannot make a model feel doubt.

**Achievable and worth having:** a confined harness that runs for hours, survives crashes, coordinates
through a board, reaches consensus, and escalates honestly — **with every boundary intact.**

**Not achievable by engineering:** an agent that knows what it does not know.

---

## 19. Appendix — evidence index

Every `file:line` and measurement cited above, so a reader can re-check rather than trust.

| Claim | Where to verify |
|---|---|
| Webhook listener exists | `myriaxon/bot.py`; `myriaxon/dashboard/server.py:1281` |
| Cost/token caps exist | `myriaxon/run_budget.py` (`DEFAULT_MAX_STEPS`, `_MAX_SECONDS`, `_MAX_TOKENS`) |
| Container jail exists | `docker/Dockerfile`; `myriaxon/tools.py:622` |
| File condition is poll-based | `myriaxon/conditions.py:498` (`st_mtime`) |
| One-hop failover exists | `myriaxon/cli.py:1031` (`_try_fallback`) |
| Failure breaker | `myriaxon/loop_detection.py` (`FAILURE_BREAKER_THRESHOLD`) |
| Timeout message | `myriaxon/tools.py` (`subprocess.TimeoutExpired` branch) |
| Answer-first rule | `myriaxon/system_prompt.py` (`"answer-first"` section) |
| Durable execution | `myriaxon/run_state.py` |
| Sandbox tooling | `docker/Dockerfile`; probe output in §2.3 |
| 1,532-token prompt | measured via `system_prompt.build_system_prompt` |
| Vertex caching floor 4,096 | Vertex API refusal message; `myriaxon/vertex_cache.py` |
| `import openai` 43.9 s | `myriaxon/cli.py` (`_make_openai_compat_client` docstring) |
| Dashboard 40 MB / agent 0.66 s | measured 13 Oct 2026 |
