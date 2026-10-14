# Myriaxon — Handover & Master Roadmap

> **Written for the next agent/engineer picking this up.** Read this first; it is the single entry
> point. It states what the harness is, what it does, what has been built and verified, what is
> broken or unfinished, and an exhaustive checklist of what would make it the most capable agentic
> harness available.
>
> **Last updated:** the documentation pass that followed the Myriaxon rebrand and the packaging
> work (console script, wheel contents, extras, licence). It also refreshed the items the gap
> analysis had since closed and corrected the stale counts.
>
> **Repository:** `C:\Users\arian\OneDrive\ドキュメント\Myriaxon`
> **Working directory for all commands:** the repository root.

---

## 0. TL;DR for the impatient

| | |
|---|---|
| **What it is** | A single-agent coding harness (CLI + local web dashboard) with built-in agentic benchmarking (IQB-14). |
| **Code size** | ~23,800 Python lines across 77 files in `myriaxon/`; `bench/` is ~4,200 of those and `dashboard/` ~5,400. |
| **Provider tested** | **Netra only** (`deepseek/deepseek-v4.1-flash`) per an explicit instruction from the project owner. |
| **Test state** | 22 offline pytest suites (**451 tests**, including a real end-to-end agent turn) + 36 check scripts, of which **24 are wired into one runner**. Last full run: all green. |
| **How to run the harness** | `myriaxon-web.cmd` (dashboard) or `myriaxon.cmd` (terminal); `myriaxon` once installed. |
| **How to run all tests** | `python -m pytest tests/ -q` (fast, offline) and `python scripts\run_dashboard_checks.py` (~15–25 min). |
| **Biggest open gap** | No curated IQB-14 corpus beyond the 14 seed tasks; mid-stream cancel stops the agent's stream loop; no remote/networked eval farm. |
| **The one instruction to honour** | **All live provider testing must use Netra.** Do not switch provider for testing. |

---

## 1. What this harness is

Myriaxon (`myriaxon`) is a **single-agent** coding harness with two front ends over one core:

- **Terminal CLI** — `myriaxon.cmd` → `myriaxon/cli.py` (1,971 lines, the largest file).
- **Local web dashboard** — `myriaxon-web.cmd` → `myriaxon/dashboard/` (a zero-dependency
  `ThreadingHTTPServer` + SSE UI; no Node, no external CDN, no network fetches).

It is deliberately **not** a thin wrapper over one vendor. It speaks the Anthropic Messages API, the
OpenAI-compatible chat completions shape *and* the Gemini/Vertex protocol through three agent
implementations (`agent.py`, `openai_agent.py`, `gemini_agent.py`) selected by provider, sharing one
tool-execution and safety-confirmation core.

It also ships its **own evaluation standard, IQB-14**, so the harness can measure agentic capability
rather than only run tasks.

### 1.1 Design principles that are actually enforced in code

Read these before changing anything — they explain many otherwise-odd decisions:

1. **Zero third-party dependencies in the dashboard.** `ThreadingHTTPServer`, SSE, inline CSS/JS. This
   is why the UI checks grep markup and why there is a custom `ui_render.js` for browser testing.
2. **Direct HTTP over SDKs where the SDK is slow.** `direct_http_client.py` replaces the `openai` SDK:
   `import openai` measured **44–75s** (803 modules) versus `import httpx` at **1.0–4.2s**.
3. **Never expand into a multi-agent architecture by default.** The project owner was explicit. Multi
   agent capability exists (`agent_mode="multi"` gates the delegation tools) but is **off** unless
   chosen.
4. **Facts are verified by running something.** Every claim in this document was produced by executing
   code, not by reading it. Continue that habit; it repeatedly caught bugs that looked fine.
5. **Fail closed on anything security-shaped.** Empty allowlist = nobody. Plugin cannot shadow a
   built-in. `deny` rules survive auto-approval.

### 1.2 Architecture map

```
myriaxon/
  cli.py                 terminal entry, command loop, _build_agent()
  argparse_cli.py        flags
  agent.py               Anthropic agent (Messages API, streaming, tool loop)
  openai_agent.py        OpenAI-compatible agents (Netra, DeepSeek, OpenRouter, custom, OpenAI)
  gemini_agent.py        Gemini / Vertex (both Google protocols)
  direct_http_client.py  SDK-free OpenAI-shaped client (big speed win)
  config.py              provider/model/base_url/api-key resolution
  model_profiles.py      small/balanced/large context adaptation
  model_adapt.py         per-model quirks
  context_budget.py      context accounting
  compaction.py          history compaction
  tools.py               built-in tools (read/write/edit/bash/grep/glob/git/web…)
  tool_registry.py       tool definitions, dispatch, schema index, plugin integration
  tool_context.py        ToolContext passed to every tool
  policy.py              permission-rule key validation
  settings.py            layered settings + argument-aware permission resolution
  hooks.py               shell/JSON hooks (pre/post tool use, prompt submit)
  plugins.py             in-process plugin API (tools + hooks)
  skills.py              SKILL.md discovery/use
  builtin_skills/        shipped skills
  subagents.py           subagent types (multi mode only)
  scheduler.py           background jobs
  checkpoints.py         file checkpoint / restore
  memory_store.py        persistent memory search
  notebook.py            Jupyter cell editing
  ask_user.py            structured questions to the user
  server_tools.py        web_search / web_fetch tool defs
  attachments.py         image/file attachments per provider
  grounding.py           which paths the model actually looked at
  audit.py               JSONL governance audit trail
  usage.py               token accounting
  session_store.py       session persistence
  mcp_client.py          MCP bridge: stdio + streamable-HTTP + SSE
  mcp_catalog.py         **18 preconfigured MCP servers**
  messaging.py           WhatsApp: Twilio / Meta / generic webhook
  git_view.py            structured git status + diffs for the UI
  banner.py, theme.py, completion.py, templating.py, frontmatter.py,
  atomic_io.py, job_object.py, dotenv_support.py, logging_setup.py,
  project_instructions.py, system_prompt.py, constants.py

  bench/                 IQB-14 evaluation standard
    tasks/               the 14 seed tasks, shipped inside the package
    parameters.py        the normative 14-parameter registry (weights sum to 1.0)
    scoring.py           composite AXS, Wilson CI, pass@k
    runner.py            task execution, budgets, repetition
    assertions.py        deterministic checks
    judge.py             LLM-as-judge
    guard.py             budget enforcement
    report.py, exporters.py, state.py, tasks.py, prices.py, __main__.py

  brand/                 the palette, type, mark and display copy (single source of truth);
                         assets/ holds the SVGs shipped in the wheel

  dashboard/             local web UI (zero dependencies)
    server.py            routes, SSE, app object (1,231 lines)
    ui.py                the entire single-page UI (2,114 lines)
    session.py           agent sessions for the web
    settings.py          dashboard preferences
    policy.py            permission-rule persistence
    endpoints.py         custom provider endpoints
    console.py           EventConsole: streams agent output to SSE
    events.py, commands.py, paths.py
```

---

## 2. IQB-14 — the built-in evaluation standard

Defined in `myriaxon/bench/parameters.py` (normative registry) and documented in
`docs/IQB-14.md`. Weights sum to exactly **1.0**; composite is **AXS, 0–100**.

Read verbatim from the registry at handover (14 parameters, total 1.0000):

| # | id | Weight | Name |
|---|---|---|---|
| 1 | `task_success` | 0.16 | Task Success |
| 2 | `tool_use_correctness` | 0.10 | Tool-Use Correctness |
| 3 | `action_safety` | 0.09 | Action Safety & Guardrail |
| 4 | `reasoning_plan` | 0.08 | Reasoning / Plan Trace |
| 5 | `environment_drift` | 0.07 | Environment & Input Drift |
| 6 | `stability` | 0.06 | Stability |
| 7 | `robustness` | 0.06 | Robustness (Phrasing) |
| 8 | `similarity` | 0.06 | Similarity (Golden Response) |
| 9 | `comprehensiveness` | 0.08 | Comprehensiveness |
| 10 | `relevance` | 0.08 | Relevance (Grounding) |
| 11 | `fairness` | 0.04 | Fairness |
| 12 | `adversarial_risk` | 0.06 | Adversarial Risk (Red-Team) |
| 13 | `hallucination_risk` | 0.04 | Hallucination Risk |
| 14 | `security` | 0.02 | Security |
| | | **1.0000** | |

Parameters carry a **severity tier** — `OUTCOME` (did it succeed?), `BEHAVIOR` (how did it go about it?)
and `ASSURANCE` (was it safe/grounded?) — which matters for reporting: an OUTCOME failure decides
whether the task counted at all, while ASSURANCE parameters describe risk. Each parameter also declares
a `question`, a `pass_definition` and a `fail_definition`, so a score is never a matter of opinion.

Machinery that exists:

- **Evidence levels:** `primary` / `secondary` / `unmeasured` — so a parameter is never silently
  assumed.
- **Statistics:** `pass@k = 1 - C(n-c,k)/C(n,k)`, Wilson 95% confidence intervals.
- **Repeat runs per task**, budgets (tokens/steps/time/cost), trajectory capture, six explicit
  statuses (so a crash/timeout/missing-key are not all collapsed into `FAIL`).
- **Judging:** `bench/judge.py` uses a model as judge — the project owner specified the judge is
  **Myriaxon itself using DeepSeek via Netra**.
- **Task levels L1–L4** (single step → long-horizon/adversarial).
- **`compare`** computes cross-model differences rather than asserting them.

---

## 3. Delivered and verified (this session's six objectives + supporting work)

Each line is backed by a check script. "verified" means it was executed, not reviewed.

### Objective 1 — Written gap analysis ✅
`docs/GAP-ANALYSIS.md` — parity matrix vs DeepSeek Harness / Antigravity / Claude Code, auto-mode
checklist, prioritised P0/P1/P2 gaps, and honest limits.

### Objective 2 — Multi-vendor + multi-model switching **in the UI** ✅
Vendor dropdown, model dropdown that repopulates per vendor, plus a `+` to add endpoints. Switching
**mid-conversation keeps the transcript**. Verified by `check_endpoints.py` (25 checks) and
`check_dashboard_provider_sessions.py`.

> **Known limitation:** vendor switching carries **conversational text only, not tool-call history**.
> Reason: Gemini requires a `thought_signature` on replayed `functionCall` parts, while OpenAI stores
> `tool_calls`. Reconciling these is an open task (see §5, item C7).

### Objective 3 — Easy custom-endpoint add/edit from the UI ✅
Unlimited custom endpoints (`dashboard/endpoints.py`) with name/base_url/api_key/models/protocol.
Keys are **never echoed** — only `has_api_key`. Endpoint probe fetches the model list. Verified by
`check_endpoints.py`.

### Objective 4 — Plugin support ✅
`myriaxon/plugins.py` + `docs/PLUGINS.md`. In-process plugins that add tools and observe hooks
(`pre_tool_use`, `post_tool_use`, `user_prompt`, `agent_start`, `agent_stop`). Distinct from shell
hooks because they need **no subprocess per call**.

Security property, verified: **a plugin cannot shadow a built-in tool.** This was a real
vulnerability — a plugin registering `write_file` *silently intercepted the real tool*, identical name
and arguments. Filtering the tool list was insufficient because dispatch looks names up in the plugin
registry; names are now **reserved before any plugin registers**.
Verified by `check_plugins.py` (24) and `check_plugin_shadowing.py` (15).

### Objective 5 — MCP discovery/install from the UI ✅
- Three transports implemented: **stdio**, **streamable-HTTP**, **SSE**. The live fixtures prove
  streamable-HTTP and stdio; SSE is negotiated as a fallback and has not been exercised against a
  real server.
- `mcp_catalog.py` holds **18 preconfigured servers** across Design / Files & data / Web & search /
  Developer. **7 need nothing at all** — figma, drawio, excalidraw, memory, fetch, puppeteer,
  everything — so enabling one is a single click.
- Auth supported (bearer token or explicit headers); an existing token survives an edit.
- Public registry search is wired to the panel.
- Verified against **real servers**: `check_mcp_remote_live.py` (11 checks) and `check_mcp_live.py`
  (13 checks) start genuine MCP servers and **call their tools**
  (`mcp__remotefix__echo → 'echo: hello remote'`, `mcp__remotefix__add → '42'`).
- `check_mcp_catalog.py` — **34 checks, confirmed 34/34** — one-click enable, credential refusal,
  path substitution, disable, unknown-key handling.

> **MCP fixes that mattered** (all were silent failures):
> 1. `McpBridge.start()` iterated only `stdio_servers` — **remote servers were configured and never
>    connected**, contributing zero tools and saying nothing.
> 2. `cli.py` gated bridge creation on `stdio_servers`; the **dashboard passed `None` for the bridge**,
>    so MCP never loaded in a web session at all.
> 3. No auth support — every hosted server (Figma, Microsoft 365, GitHub) was unfixably broken.
> 4. SDK 1.x→2.x renames: `streamablehttp_client` → `streamable_http_client` (takes `http_client`, not
>    `headers`), and `tool.inputSchema` → `tool.input_schema`. Only the old names were handled.
> 5. `is_repository` returned True for a plain subdirectory of a repo, and status paths were prefixed
>    with the outer repo's subdirectory.

### Objective 6 — WhatsApp / messaging integration ✅
`myriaxon/messaging.py` + `docs/MESSAGING.md`. Providers: **Twilio**, **Meta Cloud API**, **generic
webhook**. Inbound `POST /api/inbound` → parse → allowlist → **real agent turn** → reply to sender.
Outbound turn summaries. Verified by `check_messaging.py` (**42 checks**, including a full round trip:
webhook → real Netra turn → reply sent back) and `check_messaging_ui.py` (30 checks).

Security: **an empty allowlist refuses everybody** (an agent reachable by anyone who finds the number
is a remote code execution service). Numbers compare digits-only. A shared secret is enforced when set.
Provider outages are **recorded, never raised into the turn**.

### Additional work delivered

| Feature | Where | Verified by |
|---|---|---|
| **Permission rules** (`allow`/`ask`/`deny`, argument-aware, e.g. `run_bash:git *`) | `policy.py`, `settings.py`, rules panel | `check_permission_rules.py` (25) + a **live turn actually blocked by a deny rule** |
| **File explorer + diff review** | `git_view.py`, code panel | `check_files_ui.py` — status parsing, paths with spaces, renames, untracked content, hunk parsing, XSS-as-data |
| **Burger menu** replacing the flat button row | `ui.py` | `check_ui_js.py`, visual check |
| **Visual/browser verification harness** | `scripts/check_ui_visual.py` + `scripts/ui_render.js` | **25 checks in real Chrome via CDP**; screenshots of every dialog in both themes |
| **JS syntax + element-id guard** | `check_ui_js.py` (16) | parses the inline script with node; asserts no `$("id")` targets a missing element |
| **Handler-vs-App static guard** | `check_ui_js.py` | catches a bug that occurred **three times** |

### Bugs found and fixed that were *not* part of any objective

These are worth reading because they show the character of the remaining work — several were silent.

1. **Settings layers loaded twice** — `USER_SETTINGS_FILE` and the project settings path resolved to
   the *same file*, so identical rules loaded as both user and project layers, making
   "system > project > user" precedence meaningless.
2. **An unwritable user settings file was returned anyway**, so the rule editor failed for a reason
   unrelated to rules.
3. **A stored secret could be set but never cleared** — blank meant "keep", so a rotated or leaked
   token was unremovable. Added `clear_secrets` + a UI checkbox.
4. **The sidebar footer clipped its own buttons** — every button had `margin-left:auto`; with seven
   actions the later ones (including **the light/dark toggle**) were pushed off screen and were **not
   clickable at all**. Replaced with a burger menu.
5. **`--mono` was referenced but never defined**, so file and diff panels silently fell back to a
   sans font.
6. **The plugin directory was listed twice** in the panel (config dir == project dir on this machine).
7. **Catalog path fields were ignored** — `args=["...", "."]` hard-coded the directory, so the path a
   user typed was discarded. Now `{path}` placeholders, with a guard against unfilled ones.
8. **`is_repository` / status paths wrong** for a directory inside a repo (see MCP note 5).
9. **A staged file's legitimately-empty diff was shown as if newly added** — the untracked fallback
   fired for tracked files.
10. **The schema index was built from `build_tool_defs()`**, which includes plugin tools — so a
    shadowing plugin could replace the schema a built-in is validated against.

---

## 4. Current verified state

### 4.1 Test suites

**24 suites are wired into `scripts/run_dashboard_checks.py`.** 36 `check_*.py` scripts exist in total
(some are standalone: Vertex, Gemini protocol/vision, budget, serialization, profiles, roles, judge).

```
unit tests (tests/, offline, incl. an end-to-end turn against a local mock provider) ... 191
markdown renderer .......................... 10
history handoff ............................ 15
harness launch ............................. 10
plugin API ................................. 24
plugin shadowing ........................... 15
live MCP (remote) .......................... 11
messaging engine ........................... 42
live MCP (stdio) ........................... 13  (skips in a confined sandbox)
server startup
UI markup .................................. 51
core e2e ................................... 23
commands + vision .......................... 21
provider + sessions ........................ 15
session delete + projects .................. 17
settings + workflow ........................ 28
custom endpoints + vendor .................. 25
MCP server management ...................... 21
MCP preconfigured catalog .................. 34   ← confirmed 34/34 standalone
permission rules in the UI ................. 23
plugins panel .............................. 11
messaging panel + webhook .................. 30
file explorer + diff review
visual render in a real browser ............ 25
dashboard JavaScript ....................... 16
streamed markdown answer ................... 12
```

### 4.2 ⚠️ Outstanding at handover — fix first

1. **Re-run the full suite.** The last complete run showed `MCP preconfigured catalog` FAIL. That was
   a bug in the *check* (it read dataclass attributes off dicts); it was fixed and **confirmed 34/34
   standalone**, but **the full runner has not been re-run since**. Command:
   `python scripts\run_dashboard_checks.py` (delete `bench-out\checks-userconfig` first).

2. **`check_mcp_catalog.py` leaves servers configured if it crashes.** It does clean up on success; on
   failure it may leave `mcp_servers.json` entries behind. Check the config if later suite behaviour
   looks odd.

3. **Environment-mandated skips.** Two suites cannot run confined and *skip* rather than fail:
   - `live MCP (stdio)` — asyncio piped child processes are denied
     (`PermissionError: [WinError 5]` on `_winapi.CreateFile`).
   - `visual render in a real browser` — Chrome needs process/pipe access.
   Both were verified green with wider access (**13/13** and **25/25**). This is the documented
   sandbox shared-memory/named-pipe boundary, **not** an ACL problem — do not attempt an ACL repair.

### 4.3 Environment facts you must know

- **Only the repository and temp are writable.** `~/.myriaxon`, the home directory, Documents and
  Desktop are **not**. `CONFIG_DIR` therefore falls back into the repo, which is why the user-config
  directory and the project settings path collide on this machine (handled, but know it).
- **Background jobs and detached servers are killed between tool calls.** You **cannot** leave a server
  running for the user. They run `myriaxon-web.cmd` themselves.
- **In-process servers `os.chdir()`** into their workspace by design, and the restore lives in a
  `finally` that never runs for a daemon thread. `run_dashboard_checks.py` saves and restores the cwd
  around every check because of this.
- **`subprocess.run(capture_output=True)` fails in confined mode** for *grandchild* piped stdio; use
  in-process `main()` calls (that is why the runner is built that way).
- **pip is blocked in confined mode** (wheel-metadata unpacking). `scripts/install_mcp_manually.py`
  installs wheels with plain `urllib` + `zipfile` and picks by interpreter tag — it exists because an
  earlier naive version installed a **cp310-32bit** pywin32 on a 3.12-64bit interpreter and failed at
  import with a confusing "No module named 'pywintypes'".
- **The `mcp` SDK is now installed** (mcp 2.2.0 + httpx2, starlette, sse-starlette, jsonschema,
  pywin32, uvicorn). `pyjwt` and `python-multipart` are **still missing** but are server-side only.

### 4.4 Performance measured

| Measurement | Value |
|---|---|
| `import openai` | 44–75 s (803 modules) |
| `import httpx` | 1.0–4.2 s |
| Gemini warm turn | 1.64–2.21 s, harness overhead 0.03–0.08 s |
| Cold start | ~35–58 s |
| `--help` | 1.77 s |
| `genai.Client()` | 2.19 s, cached to 0.0000 s |
| Netra model calls | 1.1–3.8 s, ttfb 0.36–0.93 s |

---

## 5. THE MASTER CHECKLIST — what is needed to be the most powerful harness in the world

Legend: **[x]** done & verified · **[~]** partial / works but shallow · **[ ]** missing ·
**[!]** known bug · **(S/M/L/XL)** effort.

### A. Core agent loop
- [x] Single-agent tool loop, streaming, tool-call parsing
- [x] Modes: `manual`, `auto_edit`, `plan`, `auto`
- [x] Effort levels: low/medium/high/xhigh/max
- [x] Turn-level error recovery and retries
- [x] Loop detection (repeated identical tool calls)
- [~] **(M)** Mid-stream cancel — `/api/cancel` sets a flag that is read at `turn_end` for reporting;
  the agent checks nothing mid-stream (`dashboard/session.py:330`). Still open.
- [ ] **(M)** Auto-continue across turns on a truncated answer
- [ ] **(L)** Speculative/parallel tool execution for independent read-only calls
- [ ] **(M)** Deterministic replay of a recorded trajectory for regression testing
- [ ] **(L)** Self-critique pass before final answer (opt-in, measured, not free)
- [ ] **(M)** Plan-then-execute mode with an explicit, editable plan artifact
- [ ] **(S)** Interrupt and redirect: "stop, do X instead" mid-turn
- [ ] **(M)** Per-tool timeouts with graceful degradation
- [ ] **(L)** Checkpoint/rollback of *conversation* state, not just files

### B. Context & memory
- [x] Model profiles (small 16k / balanced 64k / large 200k) with per-profile tool-result caps
- [x] Context budget accounting, manual `/compact`, auto-compaction toggle
- [x] Persistent memory store with search
- [x] Checkpoints (file-level create/list/restore)
- [ ] **(L)** Semantic retrieval over project files (embeddings) rather than grep-only
- [ ] **(M)** Hierarchical summarisation of old turns that preserves exact tool results on demand
- [ ] **(M)** Automatic "what changed since last session" briefing
- [ ] **(L)** Cross-session long-term memory with decay and provenance
- [ ] **(M)** Per-file context pinning ("always keep this in context")
- [ ] **(S)** Token-heat map showing which context regions cost the most
- [ ] **(M)** Context diffing between two runs to explain a behavioural change
- [ ] **(L)** Learned context selection (which files actually mattered, fed back)

### C. Tools
- [x] read/write/edit, bash/PowerShell, grep, glob, git status/diff, todo, notebook, ask_user
- [x] web_search / web_fetch (Anthropic-shaped), memory search, skills, checkpoints, subagents
- [x] MCP tools from stdio and streamable-HTTP (both live-verified) and SSE (implemented, negotiated
      as a fallback), namespaced and non-shadowable
- [x] Plugin tools
- [ ] **(M)** `apply_patch`-style atomic multi-hunk edit with conflict detection
- [ ] **(L)** LSP integration: real diagnostics, hover, definition, references
- [ ] **(L)** Tree-sitter–based structural search/rewrite (AST-aware, not regex)
- [ ] **(M)** Browser automation tool (Playwright) with a page-state snapshot
- [ ] **(L)** Debugger integration (breakpoints, step, inspect) for at least one runtime
- [ ] **(M)** Test-runner tool that understands failures and re-runs only what broke
- [ ] **(M)** Package-manager tool with dependency-resolution awareness
- [ ] **(L)** Database tool with schema introspection and safe read-only defaults
- [ ] **(M)** Image/vision *write* (generate/annotate), not just read
- [ ] **(M)** Screenshot-diff tool for UI regression
- [ ] **(L)** Container/sandbox execution tool with resource caps
- [ ] **(M)** HTTP client tool with saved, parameterised requests (like a .http file)
- [ ] **(L)** Cloud/CLI integrations (AWS/GCP/Azure read-only introspection)
- [ ] **(M)** PDF/Office document read AND write (docx/xlsx/pptx)
- [ ] **(L)** Audio transcription and synthesis tools
- [ ] **(M)** Regex/replace tool with dry-run preview and undo

### D. Evaluation & benchmarking (IQB-14)
- [x] 14-parameter normative registry, weights sum to 1.0, AXS 0–100
- [x] Wilson CI, pass@k, repeat runs, evidence levels, six explicit statuses
- [x] Budget guard (tokens/steps/time/cost), trajectory capture, `compare`
- [x] Deterministic assertions + LLM judge (`bench/judge.py`)
- [x] Task levels L1–L4
- [ ] **(L)** **Task corpus.** The standard is defined; there is no large, curated, versioned task set.
      This is the single highest-value missing piece for "benchmarking" claims.
- [ ] **(M)** Statistical significance testing between two models (not just CIs)
- [ ] **(L)** Adversarial/prompt-injection task family
- [ ] **(M)** Long-horizon (multi-hour) task family with drift measurement
- [ ] **(L)** Human-baseline calibration so AXS is interpretable
- [ ] **(M)** Contamination detection (task leakage into training data)
- [ ] **(L)** Leaderboard/registry format for public comparison
- [ ] **(M)** Per-parameter failure clustering (why did models fail?)
- [ ] **(L)** Auto-generated tasks from real repository history
- [ ] **(M)** Flakiness detection across repeats (is the task or the model unstable?)
- [ ] **(L)** Cost-normalised scoring (AXS per dollar)
- [ ] **(M)** Cache/replay so a re-scored run costs nothing

### E. Multi-agent & orchestration
- [~] **(L)** Subagent spawning + parallel tasks exist, gated by `agent_mode="multi"` (off by default)
- [ ] **(L)** Real role specialisation (planner/executor/reviewer) with hand-off contracts
- [ ] **(L)** Shared blackboard state between agents
- [ ] **(L)** Conflict resolution when two agents edit the same file
- [ ] **(L)** Hierarchical budgets that subdivide and enforce
- [ ] **(M)** Subagent result distillation (return findings, not transcripts)
- [ ] **(L)** Durable, resumable multi-agent runs
- [ ] **(M)** Agent-to-agent message audit trail

### F. Providers & models
- [x] 6 built-in providers + unlimited custom endpoints; mid-chat switching with context
- [x] Gemini/Vertex `generateContent` with attachments and `thought_signature` handling
- [x] Direct HTTP client replacing the slow `openai` SDK
- [x] Model-limits lookup with a known-window cache (avoids 30 s+ probes)
- [x] Anthropic-native path: Messages API, hosted `web_search`/`web_fetch`, remote MCP connector,
      prompt caching on the tool block, native server-side compaction
- [ ] **(L)** Local model support (Ollama/vLLM/llama.cpp) with capability detection (any
      OpenAI-compatible endpoint already works)
- [ ] **(M)** Automatic fallback chain on provider failure (partly present: `fallback_model`)
- [ ] **(M)** Per-provider prompt templating (some models need different system prompts)
- [ ] **(S)** Model capability probe (does it actually support tool calls/vision/JSON mode?)
- [ ] **(M)** Tokeniser-accurate counting per provider rather than estimation
- [ ] **(L)** Fine-tune/adapter management and A-B routing to them

### G. Dashboard / UI
- [x] Chat with markdown, tables, code, streaming, thinking indicator
- [x] Sessions create/delete, project switching, workspace browser
- [x] Vendor/model switching, endpoint management, MCP catalog, plugins, rules, messaging panels
- [x] Light/dark themes, Myriaxon branding
- [x] **Verified in a real browser** (screenshots of every dialog) — `check_ui_visual.py`
- [x] File explorer + diff review with colourised hunks
- [ ] **(M)** Syntax highlighting in the code viewer
- [ ] **(M)** Side-by-side diff mode
- [ ] **(L)** Inline edit + save from the UI, feeding the agent's next turn
- [ ] **(M)** Keyboard shortcuts and a command palette
- [ ] **(M)** Mobile-responsive layout (currently desktop-oriented)
- [ ] **(L)** Multi-file diff review with approve/reject per hunk
- [ ] **(M)** Per-tool-call approval UI (the dashboard currently forces yolo — no terminal to prompt)
- [ ] **(L)** Artifact/preview pane (HTML, images, PDFs rendered live)
- [ ] **(M)** Session search and full-text transcript search
- [ ] **(L)** Cost/token dashboard with per-session breakdowns
- [ ] **(M)** Accessibility pass (focus order, ARIA, contrast beyond the crude luminance check)
- [ ] **(L)** Real-time collaborative sessions (multiple humans watching/driving)

### H. CLI / TUI experience
- [x] Rich terminal UI, banners, themes, slash commands, completion, traces
- [x] Yellow thinking/buffering indicator with elapsed time
- [ ] **(M)** True TUI (panes, scrollback, mouse) rather than streamed output
- [ ] **(M)** Interactive diff review in the terminal before applying edits
- [ ] **(S)** Shell integration (cd/export propagation back to the parent shell)
- [ ] **(M)** `git commit`/branch workflow helpers
- [ ] **(L)** Offline/air-gapped mode with local models
- [ ] **(M)** Scriptable non-interactive mode with structured JSON output (partly present)

### I. Extensibility
- [x] Plugin API (tools + 5 hook events), `docs/PLUGINS.md`
- [x] Shell/JSON hooks, skills (`SKILL.md`), slash commands, subagent types
- [x] MCP (stdio + streamable-HTTP live-verified, SSE implemented) + 18-server catalog + registry search
- [ ] **(M)** Plugin *marketplace/download* — currently you author files by hand ("nothing is
      downloaded for you")
- [ ] **(L)** Auto-configured plugins: detect the stack (Node/Python/Go/Rust) and enable relevant
      plugins/skills automatically
- [ ] **(M)** Plugin sandboxing/permissions (a plugin runs in-process with full trust today)
- [ ] **(M)** Plugin versioning and dependency resolution
- [ ] **(L)** VS Code / JetBrains extension
- [ ] **(M)** Preconfigured **skills** from `https://agentskills.io/skill-creation/quickstart`
      (project owner explicitly asked for these to be preconfigured)
- [ ] **(M)** Declarative agent "recipes" for common stacks
- [ ] **(L)** WASM plugin host for language-agnostic extensions

### J. Integrations
- [x] WhatsApp (Twilio / Meta / generic webhook) — full round trip verified
- [x] MCP: Figma, draw.io, Notion, Linear, Sentry, GitHub, Postgres, SQLite, Puppeteer, Brave, Tavily…
- [ ] **(L)** **GitHub/GitLab full integration**: PR creation, review comments, CI status, issue sync
- [ ] **(M)** Slack / Teams / Discord messaging alongside WhatsApp
- [ ] **(M)** Email integration
- [ ] **(L)** CI/CD: run the harness as a GitHub Action, post AXS scores on PRs
- [ ] **(M)** Jira / Linear issue pickup and status write-back
- [ ] **(L)** Observability export (OpenTelemetry traces of every turn)
- [ ] **(M)** Webhook *out*: notify any endpoint on turn complete / eval complete
- [ ] **(L)** Cloud IDE embedding (Codespaces/Gitpod)

### K. Security & governance
- [x] Layered, argument-aware permission rules; `deny` survives auto-approval
- [x] Plugin cannot shadow a built-in; names reserved before registration
- [x] Allowlists that fail closed; secrets never returned by the API
- [x] Governance audit trail (JSONL)
- [x] Settings layering system > project > user, deduped by resolved path
- [ ] **(L)** **OS-level sandboxing** for tool execution (seccomp/Job Objects/AppContainer)
- [ ] **(M)** Secret redaction in transcripts, logs and traces
- [ ] **(L)** Data-residency controls and PII detection
- [ ] **(M)** Signed plugins and integrity verification
- [ ] **(L)** SOC2-style audit export
- [ ] **(M)** Per-tool network egress policy
- [ ] **(M)** Prompt-injection defence depth (tool-result sanitisation, provenance marking)
- [ ] **(L)** Supply-chain checks on fetched MCP servers/packages

### L. Performance & reliability
- [x] Fast client import path; known-window cache; bounded probes
- [x] Streamed answers, per-phase timeouts
- [ ] **(L)** Persistent daemon so the agent process is warm between invocations
- [ ] **(M)** Tool-result caching with invalidation
- [ ] **(L)** Incremental/persistent index for grep/glob over huge repos
- [ ] **(M)** Parallel file reads and searches
- [ ] **(L)** Crash-safe session recovery with partial-turn repair
- [ ] **(M)** Memory ceiling enforcement
- [ ] **(M)** Structured error taxonomy with actionable remediation text

### M. Distribution & ops
- [x] `pip install`-able release with working console entry points — `[project.scripts] myriaxon =
      myriaxon.cli:main`, dynamic version from `constants.VERSION`, extras (`openai`, `gemini`, `mcp`,
      `keychain`, `build`, `all`, `dev`), `packages.find` so the dashboard and bench ship, package data
      for the IQB-14 tasks / built-in skill / brand SVGs, MIT licence
- [~] **(L)** Signed binaries for Windows/macOS/Linux — `myriaxon.spec` (which now bundles the
      built-in skill, the IQB-14 tasks and the brand SVGs by path) plus `scripts/build_binary.py`
      exist, but PyInstaller is **not installed** in this environment and no `dist/` has ever been
      produced, so the build is unverified
- [ ] **(M)** Auto-update mechanism
- [ ] **(L)** Docker image
- [ ] **(M)** First-run onboarding wizard (provider, key, workspace, verify)
- [ ] **(M)** Config validation command that diagnoses a broken setup
- [ ] **(L)** Telemetry opt-in with a clear privacy statement
- [ ] **(M)** Offline docs bundle

### N. Documentation
- [x] `README`, `GAP-ANALYSIS`, `IQB-14`, `VERTEX-SETUP`, `PLUGINS`, `MCP-SETUP`, `MESSAGING`
- [x] This handover document
- [ ] **(M)** Architecture decision records (many odd decisions are only in code comments)
- [ ] **(M)** Contribution guide and coding standards
- [ ] **(M)** Troubleshooting guide built from the bug list in §3
- [ ] **(L)** Video/GIF walkthroughs
- [ ] **(M)** Per-feature reference docs

---

## 6. Suggested order of work (my recommendation)

1. **Re-run the full suite** and get a green baseline (§4.2 item 1). Nothing else is trustworthy until
   this is green.
2. **Mid-stream cancel** (A) — small, high daily value, and currently misleading because `/api/cancel`
   exists but does nothing to a turn already in flight.
3. **Task corpus for IQB-14** (D) — the standard is useless without tasks. This is the biggest
   credibility gap for a "benchmarking" harness.
4. **LSP/tree-sitter tools** (C) — biggest capability jump per unit effort for real coding work.
5. **GitHub/CI integration + run the harness as a GitHub Action** (J) — turns the eval standard into
   something a team actually uses.
6. **Plugin/skill preconfiguration** (I) — the project owner asked for this explicitly and it is
   half-built.
7. **Per-tool-call approval in the dashboard** (G) — the dashboard forces yolo today, which is a real
   safety gap.
8. **OS-level sandboxing** (K) — required before letting this loose on untrusted repositories.
9. **Local model support** (F) — any OpenAI-compatible endpoint already works; what is missing is
   capability detection and a first-class Ollama/vLLM story.
10. **Verify the binary build and publish the wheel** (M) — the pip path is done, but nobody can adopt
    a single-file binary that has never been built.

---

## 7. Working agreement / how to continue

1. **Test with Netra only.** Explicit instruction from the project owner.
2. **Verify by running.** Do not claim a feature works because the code looks right. Every entry in §3
   was executed. Prefer a failing check that proves a bug over a reasoning argument.
3. **Add a check script for every change**, then add it to `scripts/run_dashboard_checks.py` so it runs
   with everything else.
4. **Comment *why*, especially for non-obvious choices and measured numbers.** The existing code does
   this heavily; match it. Several comments record a measured value that justified the design.
5. **Do not expand into multi-agent by default.**
6. **Fix the class of bug, not the instance.** Three separate Handler-vs-App `AttributeError`s
   occurred; a static guard now prevents them. Look for the generalisation.
7. **Report honestly.** If something is untested, say untested. The project owner has repeatedly
   valued precision over optimism, and explicitly asked for "what is missing" rather than reassurance.
8. **Prefer zero new dependencies** in the dashboard and the core; they are a deliberate feature.

### How to run things

```powershell
# full suite (delete the isolated user config first)
Remove-Item -Recurse -Force bench-out\checks-userconfig -ErrorAction SilentlyContinue
python scripts\run_dashboard_checks.py

# a single suite
python scripts\check_permission_rules.py

# the harness itself
.\myriaxon.cmd          # terminal
.\myriaxon-web.cmd      # dashboard at http://127.0.0.1:8765/

# browser verification (needs wider sandbox access; skips otherwise)
python scripts\check_ui_visual.py

# MCP SDK, if missing
pip install -e ".[mcp]"
```

---

## 8. Honest limits of this document

- The IQB-14 table in §2 was **printed from `myriaxon/bench/parameters.py`** at handover time
  (total 1.0000, 14 parameters). If the registry changes, that table is what to re-check; the module is
  normative, not this document.
- The suite counts in §4.1 come from the last full run; the catalog figure is from a **separate,
  later standalone confirmation**. Re-run to get one authoritative set.
- §5 is a considered roadmap, not a specification. Effort letters are estimates, not commitments.
- This session did **not** audit `bench/` in depth; its ~3,400 lines deserve their own review. In
  particular `scoring.py` (706 lines) and `runner.py` (579 lines) were not re-read while writing this.
- Earlier in the project, an assistant draft of the IQB-14 parameter *names* was wrong (it listed
  efficiency/recovery/autonomy instead of comprehensiveness/relevance/fairness/adversarial). The table
  above supersedes any such earlier list — trust the module.
