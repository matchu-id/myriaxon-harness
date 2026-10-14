# Changelog

All notable changes to Myriaxon. This project follows [Semantic Versioning](https://semver.org/).

## [1.2.0] — 2026-10-01

Mission control: the dashboard becomes an operations surface rather than a chat window with a
drawer of dialogs, and the persistent agents become something you can rely on unattended.

### Added — Mission control

The dashboard now opens on a view that answers "what is going on" without being asked: connection
health for every provider and integration, usage and token accounting for this process, active and
recent runs with elapsed time, the queue of decisions waiting on a person, scheduled work with the
next objective, a live trace feed, model routing shares, indexed memory and available skills.

- **A rail with rooms.** Mission control, Chat, Approvals, Dots, Files, MCP, Plugins, Policies,
  Messaging and Settings. Entries that open a dialog reuse the button that already owns it, so
  there is one implementation of each panel; an entry naming a panel that does not exist says so
  instead of appearing to do nothing.
- **A connection diagram** drawn from the same health data as the list beside it, so the picture
  and the rows can never disagree.
- **Every number is real.** The snapshot is assembled server-side from the machine's actual state —
  usage totals, session state, the audit log, the memory index, the dot registry. Nothing on the
  view is illustrative, and a section with nothing to report says so rather than showing an
  invented row. That distinction is the whole point of a control surface.
- **It cannot blank the page.** The route degrades to an explained empty snapshot instead of a 500,
  because it is the landing view: an exception here would leave the user on a white screen with no
  way to reach the chat either. Verified by breaking every data source in a test.
- **`myriaxon/mission_control.py`** also exposes `render_lines()`, so the same state can be read
  from a terminal without a browser.

### Added — dots in the dashboard

Persistent agents are fully usable from the browser: list, create, add objectives, mark them done
or blocked, toggle proactive and enabled, delete, and run one now. A run is an ordinary session
turn with its own transcript, so it streams through the same event feed as anything typed by hand,
can take minutes without blocking a request, and stays readable afterwards as a record of what that
dot did. A disabled dot will not run, and a dot with no open objectives is refused rather than left
to invent work.

### Fixed — the form layout

The messaging dialog laid its checkboxes out as separate blocks from their own captions, wrapping
labels onto a second line. Cause: the `.chk` class was referenced in three places and defined
nowhere, so each control and its text were two unrelated elements.

- Dialogs now share real form components — `.field`, `.fieldset`, `.chk`, `.statusline` — declared
  once. Three dialogs had each invented their own version of "a labelled control", which is how
  they drifted apart. Messaging and Dots are rebuilt on them, with titled sections and a status
  line that says in words who can drive the agent.
- `tools/css_audit.py` now catches this whole class of defect: a class used in markup with no rule,
  a `var()` that resolves to nothing, and rules nothing uses. It found two more real cases —
  `.codepane` and `.codeviewer` were also unstyled, so the file explorer panes grew the dialog
  instead of scrolling inside it — and `.seg`, the trace filter, which had no styling at all.

### Fixed — test isolation

- `tests/conftest.py` points Myriaxon's state at a scratch directory for the whole session. Without
  it, whichever test module imported the package first decided where every test wrote, and a full
  run created eight real agents in the developer's own registry. A test-isolation bug that presents
  as a product bug, and that edits the user's configuration while doing it.
- An autouse fixture restores the working directory around every test. `serve()` chdirs into its
  workspace, deliberately, because the agent's tools resolve relative paths against the process
  directory — correct for the product, and a trap for a suite.
- A second fixture hands each test a fresh dot registry. The accessor caches a singleton, so a test
  that pointed it at a scratch file left that path behind for the next file, which is why the same
  suite produced a different failure set on each run.
- The mock provider reads a dot's name from its briefing by matching the full phrase and skipping
  system messages. It previously matched any message starting "You are ", which includes the
  product's own system prompt ("You are Myriaxon, a terminal-based coding agent"), so scripted tool
  calls were intermittently addressed to a dot called "Myriaxon" and failed.

## [1.1.1] — 2026-10-01

Three shipped-artifact defects, found by re-auditing the tree rather than by a failure report.

### Fixed

- **The distributable brand assets had gone missing.** The SVG set under `brand/` was deleted during
  a repository cleanup and only the packaged copy survived. `tools/export_brand_assets.py` now
  writes both destinations from one source in a single command, so the set a designer or a deck
  needs and the set a wheel serves cannot drift apart or vanish again.
- **The red-team provider invoked the pre-rebrand module name.** `redteam/myriaxon_provider.py` ran
  `python -m ioh_code` and set `IOH_PROVIDER`, so every probe would have failed to start and the
  provider selection would have been silently ignored. The three promptfoo configs also still named
  the old script — a quoted string inside an `exec:` target, which is why no import broke and
  nothing failed loudly.
- **The installer could delete a working install while reporting success.** Three separate defects
  in the one script whose entire job is to leave a working tool:
  - `site.getsitepackages()` returns the virtualenv **root** before `site-packages`, so the editable
    redirect was written to a directory the import system never reads — and the same wrong answer
    decided which copy to delete, removing the working install at the same time. The directory is
    now resolved by checking each candidate against a module that is certainly installed, and the
    copied package is only removed once the redirect is confirmed to point at this checkout.
  - pip's build step is pathologically slow on a synchronised folder (thousands of small files; 91
    seconds measured on a OneDrive-hosted virtualenv). It is now bounded by a timeout, with a
    fallback that installs the dependencies and writes a source redirect — reading the real
    dependency set from `pyproject.toml` rather than a copy that would drift. `--pip-timeout` tunes
    the budget.
  - pip's output was captured into a string, so a first install printed "installing …" and then
    appeared to do nothing at all. Output is inherited now.

### Added

- `tests/test_installer.py` — locks the above in, including a probe that writes a uniquely named
  module into the resolved directory and imports it, which is the only way to prove that directory
  is the one Python actually reads.
- `INSTALL.md` documents the `site-packages` trap, the synchronised-folder slowdown, and the fast
  fix: keep the virtualenv off the synced folder, where it never needed to be.

## [1.1.0] — 2026-10-01

The first release under the Myriaxon name. This is a rename and a substantial repair of the
predecessor harness, not a cosmetic rebrand: the tool did not start before this work, and
several features were false in their own documentation.

### Identity

- **New name and identity.** Myriaxon, an advanced agentic harness. Tagline: *Ten thousand
  paths. One mind.* The mark is an eight-ray astral compass converging on one signal-green
  core — many paths, one mind.
- **New palette.** Signal green `#7CF5C8` on near-black `#0A0C11`, with a full light theme
  where the accent darkens to `#0B8F63` because signal green on white fails contrast. Colour is
  bound to meaning: signal means the live thing, amber means it needs you, violet means memory.
- **New type.** Unbounded for display, Instrument Sans for interface, JetBrains Mono for code —
  as font stacks rather than fetched webfonts, because the dashboard makes zero network requests
  and must work air-gapped.
- **`myriaxon/brand/__init__.py` is the single source of truth** for all of it. The CLI banner,
  Rich theme, syntax highlighter, prompt_toolkit style, dashboard CSS and the exported SVG
  assets are all generated from or asserted against those tokens.
- **New terminal header.** The braille compass mark beside a five-row block wordmark, with the
  connection and model on their own line and the tagline beneath. Falls back to tracked-out
  capitals on terminals too narrow for the block letters.

### Renamed

- Python package `ioh_code` → `myriaxon`; console command `myriaxon`; module entry
  `python -m myriaxon`.
- Environment variables `IOH_*` → `MYRIAXON_*`.
- State directory `~/.iohcode` → `~/.myriaxon`; project instructions `IOH.md` → `MYRIAXON.md`;
  ignore file `.iohignore` → `.myriaxonignore`.
- Launchers `ioh.cmd`/`harness.cmd` → `myriaxon.cmd`/`myriaxon-web.cmd`.

### Fixed — the tool did not run at all

- **`NameError` on every launch.** `cli.py` passed a variable that was never assigned, raised
  before the banner and outside the top-level error handler, so every invocation — interactive
  and one-shot — died with a raw traceback. The argument was the MCP tool list, which had been
  renamed everywhere except at its use site.
- **`NameError` on every Anthropic turn.** `agent.py` called `json.dumps` without importing
  `json`, so the context-budget check raised on the first iteration of every turn. It was
  swallowed as "Unexpected error", which is why it read as a provider problem.
- **Context budget measured the wrong text.** The budget estimate used `messages[0]` as the
  system prompt. For Anthropic that is the first *user* message, and a list of content blocks
  once attachments are attached — so the estimator raised `TypeError` on a list, or silently
  under-counted and compacted late. Now built from the same call the request itself makes.
- **`ContextExhausted` was never caught.** The deliberate refusal to send an over-window request
  carries a written explanation and the fix; it fell through to the generic handler and was
  reported as "Unexpected error — details logged to …", telling the user nothing actionable.
- **Hook and plugin prompt rewrites were discarded.** Both agents computed a rewritten prompt
  and then appended the raw input in the no-attachment case — the one case prompt hooks exist
  for. Rewrites now take effect.
- **BOM stripping was broken and its comment claimed otherwise.** The literal had been
  destroyed by an encoding round-trip into mojibake, so a BOM-prefixed line was never
  recognised as a slash command and was sent to the model as chat text. Now written as the
  `\ufeff` escape, which cannot be mangled the same way.
- **24 mojibake strings repaired** across the agents and the setup wizard — em dashes and
  arrows that had been through UTF-8 → cp1252 → UTF-8 and were being printed and sent to models.
  Twelve files carried a stray UTF-8 BOM; all are now clean UTF-8 with LF endings.
- **`/plugins` was undocumented and unreserved.** It worked but appeared in neither `/help`, the
  completion menu, nor the reserved-name list, so a custom command named `plugins` was silently
  shadowed with no warning.
- **`web` subcommand could not see global flags.** They had to precede the subcommand, which was
  undocumented and silently ignored `--mode` when placed after `web`.

### Fixed — data loss and credentials

- **Rebranding stranded the user's configuration.** The state-directory migration declined to
  run whenever the destination existed, and anything that touched the config directory first —
  a log write, a probe, an earlier build — created it. The result was a live `config.json` with
  a real API key left behind while every launch showed the first-run setup wizard. The migration
  now adopts a destination that holds no state of its own, distinguishing state from regenerable
  files such as `logs/`. Verified against five scenarios including the exact failure.
- **`.gitignore` did not cover the state directory.** `.myriaxon/` — holding an API key, MCP
  tokens, messaging credentials, an audit log and a memory database — was untracked and
  unignored: one `git add -A` from being committed. Now ignored, along with the legacy name.

### Added

- **A hard cap of three concurrent sub-agents**, enforced by a session-wide semaphore and a
  schema limit rather than merely described in a prompt. Every sub-agent shares one tool
  context, working directory and checkpoint repository, so unbounded fan-out produced git index
  collisions and interleaved edits. A limit that exists only in prose is not a limit.
- **Favicon and header mark in the dashboard.** The favicon is an inline `data:` URI, which
  cannot 404 and keeps the page self-contained. The mark is declared once in a `<template>` and
  cloned into each placement, so the sidebar and header cannot drift into two drawings.
- **Guided installers** for Windows and POSIX, plus `tools/install_dev.py`. They detect a dead
  virtualenv and rebuild it, and they handle non-ASCII install paths, where `pip install -e .`
  fails because setuptools writes its redirect in the console's locale encoding.
- **A generated palette sheet and lockup**, produced from the brand tokens by
  `tools/export_brand_assets.py` so the distributed assets cannot go stale.

### Added — persistent agents (dots)

A dot is a durable agent identity, not a session and not a sub-agent. It has a name, a role,
standing instructions, objectives it is working toward, its own workspace, and a log of what it
has done — and it resumes all of that every time it runs. Where a session forgets and a sub-agent
lasts one tool call, a dot persists.

- **It remembers.** Identity, objectives, notes and history live in `~/.myriaxon/dots.json`, so a
  dot that started an objective yesterday continues it today without being re-briefed. The standing
  brief handed to the model is rebuilt from durable state on every turn, which is why it is
  identical whichever surface the dot runs from.
- **It acts.** With proactive mode on, a due dot's objectives are advanced by ordinary turns while
  a session is open. Its writes pass through the identical permission layer as anything typed by
  hand — a dot is autonomous, not privileged — and every action is appended to its activity log.
- **It is inspectable.** Objectives, notes, workspace and history can be listed, shown, and read
  in the dashboard. Autonomy without visibility is a black box that spends money.
- **Four new tools** (`dot_list`, `dot_create`, `dot_goal_update`, `dot_run`) and a `/dots` command
  with `create` / `show` / `run` / `goal` / `proactive` / `rm`.
- **Cross-process safe.** The terminal and the dashboard share one registry and are routinely open
  together, so every mutation re-reads the file first. Without that, the second process to save
  silently reverted every dot the first had created — verified by test.

A dot composes what already existed rather than replacing it: the scheduler fires its turns, the
memory store indexes what it did, skills capture what it learned, checkpoints make its edits
reversible, and the permission policy gates its writes. "Always on" here means *it resumes by
itself without being prompted*, not *it runs when nothing is running* — a background process that
keeps editing files after the window is closed is a surprise, not a feature. `DotRegistry.next_due`
is the hook for making it genuinely continuous via a service or scheduled task.

### Added — end-to-end verification

- **A real agent turn, offline.** `tools/mock_provider.py` is a scripted OpenAI-compatible server,
  and `tests/test_e2e_turn.py` drives the full path through the same `_build_agent` that `main()`
  calls: streamed response, tool call, tool result fed back, final answer, persisted transcript,
  plus the dashboard's separate agent builder. 31 checks, no API key, no network, deterministic.
- This exists because **every release-blocking defect in this codebase lived on that path** and was
  invisible to unit tests that check the pieces. A suite can pass while the product cannot start.

### Added — brand consistency tests

- 21 tests that fail if the dashboard's palette drifts from `myriaxon/brand/`, if any CSS rule
  references a variable the page never defines, or if the identity loses the properties that make
  it recognisable. An undefined CSS variable is not an error — it resolves to nothing and the
  element silently loses its styling, which is exactly how the previous design shipped its code
  panels in the wrong font.

### Changed — packaging

- **A wheel built from the previous configuration lost the web dashboard and the entire
  evaluation harness.** `packages` named one package explicitly; the subpackages were never
  included. Editable installs kept them on disk, which hid it. Now uses package discovery, and a
  build test asserts the subpackages and the task suite are present.
- **`httpx` is a declared dependency.** It is the default transport for every OpenAI-compatible
  provider and for MCP registry search, but was only ever present transitively via `anthropic`.
- **The IQB-14 task suite moved inside the package** and its path resolves against the installed
  package rather than the working directory. It previously only worked from a source checkout at
  the repository root, and elsewhere reported zero tasks instead of erroring.
- **The PyInstaller spec no longer hardcodes a developer's checkout path** and now bundles the
  built-in skill, the task suite and the brand assets — all loaded by path at runtime, so a
  frozen build previously started with no built-in skills and an empty benchmark suite, silently.
- `LICENSE` (MIT), `CHANGELOG.md`, `INSTALL.md`, and `docs/BRAND.md` added. `pytest`
  configuration added so the offline suites are the default run.

### Changed — documentation

- `README.md` rewritten as a product README.
- `docs/GAP-ANALYSIS.md` updated against the code, with the remaining gaps stated honestly
  rather than rounded up.
- The pre-rebrand corporate attribution was removed from the product name and author strings.

### Known limitations

- **Mid-stream cancellation is not implemented.** `/api/cancel` records intent and the UI
  reports it, but a turn already in flight is not interrupted; the agent checks no flag between
  steps. Documented as open rather than closed.
- **A dot runs while a Myriaxon process is open.** It is not a detached daemon, deliberately: a
  background process that keeps editing files and spending tokens after the window is closed is
  a surprise rather than a feature. `DotRegistry.next_due` is the hook for a supervisor.
- **The webhook shared-secret header is still `X-Ioh-Secret`**, a pre-rebrand name. It is part
  of a deployed contract; a `X-Myriaxon-Secret` alias was added and the old name is still
  accepted so an existing integration does not break.
- **The standalone binary and the Docker / Windows Sandbox `run_bash` paths are unverified** —
  PyInstaller is not installed here and neither sandbox backend is available on this machine.
  The code paths exist and are marked unverified in the source.
- Vendor switching carries conversational text but not tool-call history; the provider message
  formats are not interchangeable (Gemini requires a `thought_signature` on replayed calls).
- The visual layout is verified by measurement inside a real browser, but Chrome cannot be
  driven in this environment, so computed-style verification of the new palette was skipped.
  Markup, contrast tokens and every CSS variable reference are asserted instead.
- **The dashboard does not yet surface dots in its own panel.** They are fully usable from the
  terminal (`/dots`) and by the agent through the four tools; the web panel is designed but not
  built, and is listed here rather than implied to exist.
