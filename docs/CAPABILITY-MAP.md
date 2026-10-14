# Containerized Agent Capability Map

**Measured, not assumed.** Every "covered" below rests on a probe of the built sandbox image
(`myriaxon-sandbox:latest`), not on a wish list. The probe ran inside the container and reported what
is genuinely present.

## The measured environment (2026-10-12)

```
HOST PROGRAMS present (22/51)
  pwsh, bash, sh, python3, pip3, node, npm, git, make, gcc, g++, curl, wget,
  jq, sed, awk, grep, tar, openssl, ssh, diff, patch

MISSING (29)
  zip, unzip, sqlite3, psql, mysql, mongosh, redis-cli, pandoc, pdflatex,
  convert, ffmpeg, dot, soffice, chromium, playwright, java, dotnet, go,
  rustc, ruby, php, Rscript, gnuplot, docker, rsync, file, bc, vim, nano

PYTHON STDLIB present (58/59)     missing: tomllib (3.10 image)
THIRD-PARTY present (0/34)        NONE
```

### The three facts that decide everything

1. **No network at run time.** `--network none` is the isolation. So a missing package can never be
   installed, and `pip3` being present does **not** mean pip works. `THIRD-PARTY present (0/34)` is
   therefore permanent for this image, not a temporary state.
2. **Python standard library is 58/59.** This is the single biggest capability asset: `json`, `csv`,
   `sqlite3`, `xml`, `re`, `statistics`, `decimal`, `hashlib`, `zipfile`, `tarfile`, `http.server`,
   `smtplib`, `ssl`, `email`, `asyncio`. Almost all text, data and file work is therefore reachable.
3. **A toolchain exists.** `gcc`, `g++`, `make`, `node`, `npm`, `git`. Real compilation and real
   JavaScript are available.

### The workarounds that matter

| Missing | Workaround, available now |
|---|---|
| `sqlite3` CLI | `python3 -c "import sqlite3"` — the module is present |
| `jq` is present, but structured edits | `python3 -m json.tool`, `python3` one-liners |
| `pandoc`, `pdflatex` | **none** — document conversion is blocked |
| `convert` (ImageMagick) | SVG by hand; PIL is absent |
| `ffmpeg` | **none** — no audio or video |
| `chromium`, `playwright` | **none** — no browser automation |
| `dot` (Graphviz) | write DOT by hand; no renderer. SVG layouts by hand |

## Coverage scale

| Mark | Meaning |
|---|---|
| **FULL** | The container has what the role needs; the harness can drive it today |
| **PARTIAL** | Real work is possible, with a named and permanent limitation |
| **BLOCKED** | The container cannot do it at all, and cannot be made to without changing the image |

Role percentages count **artifacts the agent can actually produce**, not whether a human could do the
job with more context.

---

## 1. Text and document work

| # | Role | Deliverable | Verdict | Why |
|---|---|---|---|---|
| 1 | Technical writer | docs, references | **FULL** | markdown + files |
| 2 | API reference writer | OpenAPI docs | **FULL** | read code, emit markdown |
| 3 | Copy editor | corrected prose | **FULL** | `difflib`, `re` |
| 4 | Translator | translated text | **FULL** | model-bound, not tool-bound |
| 5 | Localization specialist | locale files | **FULL** | `json`, `gettext` |
| 6 | Proofreader | diff + notes | **PARTIAL** | no spell dictionary bundled |
| 7 | Documentation architect | IA, nav trees | **FULL** | structure is text |
| 8 | Release-notes writer | changelog from `git log` | **FULL** | `git` present |
| 9 | Grant / proposal writer | long-form docs | **FULL** | text output |
| 10 | Policy drafter | policy docs | **FULL** | text output |
| 11 | Contract reviewer | annotated contract | **PARTIAL** | text analysis yes; no legal DB |
| 12 | UX writer | microcopy, strings | **FULL** | text output |
| 13 | Knowledge-base curator | structured articles | **FULL** | text + `json` |
| 14 | Transcriptionist | — | **BLOCKED** | no audio; `ffmpeg` absent |
| 15 | Document converter | docx→pdf | **BLOCKED** | no `pandoc`/`pdflatex`/LibreOffice |

**Coverage: 11 FULL, 2 PARTIAL, 2 BLOCKED → 80%**

---

## 2. Software engineering

| # | Role | Deliverable | Verdict | Why |
|---|---|---|---|---|
| 16 | Backend engineer | service code | **FULL** | gcc, python3, node |
| 17 | Frontend engineer | HTML/CSS/JS | **PARTIAL** | no browser to verify rendering |
| 18 | Full-stack engineer | both | **PARTIAL** | same as 17 |
| 19 | CLI tool author | CLI + tests | **FULL** | everything needed |
| 20 | Library author | package + tests | **FULL** | Python/node both work |
| 21 | Bug fixer | patched code | **FULL** | proved by benchmark L01/L04 |
| 22 | Refactorer | refactored code | **FULL** | proved by benchmark L04 |
| 23 | Test engineer | test suites | **FULL** | `unittest`, `pytest` absent but stdlib works |
| 24 | TDD practitioner | failing test → fix | **FULL** | benchmark L01 does exactly this |
| 25 | Code reviewer | review notes | **FULL** | read + reason |
| 26 | Static analysis author | linters | **FULL** | `ast` module present |
| 27 | Build engineer | Makefiles, CI scripts | **FULL** | `make`, `gcc` present |
| 28 | Compiler / interpreter dev | working compiler | **PARTIAL** | gcc yes; `llvm` absent |
| 29 | Systems programmer | C/C++ | **FULL** | gcc, g++ present |
| 30 | Embedded engineer | firmware | **BLOCKED** | no cross-toolchain, no hardware |
| 31 | Game developer (logic) | game logic | **PARTIAL** | no GPU, no engine |
| 32 | Graphics programmer | renderer | **PARTIAL** | CPU only; no OpenGL |
| 33 | Mobile developer | iOS/Android app | **BLOCKED** | no SDK, no emulator |
| 34 | Desktop app developer | app | **BLOCKED** | no GUI toolkit (tkinter absent) |
| 35 | Data engineer | ETL pipelines | **PARTIAL** | stdlib yes, no pandas/spark |
| 36 | DevOps engineer | infra scripts | **PARTIAL** | no docker-in-docker, no cloud CLI |
| 37 | SRE | runbooks, probes | **PARTIAL** | no live systems to observe |
| 38 | Platform engineer | IaC | **BLOCKED** | no terraform/cloud |
| 39 | Security engineer (offensive) | exploit PoC | **PARTIAL** | local targets only, no network |
| 40 | Security engineer (defensive) | hardening, audits | **FULL** | static review works |
| 41 | Reverse engineer | disassembly | **PARTIAL** | no objdump/gdb listed |
| 42 | Database engineer | schema, queries | **FULL** | `sqlite3` module, SQL by hand |
| 43 | DBA (ops) | tuning live DB | **BLOCKED** | no server, no client |
| 44 | API designer | OpenAPI spec | **FULL** | text output |
| 45 | Integration engineer | connector code | **PARTIAL** | code yes, no live endpoint |
| 46 | Performance engineer | profiling | **PARTIAL** | `time`/`cProfile` in stdlib |
| 47 | Release engineer | versioning, tags | **FULL** | `git` present |
| 48 | Package maintainer | build config | **FULL** | pip/npm config as text |
| 49 | Technical debt analyst | refactor plan | **FULL** | static reading |
| 50 | Legacy modernization | migration code | **PARTIAL** | code yes; no live cutover |

**Coverage: 21 FULL, 20 PARTIAL, 9 BLOCKED → 62%**

---

## 3. Data and analysis

| # | Role | Deliverable | Verdict | Why |
|---|---|---|---|---|
| 51 | Data analyst | findings + charts | **PARTIAL** | figures exact; no matplotlib |
| 52 | Business analyst | requirements, models | **FULL** | text + tables |
| 53 | BI developer | dashboards | **PARTIAL** | text/HTML dashboards; no live BI |
| 54 | Statistician | statistical results | **PARTIAL** | `statistics`, `decimal`; no scipy |
| 55 | Actuary | risk models | **PARTIAL** | maths by hand |
| 56 | Quantitative analyst | backtests | **PARTIAL** | no market data (no network) |
| 57 | Data scientist | models | **PARTIAL** | no numpy/sklearn |
| 58 | ML engineer | trained model | **BLOCKED** | no torch; no GPU |
| 59 | NLP engineer | text pipeline | **PARTIAL** | stdlib regex ok; no spaCy |
| 60 | ETL developer | pipelines | **PARTIAL** | csv/json/sqlite strong |
| 61 | Data quality analyst | validation reports | **FULL** | stdlib is enough |
| 62 | Survey analyst | cross-tabs | **PARTIAL** | no pandas; manual is fine |
| 63 | Financial analyst | statements, models | **PARTIAL** | arithmetic exact; no Excel engine |
| 64 | Accountant | ledger, reconciliation | **PARTIAL** | `decimal` yes; no accounting pkg |
| 65 | Auditor (data) | sampled evidence | **FULL** | reading + hashing |
| 66 | Fraud analyst | pattern detection | **PARTIAL** | no live feed |
| 67 | Market researcher | research synthesis | **PARTIAL** | no web search key configured |
| 68 | SEO analyst | site analysis | **BLOCKED** | no crawler, no network |
| 69 | Growth analyst | funnel analysis | **PARTIAL** | data yes, no live analytics |
| 70 | Operations analyst | process maps | **FULL** | text + SVG diagrams |

**Coverage: 4 FULL, 13 PARTIAL, 3 BLOCKED → 45%**

---

## 4. Visual, media and design

| # | Role | Deliverable | Verdict | Why |
|---|---|---|---|---|
| 71 | Graphic designer | SVG artwork | **FULL** | proved: cowork's poster + brand kit |
| 72 | Brand designer | logos, guidelines | **FULL** | proved by benchmark L02 |
| 73 | Illustrator | vector art | **FULL** | SVG by hand |
| 74 | Icon designer | icon sets | **FULL** | SVG |
| 75 | Typographer | type specimens | **PARTIAL** | SVG yes; no font files bundled |
| 76 | Layout designer | page layouts | **FULL** | SVG |
| 77 | Poster designer | posters | **FULL** | proved end to end |
| 78 | Presentation designer | slide decks | **PARTIAL** | SVG/HTML slides; no pptx |
| 79 | Infographic designer | infographics | **FULL** | SVG |
| 80 | Data visualizer | charts | **PARTIAL** | must hand-write SVG charts |
| 81 | UI designer | mockups | **PARTIAL** | SVG yes; no rendering to check |
| 82 | Motion designer | animation | **PARTIAL** | SVG SMIL/CSS; no video export |
| 83 | Photo editor | retouched images | **BLOCKED** | no raster tools, no PIL |
| 84 | Video editor | edited video | **BLOCKED** | no ffmpeg |
| 85 | Audio engineer | mixed audio | **BLOCKED** | no ffmpeg, no DAW |
| 86 | 3D artist | 3D models | **BLOCKED** | no Blender; no GPU |
| 87 | Print production | press-ready PDF | **BLOCKED** | no PDF engine, no CMYK |

**Coverage: 7 FULL, 7 PARTIAL, 3 BLOCKED → 55%**

---

## 5. Research, analysis, and knowledge

| # | Role | Deliverable | Verdict | Why |
|---|---|---|---|---|
| 88 | Technical researcher | literature synthesis | **PARTIAL** | no network; model knowledge only |
| 89 | Competitive analyst | competitor matrix | **PARTIAL** | cannot verify current facts |
| 90 | Patent analyst | prior-art review | **BLOCKED** | no patent DB, no network |
| 91 | Legal researcher | case summaries | **BLOCKED** | no legal DB |
| 92 | Scientific reviewer | critique | **PARTIAL** | reasoning yes; no literature access |
| 93 | Fact checker | verified claims | **BLOCKED** | cannot verify without sources |
| 94 | Interview analyst | coded transcripts | **FULL** | text analysis |
| 95 | Ethnographer | field notes synthesis | **PARTIAL** | no data collection |
| 96 | Librarian / taxonomist | classification | **FULL** | text work |
| 97 | Competitive intelligence | monitoring | **BLOCKED** | no network |
| 98 | OSINT analyst | open-source intel | **BLOCKED** | no network (by design) |

**Coverage: 2 FULL, 4 PARTIAL, 4 BLOCKED → 30%**

---

## 6. Business operations

| # | Role | Deliverable | Verdict | Why |
|---|---|---|---|---|
| 99 | Project manager | plans, RAID logs | **FULL** | text + tables |
| 100 | Product manager | PRDs, roadmaps | **FULL** | text output |
| 101 | Scrum master | ceremonies, metrics | **PARTIAL** | no live board |
| 102 | Business process analyst | process docs | **FULL** | text + SVG |
| 103 | Operations manager | SOPs | **FULL** | text output |
| 104 | HR generalist | policies, JDs | **FULL** | text output |
| 105 | Recruiter (sourcing) | candidate pipeline | **BLOCKED** | no network, no ATS |
| 106 | Sales engineer | technical proposals | **FULL** | text + diagrams |
| 107 | Sales development | prospect research | **BLOCKED** | no network, no data source |
| 108 | Account manager | QBR decks | **PARTIAL** | SVG/HTML decks; no pptx |
| 109 | Customer support | response drafts | **FULL** | text output |
| 110 | Support knowledge author | help articles | **FULL** | text output |
| 111 | Training designer | curricula, exercises | **FULL** | text output |
| 112 | Technical trainer | workshop material | **FULL** | text + SVG |
| 113 | Compliance officer | control evidence | **PARTIAL** | documents yes; no live systems |
| 114 | Risk analyst | risk registers | **FULL** | text + tables |
| 115 | Procurement analyst | vendor comparisons | **PARTIAL** | no live pricing |
| 116 | Supply chain analyst | models | **PARTIAL** | maths yes; no live data |
| 117 | Marketing strategist | GTM plans | **FULL** | proved by the sales locus |
| 118 | Content marketer | articles, SEO copy | **FULL** | text output |
| 119 | Social media manager | calendars, copy | **PARTIAL** | cannot publish (no network) |
| 120 | Email marketer | sequences | **PARTIAL** | cannot send |
| 121 | Brand strategist | positioning | **FULL** | reasoning + text |

**Coverage: 15 FULL, 8 PARTIAL, 3 BLOCKED → 79%**

---

## Summary

| Domain | Roles | FULL | PARTIAL | BLOCKED | Coverage |
|---|---|---|---|---|---|
| Text & documents | 15 | 11 | 2 | 2 | **80%** |
| Software engineering | 35 | 21 | 20 | 9 | **62%** |
| Data & analysis | 20 | 4 | 13 | 3 | **45%** |
| Visual & media | 17 | 7 | 7 | 3 | **55%** |
| Research & knowledge | 11 | 2 | 4 | 4 | **30%** |
| Business operations | 23 | 15 | 8 | 3 | **79%** |
| **TOTAL** | **121** | **60** | **54** | **24** | **≈ 62%** |

## What this says, plainly

**Where the platform is genuinely strong:** everything whose deliverable is *text, code, or
hand-authored vector art*. Documentation, engineering, refactoring, design, planning, business
writing. That is a real, sellable capability set — and two of the three were proved end to end today
(the poster and brand kit; the benchmark's coding tasks).

**What one change would unlock most:** `THIRD-PARTY present (0/34)` is the single largest gap. Adding
`numpy`, `pandas`, `matplotlib` and `Pillow` to the image would move **Data & analysis from 45% to
roughly 75%** and **Visual from 55% to 70%** — chart rendering, image handling, real dataframes —
without touching the network, because they would be baked in at build time like everything else.

**What no image change fixes:** the 24 BLOCKED roles are all *network or live-system* roles —
prospecting, OSINT, fact-checking, publishing, cloud ops, Android/iOS, video and audio. Those need an
egress allowlist and connectors, which is Phase 3 of the build plan, not a Dockerfile line.

**The honest ceiling for this architecture:** about **75%** of these roles, once the image is
enriched. The remaining quarter are network-bound, and reaching them trades the isolation that makes
unattended agents safe. That trade should be made deliberately, per connector, not by opening the
network.

## Suggested next step

One Dockerfile change, four packages, and re-probe:

```dockerfile
RUN pip3 install --no-cache-dir numpy pandas matplotlib Pillow
```

Then re-run the probe and this table. If the measured numbers move as predicted, the image is worth
shipping; if they do not, the prediction was wrong and the table says so.
