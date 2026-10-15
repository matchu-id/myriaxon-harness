# Putting in an API key

Four ways to give Myriaxon a credential, in the order most people want them. Pick one; you do not need
several, and having several is the main cause of "it is using the wrong provider".

| Where | Survives a restart | Right for |
|---|---|---|
| The setup wizard (`myriaxon --reauth`) | yes, saved to `~/.myriaxon/config.json` | one machine, one provider |
| A `.env` file beside your project | yes, per project | different providers per project; keeping keys out of a JSON file |
| An environment variable | for the session | CI, containers, a key already exported for something else |
| A flag on the command line | no | trying a provider once |

## Which outranks which

```
   --vendor / --model          a flag wins for that run
        ↓
   .env  →  the real environment
        ↓
   ~/.myriaxon/config.json     what the wizard saved
```

**A `.env` outranks a saved choice.** If you set a provider in `.env` and later pick a different one in the
wizard, the `.env` still wins on the next run. That is usually what a per-project file is for, and it is
surprising exactly once.

**Nothing else is consulted.** There is no global override, and no hidden fallback to a key you forgot about —
which is deliberate, because "it authenticated as somebody else" is a much worse failure than "no key found".

---

## DeepSeek

DeepSeek has its own API at `api.deepseek.com`. The key is `DEEPSEEK_API_KEY`, and the models are
`deepseek-v4-flash` and `deepseek-v4-pro`.

### The quick way

```powershell
myriaxon --vendor deepseek
```

`--vendor deepseek` looks for `DEEPSEEK_API_KEY` in the environment, then in `.env`, then in the saved config.
If it finds none it **asks you for one at that moment** rather than failing with a 401 in the middle of your
first real task, and it remembers the answer.

### The explicit way

```powershell
# Windows
" DEEPSEEK_API_KEY=sk-your-key-here" -replace '^ ', '' | Out-File -Encoding utf8 .env
```

```bash
# macOS / Linux
echo 'DEEPSEEK_API_KEY=sk-your-key-here' >> .env
```

Then name the provider so the key is used with the endpoint it belongs to:

```bash
# .env
DEEPSEEK_API_KEY=sk-your-key-here
MYRIAXON_PROVIDER=deepseek
MYRIAXON_MODEL=deepseek-v4-flash
```

```bash
myriaxon                 # the interactive REPL, on DeepSeek
myriaxon -p "explain main.py"
```

**Do not set `MYRIAXON_BASE_URL` for DeepSeek.** The built-in endpoint is already correct, and pointing a keyed
provider at a hand-typed host is how a request ends up somewhere you did not intend.

### Check it

```bash
myriaxon --models        # lists what your key can actually reach
myriaxon doctor          # says which provider is live and whether the key resolves
```

---

## Netra

Netra (`api.netraruntime.com`) is a gateway that serves **the same models** as DeepSeek's own API. The key is
`NETRA_API_KEY`, and the models are spelled with a vendor prefix:
`deepseek/deepseek-v4.1-flash` and `deepseek/deepseek-v4-flash-0731`.

The distinction matters: the **endpoint, the model name and the credential all change together**. A DeepSeek
key against the Netra host is a 401, and a Netra key against `api.deepseek.com` is a 401 that looks like a
billing problem.

### The quick way

```powershell
myriaxon --vendor netra
```

### The explicit way

```bash
# .env
NETRA_API_KEY=sk_live_your-key-here
MYRIAXON_PROVIDER=netra
MYRIAXON_MODEL=deepseek/deepseek-v4.1-flash
```

```bash
myriaxon
```

### Check it

```bash
myriaxon --models
```

Against Netra this answers with the two DeepSeek models above. If it answers with an empty list, the key is
wrong for that host — the endpoint itself is reachable, which is why the error is not a timeout.

---

## Which should you use?

| | DeepSeek direct | Netra |
|---|---|---|
| Endpoint | `api.deepseek.com` | `api.netraruntime.com` |
| Model spelling | `deepseek-v4-flash` | `deepseek/deepseek-v4.1-flash` |
| Key variable | `DEEPSEEK_API_KEY` | `NETRA_API_KEY` |
| Use it when | you have a DeepSeek account | you have a Netra key, or you want the same models **and** others from one credential |

Both are OpenAI-compatible, so **tool calling, streaming and usage accounting behave identically** — measured
on both, including multi-tool turns.

---

## Gemini, if you have a corporate Google account

Different enough to have its own document: **[VERTEX-SETUP.md](VERTEX-SETUP.md)**. The short version:

```bash
GOOGLE_GENAI_USE_VERTEXAI=true
GOOGLE_CLOUD_PROJECT=your-project-id
GOOGLE_CLOUD_LOCATION=global
```

**No API key at all** — the credential is your `gcloud auth` login.

---

## Ollama, for no key at all

```bash
myriaxon --ollama
```

Myriaxon detects what is installed and picks a model. Nothing to configure.

---

## When it does not work

| What you see | What it means |
|---|---|
| `missing or malformed key` (401) | the key is absent, or it belongs to a **different** host. Check that the provider and the key variable match the table above. |
| `model not found` (404) | the key is fine and the model name is not. `--models` shows the real names, including any vendor prefix. |
| `API keys are not supported by this API` | you are in Vertex mode with a key set. Vertex wants an OAuth login, not a key. |
| It uses a different provider than you chose | `.env` outranks the saved config. See the precedence table at the top. |

### Where a key is actually stored

`~/.myriaxon/config.json`, in plain text, when you use the wizard. That is a normal trade for a CLI and worth
knowing before you paste a production key into it. If you would rather it were not on disk, use `.env` or an
environment variable and never run the wizard's key step.

### Where a key is never written

- **Not into the audit log.** `audit.jsonl` records tool calls with credentials redacted.
- **Not into the transparency feed.** Redaction happens at emission, before an event is stored or sent to the
  dashboard. Verified by test: a key passed as a tool argument arrives in the UI as `<redacted>`.
- **Not into a session transcript** — session files and the export path both redact.
- **Not into git.** `.env` is gitignored, including `.env.*` variants, and `.env.example` ships instead.

### Never do this

Do not put a key in `.env.example`. It is tracked by git, and its whole purpose is to be committed.
