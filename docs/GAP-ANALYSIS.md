# Myriaxon — capability map and gap analysis

Compared against **DeepSeek Harness**, **Google Antigravity** and **Claude Code**, as of this
revision. Every row states how it was verified, so the table cannot drift from the code. Regenerate
the raw inventory with `python scripts\audit_capabilities.py`.

Legend: **have** = implemented and verified · **partial** = works with a known limit · **gap** = not
implemented.

---

## 1. Auto mode checklist

"Auto mode" means different things across harnesses, so each surface is listed separately.

| Capability | Myriaxon | How verified |
|---|---|---|
| `auto` mode: act without asking, except risky shell | **have** | `tools.py:234` gates only `run_bash` on `_is_risky_bash` |
| `auto_edit` mode: file edits free, shell gated | **have** | `tools.py:231` |
| `plan` mode: read-only planning | **have** | mode gates write tools |
| `manual` mode: confirm every action | **have** | default mode |
| Mode switchable **mid-conversation** | **have** | `/mode` + the mode dropdown; agent rebuilt, transcript kept |
| Mode enforced by policy (org lock) | **have** | `settings.enforced_mode`, surfaced in `/api/config` |
| Auto-approval toggle independent of mode | **have** | `/yolo` + the *Automatic approval* setting |
| Headless/non-interactive run | **have** | `-p`, `--yolo`, `--output-format json` |
| Auto-continue until the task is done | **partial** | the agent loops on tool calls within a turn; it does not self-prompt across turns |
| Auto-retry on transient provider failure | **have** | retry wrapper + empty-response retries; Netra 502 bursts survived |
| Auto-compaction when context fills | **have** | graduated warn → prune → compact → refuse, profile-driven |
| Auto-fallback model on quota/rate limit | **have** | `fallback_model` |
| Budget guard (steps/tokens/wall clock) | **have** | `bench/guard.py`, enforced at the tool boundary |
| Risky-command classification | **have** | `_is_risky_bash` |
| Auto-approve with an allowlist (approve `git *`, ask for `rm`) | **have** | argument-aware rules, editable in the UI; `git` does **not** authorise `gitleaks` |
| **Turn-level interrupt** | **partial** | `/api/cancel` sets a flag the UI reports at `turn_end`; the agent checks nothing mid-stream (`dashboard/session.py:330`) |
| **Scheduled/background self-prompting** | **have** | `schedule_prompt`, `cancel_scheduled`, `list_scheduled` |

**To finish auto mode:** one item remains — an opt-in auto-continue across turns (and, if it is
wanted, a real mid-stream cancel flag for the agent loop rather than the intent marker that exists
today).

---

## 2. Parity matrix

### Agent capability

| Capability | Myriaxon | DeepSeek Harness | Antigravity | Claude Code |
|---|---|---|---|---|
| Tools | **32** (29 without delegation) | ~15 | ~12 | ~20 |
| Sub-agent delegation | **have** (`task`, `parallel_tasks`, capped at 3 concurrent) | yes | "agent manager" | yes |
| Skills / reusable workflows | **have** (`save_skill`, `use_skill`) | yes | no | yes (skills) |
| Memory across sessions | **have** (`search_memory`, sqlite FTS5) | yes | partial | yes |
| Hooks (PreToolUse/PostToolUse/UserPromptSubmit) | **have** | no | no | yes |
| Checkpoints / rewind | **have** (`checkpoint_list`, `checkpoint_restore`, `/rewind`, `/undo`) | no | yes | yes |
| Notebook editing | **have** (`edit_notebook`) | no | no | yes |
| MCP client | **have** (stdio + streamable HTTP + SSE, auth, registry search) | no | no | yes |
| Plugin API | **have** (in-process tools + 5 hook events; `docs/PLUGINS.md`) | plugins | extensions | plugins |
| Context adaptation per model tier | **have** (small/balanced/large profiles) | partial | no | no |
| Image / PDF input | **have** (verified: read an infographic) | yes | yes | yes |
| Self-verification (grounding) | **have** (`grounding_enabled`, `find_unverified_paths`) | no | no | no |

### Providers and models

| Capability | Myriaxon | DeepSeek Harness | Antigravity | Claude Code |
|---|---|---|---|---|
| Providers | **6 built-in + unlimited custom** | DeepSeek only | Gemini only | Anthropic only |
| Multi-vendor in the web UI | **have** | n/a (single vendor) | no | n/a |
| Multi-model in the web UI | **have** | yes | yes | yes |
| Switch vendor/model **mid-conversation** | **have** (transcript carried) | n/a | no | n/a |
| Add a custom endpoint from the UI | **have** | no | no | no (config only) |
| Probe endpoint for its model list | **have** | no | no | no |
| Test an endpoint before committing | **have** | no | no | no |
| Local model support | **via custom endpoint** | no | no | no |
| Vertex AI / WIF (corporate identity) | **have** | no | yes (GCP) | n/a |
| Fallback model per session | **have** | no | no | no |

**This is the axis where Myriaxon already leads.** Six built-in vendors, unlimited custom
endpoints, and vendor switching that preserves the conversation is more than any of the three
comparison harnesses offer.

### Interface

| Capability | Myriaxon | DeepSeek Harness | Antigravity | Claude Code |
|---|---|---|---|---|
| Terminal REPL | **have** | yes | no | yes |
| Local web UI | **have** (no framework, no build) | yes (desktop) | yes (IDE) | no |
| Trajectory / tool-call view | **have** | yes | partial | yes |
| Streaming markdown incl. tables | **have** (verified) | yes | yes | yes |
| Light / dark theme | **have** | dark only | both | n/a |
| Settings UI | **have** (9 options, generated from schema) | yes | yes | partial |
| File explorer in the UI | **have** (Files &amp; changes panel) | yes | yes | n/a |
| Diff review in the UI | **have** (colourised hunks, staged toggle) | yes | yes | n/a |
| Session list / create / delete | **have** | yes | partial | yes |
| Project switcher in the UI | **have** | workspaces | yes | n/a |
| Slash commands | **have** (33 CLI, 19 in the UI) | yes | partial | yes |
| Offline / no external assets | **have** (0 network fetches) | n/a | no | n/a |
| Packaged distribution | **have** (pip-installable wheel + console script; single-file build unverified) | yes | yes | yes |

### Evaluation and assurance

| Capability | Myriaxon | DeepSeek Harness | Antigravity | Claude Code |
|---|---|---|---|---|
| Benchmark standard | **IQB-14** (14 parameters, AXS 0–100) | no | no | no |
| Trace export | **have** (JSONL, promptfoo exporter) | no | no | partial |
| Judge model support | **have** (Myriaxon itself, or OpenAI-compatible) | no | no | no |
| pass@k / pass^k, Wilson CI | **have** | no | no | no |
| Budget guard | **have** | no | no | no |
| Reproducible scorecard | **have** (`bench report`, `bench compare`) | no | no | no |

### Integrations

| Capability | Myriaxon | Notes |
|---|---|---|
| MCP servers — CLI | **have** | add/remove/list, stdio **and remote HTTP/SSE**, registry search |
| MCP servers — from the UI | **have** | MCP panel: list, add (both transports + access token), remove, registry search — `check_mcp_ui.py` |
| MCP remote transports | **verified live (HTTP + stdio)** | streamable-HTTP against a real server, plus stdio. SSE is implemented and negotiated as a fallback, but the live fixtures do not exercise it. |
| Plugin API | **have** | in-process plugins adding tools and hooks; `docs/PLUGINS.md` |
| Messaging integration (WhatsApp) | **have** | Twilio / Meta / generic webhook; `docs/MESSAGING.md` |
| GitHub / CI integration | **gap** | `git_status`/`git_diff` tools only |

> **MCP status, verified against real servers.** Four defects were fixed and then proven:
>
> 1. `McpBridge.start()` iterated only `stdio_servers`, so a configured **remote** server — Figma,
>    Microsoft 365, GitHub, anything hosted — was written to `mcp_servers.json` and then never
>    connected. It contributed no tools and said nothing.
> 2. `cli.py` gated bridge creation on `stdio_servers`, so with only URL servers configured the bridge
>    was never even built. The dashboard passed `None` for the bridge entirely, so MCP never loaded in
>    a web session.
> 3. No authentication. Hosted servers are almost all authenticated (Figma PAT, Microsoft OAuth,
>    GitHub PAT), so a remote server was unfixably broken. Tokens and headers are now stored and sent,
>    and an existing token survives an edit.
> 4. The SDK renamed things between 1.x and 2.x — `streamablehttp_client` → `streamable_http_client`
>    (which takes an `http_client`, not headers), and `tool.inputSchema` → `tool.input_schema`. Only
>    the old names were handled, so the correct transport and tool schema were unreachable.
>
> Verified live: `check_mcp_remote_live.py` starts a real MCP server over streamable HTTP, connects
> through the bridge, lists its tools and **calls** two of them (`echo` → `echo: hello remote`,
> `add` → `42`), including a token-configured connection. `check_mcp_live.py` does the same over stdio:
> `echo` → `echo: hello from Myriaxon`, `add` → `42`. Both fixtures expose a tool named `read_file` to prove
> a server's tools arrive namespaced as `mcp__<server>__<tool>` and cannot shadow a built-in.

---

## 3. Prioritised gap list

Delivered first, then what remains — ordered by user impact ÷ effort, each with an explicit success
criterion.

### P0 — delivered

| # | Gap | Status | Verified by |
|---|---|---|---|
| 1 | Custom endpoints addable from the UI | **done** | `check_endpoints.py` — 25 checks |
| 2 | Multi-vendor switching **in the UI**, not just backend | **done** | vendor dropdown + `/provider`; endpoint remap asserted |
| 3 | Multi-model switching per vendor in the UI | **done** | model list refreshes on vendor switch |
| 4 | Endpoint probe (fetch models) and test before use | **done** | live Netra probe returned both models |
| 5 | API keys never echoed to the browser | **done** | `has_api_key` only |
| 6 | MCP from the UI, both transports, with auth | **done** | `check_mcp_ui.py`, `check_mcp_remote_live.py`, `check_mcp_live.py` |
| 7 | **Permission rules** (`allow`/`ask`/`deny`, argument-aware) | **done** | `check_permission_rules.py` (25) + a live turn blocked by a deny rule |
| 8 | **Plugin API** | **done** | `check_plugins.py` (24), `check_plugin_shadowing.py` (15), `docs/PLUGINS.md` |
| 9 | **WhatsApp / messaging** | **done** | `check_messaging.py` (42, incl. a real round trip), `check_messaging_ui.py`, `docs/MESSAGING.md` |
| 10 | **File explorer + diff review in the UI** | **done** | `check_files_ui.py` — `/api/files`, `/api/file`, `/api/git`, `/api/git/diff` + the Files & changes panel |
| 11 | **Packaging / distribution** | **done** | `pyproject.toml` — console script `myriaxon = myriaxon.cli:main`, extras (`openai`, `gemini`, `mcp`, `keychain`, `build`, `all`, `dev`), dynamic version from `constants.VERSION`, `packages.find` so the dashboard and bench ship too, package data for the IQB-14 tasks, the built-in skill and the brand SVGs |

### Open — prioritised

| # | Gap | Why it matters | Success criterion |
|---|---|---|---|
| 12 | **Mid-stream cancel** | A turn cannot be stopped once it is in flight; `/api/cancel` only records the intent and the UI reports it at `turn_end` (`dashboard/session.py:330` says so in as many words) | a running turn stops within a second of the request |
| 13 | **Auto-continue across turns** | Long tasks currently need a nudge; the agent loops on tool calls *within* a turn and then stops | an opt-in setting that re-prompts until a declared stop condition |
| 14 | **GitHub / CI integration** | PR review, issue-to-fix in CI; today there are `git_status` / `git_diff` tools and the diff viewer only | a documented headless command that posts a review comment |

---

## 4. Visual verification

`scripts/check_ui_visual.py` renders the dashboard in **real Chrome** through the DevTools Protocol
(`scripts/ui_render.js`), screenshots the main view and every dialog in both themes, and asserts on
measurements taken **inside the page** — resolved CSS variables, text/background luminance contrast,
control sizes and on-screen positions, dialog dimensions, console exceptions.

This exists because every other UI check greps the served HTML for an element id, which cannot see a
panel rendered off screen, an unresolved CSS variable, or a dialog that opens as an empty box. It
immediately found two such defects:

* **The sidebar footer clipped its own buttons.** Every footer button carried `margin-left:auto`, so
  with seven action buttons the later ones — including **the light/dark toggle** — were pushed past the
  sidebar edge and were not clickable at all. Replaced with a wrapping grid, so adding a button can
  never hide another one.
* **`--mono` was referenced but never defined**, so the file and diff panels silently fell back to the
  default sans font instead of showing aligned code.

The check skips (rather than fails) when no browser is present or the environment denies the browser's
launch, since that is an environment capability rather than a property of the UI. Run it with wider
access for the full 25 checks: `python scripts/check_ui_visual.py`.

---

## 5. Where Myriaxon already surpasses the comparison

1. **Provider freedom.** Six built-in vendors plus unlimited custom endpoints, switchable mid-chat
   with the transcript preserved. DeepSeek Harness is single-vendor, Antigravity is Gemini-only,
   Claude Code is Anthropic-only.
2. **Evaluation built in.** IQB-14 with primary/secondary evidence grading, pass@k, Wilson intervals,
   budget guards and a reproducible scorecard. None of the three has a benchmark standard.
3. **Context adaptation.** Explicit behavioural profiles for small/balanced/large models —
   verbosity, tool-result caps, delegation, compaction thresholds — so a weak model with a small
   window is usable rather than merely tolerated.
4. **Corporate identity.** Vertex AI through workforce federation (WIF), so runs carry a company
   principal rather than a personal account.
5. **Zero-dependency UI.** No framework, no npm, no CDN, 0 external fetches — it runs where the CLI
   runs, including air-gapped machines.
6. **One-command distribution.** The wheel carries the whole product: dashboard, benchmark, the
   IQB-14 task files, the built-in skill and the brand assets. The previous packaging configuration
   named a single package, which would have shipped a wheel with no dashboard at all.

---

## 6. Honest limits

- **Mid-stream cancel is not implemented.** `/api/cancel` records the intent and the UI reports it;
  the agent loop checks no flag once a turn is in flight (`dashboard/session.py:330`). It is listed
  as an open gap in §3 above, not as a delivered one.
- **A turn does not self-prompt across turns.** Auto-continue is the one place left where the agent
  loop, rather than the surrounding harness, is the limiting factor.
- Vendor switching carries conversational text, not the tool-call history: the provider message
  formats are incompatible (Gemini requires `thought_signature` on replayed calls).
- The web UI's **behaviour** is verified in a real browser (`check_ui_visual.py` asserts resolved CSS
  variables, contrast, control geometry and console exceptions); what has **not** happened is a
  designer's aesthetic review of the layout.
- **The PyInstaller single-file build is unverified.** `myriaxon.spec` and
  `scripts/build_binary.py` exist and now bundle the built-in skill, the IQB-14 tasks and the brand
  SVGs by path, but PyInstaller is not installed in this environment and no `dist/` has been built.
  The pip-installable path is verified.
- Only **Netra** is used for live provider testing, per project rule.
