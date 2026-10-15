# How to use Myriaxon

Everything from "I just installed it" to "it is working on my behalf while I am asleep". Every command here
was run before being written down.

**New to it?** Read [Install](#1-install) and [First run](#2-first-run), then stop. The rest is reference.

---

## 1. Install

Requires **Python 3.10 or newer**.

```bash
pip install "myriaxon-harness[all] @ git+https://github.com/matchu-id/myriaxon-harness.git@v0.0.1"
```

Or from a clone, which is what to do if you intend to change anything:

```bash
git clone https://github.com/matchu-id/myriaxon-harness.git
cd myriaxon-harness
pip install ".[all]"
```

The distribution is `myriaxon-harness`; the command and the import package are both `myriaxon`.

```bash
myriaxon --version
```

---

## 2. First run

### Give it a key

```bash
myriaxon --reauth
```

The wizard asks which provider, then for the key, and remembers both. **That is the whole setup.**

Prefer a file? **[API-KEYS.md](API-KEYS.md)** covers DeepSeek, Netra, Gemini/Vertex, Ollama and the precedence
rules between `.env`, the saved config and flags.

### Start it

```bash
myriaxon                              # the interactive REPL
myriaxon -p "explain main.py"         # one-shot, non-interactive
myriaxon web                          # the dashboard, at http://127.0.0.1:8765
```

The dashboard is the better first experience: it opens on **mission control**, which answers "what is going on"
without being asked.

---

## 3. The dashboard

```bash
myriaxon web                 # opens a browser
myriaxon web --no-browser    # just serve
myriaxon web --port 9000     # a different port
```

It binds `127.0.0.1` — reachable from this machine only.

| View | What it is for |
|---|---|
| **Mission control** | The landing view: provider health, usage, active runs, the decisions queue, the live activity feed, the transparency dial |
| **Chat** | The same agent as the REPL, with the trajectory beside it |
| **Loci** | The always-on agents, what each is working on, and what it has actually done |
| **Files** | Browse and diff the workspace |
| **Approvals** | Objectives a locus marked blocked, and sessions that ended in an error |

**The supervisor runs while the dashboard is open.** Every 60 seconds it runs any Locus that is due, so closing
the tab stops the schedule. Set `MYRIAXON_NO_SUPERVISOR=1` to hold it off, or
`MYRIAXON_SUPERVISOR_SECONDS=300` to slow it down.

---

## 4. Seeing what it is doing — the transparency dial

This is the feature to reach for when you want to trust it rather than hope. Every read, write, command, refusal
and coordination step is published to a live feed, filtered by a dial from 0 to 8.

| Level | You see |
|---|---|
| 0 | nothing at all |
| 1 | a run exists and is progressing |
| 2 | what the agent says it is about to do |
| 3 | which tool was called, arguments redacted |
| 4 | **which files were read and written** |
| 5 | **shell commands, exit codes, output tails** |
| 6 | **loci and swarm coordination** |
| 7 | **the gates: refusals, confirmations, risk verdicts, budget stops** |
| 8 | **durable writes: sessions, checkpoints, memory, audit** |

Each level includes everything below it, so turning it up never hides something a lower setting showed.

### Move it

- **In the dashboard**: the Transparency panel on mission control has a slider and a legend.
- **From the shell**: `MYRIAXON_TRANSPARENCY=7 myriaxon web`
- **Live, over HTTP**, which is what the slider does:

```bash
curl -X POST http://127.0.0.1:8765/api/transparency -H 'Content-Type: application/json' -d '{"level":8}'
curl http://127.0.0.1:8765/api/transparency      # the dial plus the recent feed
curl -N http://127.0.0.1:8765/api/transparency/stream   # live, server-sent events
```

### What a real turn looks like in the feed

Measured, on a prompt asking it to write a file and read it back:

```
L3 actions   write_file
L4 files     created ui-proof.txt        op=write
L3 actions   read_file
L4 files     read ui-proof.txt           op=read
L7 decisions run_bash: refused           allowed=False
L5 system    $ echo hi                   exit=None
```

**Level 7 is the one usually missing elsewhere** and the one that answers "why did it *not* do the thing". A
refused command shows `exit=None` rather than `0`, because it never ran.

### Two guarantees worth knowing

- **A credential never reaches the feed.** Redaction happens when the event is created, not when it is drawn.
  A key passed as a tool argument arrives as `<redacted>`.
- **Model text is marked untrusted.** A path or command that came from a model is rendered as data, so a file's
  contents cannot act on the page displaying it.

---

## 5. Loci — agents that work between sessions

A **Locus** is a named agent with its own workspace, its own objectives and its own step budget. It runs on a
timer, on a condition, or when you message it, and it survives a restart mid-run.

```bash
myriaxon loci --status       # what exists, and what is due
myriaxon loci --run          # run everything due right now, then exit
myriaxon loci --watch        # keep running what comes due, in this terminal
```

Create one by asking the agent in a session ("make a locus called sales that watches …"), or through the
dashboard's Loci view.

### How a Locus differs from a sub-agent

| | A sub-agent (`task`) | A Locus |
|---|---|---|
| Lifetime | one call | until you delete it |
| Memory | none between calls | objectives, activity, its own workspace |
| Runs when | you ask | on a timer, a condition, or a message |
| Confinement | shares your workspace | **its own**, enforced |

A Locus runs **confined**. It can read and write inside its own folder and nowhere else, and it asks nobody for
permission because there is nobody to ask — the boundary is enforced rather than requested. That is also why it
**cannot run shell commands unless a sandbox is available**: a shell is not confined by a path check, so
allowing one would hand it the whole machine under the name of isolation. Install Docker (or enable Windows
Sandbox) if a Locus needs a shell.

---

## 6. Several agents at once — the swarm board

When two or more loci work the same problem, the board is how they hand work to each other.

```
~/.myriaxon/swarm/
    board/<task>/     item.json · owner.json · result.md · done.json
    shared/           artifacts any member may read
    members/          reserved; NOT private — inside the tree every member can reach
```

Three properties, each the thing a naive implementation gets wrong:

- **Two members cannot both claim a task** — settled by an exclusive create, not by read-then-write.
- **A dead member does not block the board** — leases expire and are reclaimable, and reclaiming is reported.
- **Done means the result is readable** — `done.json` is written last.

Plus **collective memory**: a finding shared by one member is retrievable by every member, with the author
attached so a reader can ask how it is known.

Nothing to configure. Loci reach the board on their own.

---

## 7. Everyday commands

```bash
myriaxon                                  # interactive
myriaxon -p "fix the failing test"        # one-shot
myriaxon --models                         # what your key can actually reach
myriaxon doctor                           # which provider is live, and whether it works
myriaxon loci --status                    # the always-on agents
myriaxon bench lint                       # validate the evaluation suite, free, no API calls
```

In the REPL, `/help` lists the slash commands. The ones worth knowing first:

| Command | Does |
|---|---|
| `/mode` | `manual`, `auto_edit`, `plan`, `auto` — how much it may do without asking |
| `/vendor` | switch provider mid-session |
| `/compact` | summarise the conversation to free context |
| `/yolo` | skip confirmations (still blocked in `plan` mode) |

---

## 8. Permissions and safety

| Mode | File edits | Shell |
|---|---|---|
| `manual` (default) | confirm | confirm |
| `auto_edit` | auto-approve | confirm |
| `plan` | **blocked** | **blocked** |
| `auto` | auto-approve | auto-approve unless the command looks destructive |

**Every path that runs a shell asks one shared question** — `tools.shell_may_run()` — so a control cannot hold
on one path and not another. That is not a design preference; the audit that found the hole is in
[SECURITY-AUDIT.md](SECURITY-AUDIT.md), and it exists because a *condition* could run a shell that `run_bash`
would have refused.

What bounds each channel:

| Channel | Bound by |
|---|---|
| File tools | the resolved path must be inside the workspace |
| Shell | refused in a confined agent without a real sandbox |
| `web_fetch` | loopback, private and cloud-metadata addresses refused |
| Outbound messaging | a shared secret, compared in constant time |
| MCP / client schemas | never indexed, so a hostile server cannot seed the catalogue |

The risky-command heuristic (`rm -rf`, force-push, disk formatting) is **best-effort, not a security
boundary** — its own docstring says so.

---

## 9. Where things live

| Path | What |
|---|---|
| `~/.myriaxon/config.json` | the saved provider and key |
| `~/.myriaxon/sessions/` | conversations |
| `~/.myriaxon/loci/` | each Locus's workspace |
| `~/.myriaxon/swarm/` | the board and shared memory |
| `~/.myriaxon/audit.jsonl` | every tool call, credentials redacted |
| `~/.myriaxon/usage.jsonl` | tokens and requests |
| `~/.myriaxon/settings.json` | your policy: permission rules, enforced mode |
| `<project>/.myriaxon/settings.json` | the repository's policy — safe to commit |

`MYRIAXON_CONFIG_DIR` moves the whole tree.

---

## 10. When it does not work

| Symptom | Cause |
|---|---|
| `missing or malformed key` (401) | the key belongs to a different host than the provider names |
| `model not found` (404) | the model name is wrong. `myriaxon --models` shows the real ones, including any `vendor/` prefix |
| `API keys are not supported by this API` | Vertex mode with a key set — Vertex wants an OAuth login |
| A Locus refuses every shell command | no sandbox available. Install Docker, or give it work the file tools can do |
| The feed is empty | the dial is at 0, or below the layer you are watching for. Try `MYRIAXON_TRANSPARENCY=8` |
| Loci never run on their own | the supervisor only runs while the dashboard is open, or use `loci --watch` |
| It picked the wrong provider | `.env` outranks the saved config. See the precedence table in [API-KEYS.md](API-KEYS.md) |

`myriaxon doctor` answers most of these directly.

---

## 11. Going further

| Document | Contents |
|---|---|
| [API-KEYS.md](API-KEYS.md) | DeepSeek, Netra, Gemini/Vertex, Ollama; precedence; where a key is and is not written |
| [VERTEX-SETUP.md](VERTEX-SETUP.md) | Gemini through a corporate Google account, no API key |
| [MCP-SETUP.md](MCP-SETUP.md) | connecting stdio and remote MCP servers |
| [PLUGINS.md](PLUGINS.md) | writing a plugin |
| [MESSAGING.md](MESSAGING.md) | WhatsApp / messaging integration |
| [SECURITY-AUDIT.md](SECURITY-AUDIT.md) | what was found, how it was proved, what is still open |
| [Level4-5-STATUS.md](Level4-5-STATUS.md) | every autonomy requirement with the commit that satisfies it |
| [RECOVERY.md](RECOVERY.md) | what to do when a run is interrupted |

### Checking your own copy

```bash
python tools/run_checks.py               # all verifiers plus the test suite
python tools/bench_l4_l5.py              # the functional autonomy bench, with evidence per line
python tools/verify_binary_identity.py   # is the installed launcher genuine?
python tools/browser/wall_check.py       # drive the dashboard in real Chrome and check it
```

All run offline and cost nothing. The browser checks need Chrome and node, and **skip** rather than fail if
either is missing.

### If an antivirus flags `myriaxon.exe`

Read **[FALSE-POSITIVE.md](FALSE-POSITIVE.md)**. The short version: pip generates that file, this repository
commits no executables, and it is unsigned with no version information Windows can read — which is genuinely
what malware looks like, and is why it can be flagged. The document has the two PowerShell commands that settle
it, and `tools/verify_binary_identity.py` prints the same thing with the numbers a vendor report needs.

### Known limitations, stated rather than discovered by you

- **Gemini 3.x multi-tool turns can fail on the second turn.** Vertex returns a `thought_signature` on some
  turns and not others; an unsigned turn cannot be replayed. Documented with its measurements in
  [GEMINI-MULTITOOL-STATUS.md](GEMINI-MULTITOOL-STATUS.md).
- **Declared uncertainty cannot be closed.** Myriaxon records whether a claim was `checked`, `inferred` or
  `assumed` and requires evidence for the first two. It cannot make a model feel doubt, and says so.
- **A Locus cannot mark its own objective done.** A self-directed run marks finished work `blocked` with a note
  for you to confirm. That is deliberate: an agent that grades its own homework is one you cannot trust the
  grade from.
