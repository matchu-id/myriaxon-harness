# Using a corporate GCP Gemini account with Myriaxon

Myriaxon can use a corporate Google account through **Vertex AI** — no API key, billed to your GCP
project. Two credential types are supported and they are **not** interchangeable:

| | Federated (WIF) — what you want | Application-default |
|---|---|---|
| Identity | your workforce identity in your own tenant, via SAML | a Google account with project access |
| Credential type | `external_account` (audience = the workforce pool) | `authorized_user` (OAuth refresh token) |
| Created by | `gcloud auth login --login-config=…` or `auth login --workforce` | `gcloud auth application-default login` |
| Reached from | a browser SAML round trip with your own IdP | a Google sign-in page |

**The identifiers below are placeholders.** Everything specific to an organisation — tenant, pool name,
signed-in account, project id — has been replaced with `YOUR_…`, because those values differ for every
reader and are not Myriaxon's to publish. Substitute your own.

## Configuration

```powershell
$env:GOOGLE_GENAI_USE_VERTEXAI = "true"
$env:GOOGLE_CLOUD_PROJECT       = "YOUR_PROJECT_ID"      # or MYRIAXON_VERTEX_PROJECT
$env:GOOGLE_CLOUD_LOCATION      = "global"               # a region also works
```

Nothing is baked into the package. `myriaxon.vertex_auth` reads the project from the environment and, when
none is set, raises a message naming the variable rather than sending a request to a project nobody can read.

## Step 1 — get the workforce login configuration file

The file describes your pool and provider to gcloud. It is tenant data and cannot be pulled without your
own Cloud access:

* Cloud console → **IAM & Admin → Workforce Identity Federation** → your pool → provider →
  **Download login configuration**, or
* ask your GCP admin for the pool's login config, or
* (with sufficient IAM) `gcloud iam workforce-pools create-login-config`.

Save it somewhere convenient, e.g. `%USERPROFILE%\Downloads\workforce-login-config.json`.
`scripts/wif_login.py` searches the usual locations; set `MYRIAXON_WIF_POOL` if yours differs.

## Step 2 — sign in (one browser trip)

Run **in your own terminal**, not a sandboxed shell:

```powershell
python scripts/wif_login.py login     # prints the exact commands for this machine
```

which is either:

```powershell
& "$env:LOCALAPPDATA\Google\Cloud SDK\google-cloud-sdk\bin\gcloud.cmd" `
  auth login --login-config="$env:USERPROFILE\Downloads\workforce-login-config.json" --update-adc
```

or, without the file, by naming the audience explicitly:

```powershell
& "$env:LOCALAPPDATA\Google\Cloud SDK\google-cloud-sdk\bin\gcloud.cmd" config set `
  auth/workforce_pool_audience "//iam.googleapis.com/locations/global/workforcePools/YOUR_POOL/providers/YOUR_PROVIDER"
& "$env:LOCALAPPDATA\Google\Cloud SDK\google-cloud-sdk\bin\gcloud.cmd" auth login --workforce
```

A browser opens to your IdP, you approve, and gcloud writes the credential. `--update-adc` is the part
that makes Myriaxon (and any `google-auth` based tool) pick it up.

If the redirect hangs on a managed browser, add `--no-launch-browser` and paste the printed URL into
your work browser manually.

## Step 3 — verify

```powershell
python scripts/wif_login.py verify
```

Expected: the token refreshes and `gemini-3.8-flash` answers. Then run Myriaxon:

```powershell
python -m myriaxon --model gemini-3.8-flash
```

## Speed: where the time actually goes

`gemini-3.8-flash` on Vertex has a **provider-side cold start of ~30-38s on the first request of a
process**; every request after that is ~1.4-2s. This is measured, reproduced, and not caused by
Myriaxon:

```
=== one-shot gemini-3.8-flash accounting ===
  import google.genai                                12.15s
  Client() constructed                                2.17s
  FIRST request (37.6s -- provider cold start)       37.63s
  SECOND request (1.4s -- warm)                       1.37s
```

Backgrounding a warm-up request does **not** help: the provider serialises the two requests, so
starting one early saves ~2s of ~34s (measured both ways in `scripts/measure_warmup.py`). The only
real lever is to keep a process alive -- in the REPL, turn 2 costs 1.8s against turn 1's 29.2s.

What *was* controllable and is now fixed (each measured):

| Cost | Before | After | How |
|---|---|---|---|
| `myriaxon --help` | 11.0s | **1.7s** | `--help`/`--version` answered by a lightweight parser; the heavy CLI is never imported |
| Model-limits lookup on Vertex | **33.8s** | 0s | the SDK never honoured the 3s timeout, so it is skipped when the window is already known locally |
| `import markdown_it` at startup | 2.5s | 0s | Markdown rendering deferred to first actual use |
| Provider SDKs imported per run | all 3 | 1 | only the configured provider is imported; the other two were imported by error-classification code even on success |
| Total controllable startup | ~50s | **~14s** | the remainder is the SDK's own 12s import plus 2s client construction |

Verify any of this yourself:

```powershell
$env:MYRIAXON_TIMING = "1"          # prints a per-phase table to stderr
python -m myriaxon -p "say hi" --model gemini-3.8-flash
python scripts/final_timing.py        # model-side accounting
python scripts/measure_warmup.py      # warm-up: measured, not assumed
python scripts/timing_breakdown.py    # imports vs model latency
```

`--help` being fast matters more than it sounds: it is the first thing a new user runs, and it used
to cost a full agent-stack import to print usage text.

## Models, as measured on one project

Availability is **per project**, so treat this as a starting point and re-check on yours:

| Model | Vertex (`global` and regional) |
|---|---|
| `gemini-3.8-flash` | **works** |
| `gemini-3.7-flash` | works |
| `gemini-2.5-pro` / `gemini-2.5-flash` | works |
| `gemini-2.0-flash`, `gemini-1.5-flash` | not found in the project these were measured on |

Protocol is chosen per model automatically (`gemini-3.8-flash` → Interactions;
`gemini-2.5-*` → generateContent), because Vertex serves different API surfaces for the two lines —
measurably: a `gemini-2.5-flash` request over the Interactions API is rejected outright. Override
with `--gemini-protocol` or `MYRIAXON_GEMINI_PROTOCOL` if a model on your tenant behaves differently.

Re-check availability any time:

```powershell
python scripts/probe_vertex_models.py
python scripts/check_gemini_protocol.py
```

## Why it previously failed (for the record)

Three separate defects, all fixed:

1. **The stored API key was sent in Vertex mode.** The SDK then preferred key auth and Google
   rejected it: *"API keys are not supported by this API. Expected OAuth2 access token"*. A key left in
   `~/.myriaxon/config.json` was enough to trigger it. Vertex mode now clears the key and says so.
2. **`GOOGLE_GENAI_USE_VERTEXAI=true` was never read.** The constant existed but nothing consulted
   it, so setting the flag alone silently did nothing. It is now honoured.
3. **The Gemini agent only spoke the Interactions API**, which Vertex does not serve for
   `gemini-2.5-*` — requests were rejected with *"Unsupported model interaction"*. The agent now
   supports both protocols and picks per model (`gemini-3.x` → Interactions, everything else →
   generateContent), overridable with `--gemini-protocol` or `MYRIAXON_GEMINI_PROTOCOL`.
