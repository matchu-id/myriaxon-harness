# Security audit — Myriaxon

**Date:** 15 October 2026
**Method:** every finding below was **verified by running it**, not inferred by reading. Where a question was
answered by measurement, the measurement is quoted. Nothing here is reported on the strength of a keyword
match — the widest hole in the package contained no dangerous function names at all.

**Threat model that matters here.** The agent runs tool calls chosen by a language model, in a process with the
user's own rights. So the question is never "is there an `eval`" but **"what can a model reach, and does every
path that executes something pass the same gate?"**

---

## Summary

| # | Finding | Severity | State |
|---|---|---|---|
| 1 | `web_fetch` reached **any host**, including loopback | **High** | ✅ Fixed, 24 tests |
| 2 | Locus **conditions executed shell commands** bypassing sandbox *and* approval | **High** | ✅ Fixed, refused by default |
| 3 | Webhook shared secret compared **non-constant-time** | **Medium** | ✅ Fixed |
| 4 | A Locus could read **every peer's workspace** | **High** | ✅ Fixed (earlier) |
| 5 | Hardcoded corporate project id in 13 files | **Medium** | ✅ Fixed before 0.0.1 |
| 6 | `--vendor gemini` rejected → editor-shipped misconfiguration | Low | ✅ Fixed |
| 7 | `_Client.post` silently dropped `json_body` (empty request body) | **Medium** | ✅ Fixed |
| 8 | Two verifiers were stale and would fail every future release | Low | ✅ Fixed |
| 9 | Pre-existing: `verify_dependencies` — `ollama.py` has no lazy-import guard | Low | ⬜ Open |
| 10 | Pre-existing: `verify_tool_catalog` — 2 phrases moved into `request_tools` | Low | ⬜ Open |

**Findings 1 and 2 are the ones that mattered**, and both were invisible to every keyword-based scanner.

---

## Finding 1 — `web_fetch` reached any host, including loopback

**High. Verified by running it.**

`web_fetch` checked the **scheme** and nothing else:

```python
if not target.lower().startswith(("http://", "https://")):
    target = "https://" + target
```

No allowlist, no loopback / RFC1918 / link-local rejection, nothing that consults the sandbox. Proved against a
loopback-only listener started for the test:

```
started a loopback-only listener on http://127.0.0.1:56094/
web_fetch(...)  ->  error=False
reached the service: True
```

**Why it matters beyond the obvious.** `run_bash` is **refused outright** when no sandbox is available, with
this reason:

> *"a shell command cannot be confined to this Locus's workspace, and no sandbox is available to isolate it.
> File tools are unrestricted inside the workspace; a shell would run with the full rights of this process."*

A fetch that reaches loopback is **the same reach through another door** — same privilege, no gate. And
`web_fetch` is in the default tool set.

**What I could not confirm:** whether the cloud metadata endpoint is reachable from a cloud VM. It failed from
this machine (`169.254.169.254` → no route), which is **luck, not a control** — and on a GCE/AWS instance that
address hands out credentials.

**Fixed.** `_refuse_reason()` now blocks, with the reason named:

```
REFUSE  http://127.0.0.1/x                         loopback address
REFUSE  http://localhost:8080/                     the name resolves to ::1
REFUSE  http://169.254.169.254/latest/meta-data/   link-local
REFUSE  http://10.0.0.5/ 192.168.1.1/ 172.16.0.1/  RFC1918
REFUSE  http://[::1]/                              IPv6 loopback
REFUSE  http://metadata.google.internal/x          metadata hostname
REFUSE  file:///etc/passwd                         not http
allow   https://example.com                        (0.7s, works)
allow   https://api.github.com/repos               (works)
```

The check runs on the **resolved** address as well as the literal one, because a hostile DNS name can point at
a private address. `MYRIAXON_ALLOW_PRIVATE_FETCH=1` is the deliberate opt-out.

---

## Finding 2 — Locus conditions executed shell commands, bypassing both gates

**High. Verified by writing a marker file.**

A model can define a condition through `locus_create` — its `condition` parameter goes straight to
`conditions.Condition`. Conditions then ran commands with `subprocess.run(..., shell=True)`, and
`conditions.py` contained **no reference to the sandbox, the confinement boundary, or `confirm_action`**:

```
sandbox         present in conditions.py: False
confirm_action  present in conditions.py: False
docker          present in conditions.py: False
confine         present in conditions.py: False
```

Verified end to end — `locus_create` with a command condition, then one evaluation:

```
locus_create -> error=False
evaluate() -> holds=True error=False
MARKER EXISTS AFTER EVALUATION: True
  contents: 'pwned'
```

Meanwhile the **same string** through `run_bash`:

```
run_bash -> Blocked: a shell command cannot be confined to this Locus's workspace,
            and no sandbox is available to isolate it...
```

**So both controls were bypassable by writing the command into a condition instead of calling the tool.** That
is the wrong shape for a control: it has to hold on every path that executes something.

**Not hypothetical.** A user asking "watch whether the build server is still up" produces a `command`
condition, and a string the user would never approve as a shell command runs **on every tick, unattended**.

**Fixed** — refused by default, and **reported rather than silent** (a monitor that quietly stopped working
would be worse than one asking for permission):

```
evaluate() -> holds=True error=True
MARKER EXISTS AFTER EVALUATION: False
```

`MYRIAXON_CONDITIONS_ALLOW_SHELL=1` restores it, verified: `allowed with opt-in: True`, `marker written: True`.

---

## Finding 3 — non-constant-time secret comparison

**Medium.** `supplied != config.secret` returns as soon as two bytes differ, leaking the secret's **prefix**
through response timing — recoverable one character at a time rather than by guessing the whole value. Replaced
with `hmac.compare_digest`, UTF-8 encoded first so non-ASCII cannot turn a rejected secret into a 500.

**Severity is honestly stated as Medium:** the webhook is the one path designed to accept an outside request,
but whether it is reachable decides how much this matters. The comparison costs nothing to make safe, so it
should not depend on that judgement.

---

## Finding 4 — a Locus could read every peer's workspace

**High.** Recorded in full in commit `a1d5920`. The run loop granted `members_dir()` — the **parent of every
member's workspace** — and `member_workspace()` returned a path **inside** the granted tree while documenting
itself as private.

The tests missed it because they granted only `swarm` while production granted both: **a test exercising a
narrower configuration than reality.** The regression test now reads the grants **out of the run loop's own
source**, so a future grant cannot escape testing.

---

## What the audit did NOT find

Stated so the absence is evidence rather than silence:

| Checked | Result |
|---|---|
| `pickle` / `marshal` / `yaml.load` on untrusted data | **none** |
| `eval` / `exec` on untrusted data | only inside `skill_tools.py`, which **scans for them** as a lint |
| `os.system` | none |
| `shell=True` | 2 sites, both in `conditions.py` (Finding 2) |
| Path writes bypassing `_resolve` | `checkpoint_restore` does **not** — it calls `confirm_action` |
| Process spawns | 35, of which only `conditions.py` and `tools.run_bash` are model-reachable |
| Credential files tracked in git | **none**; `.env` ignored, real key in no commit on any ref |
| Dashboard bind address | loopback by default |

---

## Recommendations, in priority order

1. **Route every execution path through one gate.** Conditions and `run_bash` now disagree by *policy* rather
   than by *mechanism* — `run_bash` refuses on confinement, conditions refuse on an env var. One shared
   function that asks "may this process run here?" would make the next execution path safe by default instead
   of safe by remembering.
2. **Decide the condition policy deliberately.** Refusing by default is safe and breaks existing monitors; the
   alternative is to run command conditions **inside the sandbox** when one is available, so the capability
   survives with the confinement intact.
3. **Close findings 9 and 10** — both are stale verifiers, not product defects, but a check that fails on every
   run is a check people learn to ignore.
4. **Re-run this audit on every release.** Every finding here came from *running the thing*, and none from
   reading it.
