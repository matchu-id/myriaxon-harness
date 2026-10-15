# Installing Myriaxon

Myriaxon runs on **Python 3.10 or newer**. There is nothing else to install by hand, no Node.js,
no build step, and no package manager beyond pip.

Pick the route that matches how you intend to use it.

| I want to… | Use |
|---|---|
| Try it on this machine, editing the source | [Guided installer](#1-guided-installer-recommended) |
| Install it as a normal Python package | [pip](#2-pip) |
| Run it with no Python installed at all | [Standalone binary](#4-standalone-binary) |
| Use a specific provider (OpenAI, Gemini, DeepSeek, OpenRouter, Netra) | [Provider setup](#5-provider-setup) |

---

## 1. Guided installer (recommended)

Creates a private virtualenv at `./.venv`, installs Myriaxon into it, verifies the import, and
prints the commands to start. Nothing is written outside this folder.

### Windows

```powershell
.\install.ps1
```

If PowerShell refuses to run the script, bypass the policy **for this one invocation** rather
than changing a machine-wide setting:

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

Useful options:

```powershell
.\install.ps1 -Extras all      # every provider SDK: openai, gemini, mcp, keyring
.\install.ps1 -Verify          # run the offline test suite afterwards
.\install.ps1 -Clean           # discard an existing .venv and start over
.\install.ps1 -Python python3.12
```

### Linux / macOS

```bash
chmod +x install.sh
./install.sh
```

```bash
./install.sh --extras all      # every provider SDK
./install.sh --verify          # run the offline test suite afterwards
./install.sh --clean           # discard an existing .venv and start over
```

### What the installer checks, and why

- **Your Python version**, before doing anything else, so a 3.9 interpreter fails with a clear
  message instead of a confusing syntax error three steps later.
- **Whether an existing `.venv` is actually alive.** A virtualenv records the interpreter that
  built it. Copy the folder to another machine — or open the project on a machine whose user
  profile is named differently — and the venv is dead while still looking present, failing with
  `No Python at '...\python.exe'`. The installer tests it and rebuilds rather than proceeding.
- **Non-ASCII paths.** A path containing characters outside cp1252 (a Japanese `ドキュメント`
  folder, for instance) breaks `pip install -e .`, because setuptools writes its editable
  redirect using the console's locale encoding. The installer detects that failure and writes
  the equivalent file itself in UTF-8. Both routes end in the same place; see
  [troubleshooting](#non-ascii-folder-names-break-pip-install--e).

---

## 2. pip

Inside a virtualenv you manage yourself:

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e .                   # editable, for working on the source
```

Or from a checkout without activating anything:

```bash
pip install .
```

Optional extras:

```bash
pip install -e ".[all]"            # every provider, MCP, and the OS keychain
pip install -e ".[openai]"         # OpenAI-compatible SDK
pip install -e ".[gemini]"         # Google Gemini / Vertex
pip install -e ".[mcp]"            # Model Context Protocol servers
pip install -e ".[keychain]"       # store API keys in the OS credential manager
pip install -e ".[dev]"            # pytest
```

The **base install is complete** — Anthropic, the OpenAI-compatible HTTP transport, MCP over
stdio and remote HTTP, the web dashboard, the evaluation harness and the built-in skill all
work without any extra. The extras add provider SDKs and the OS keychain; Myriaxon talks to
OpenAI-compatible endpoints over its own `httpx` client either way, so `.[openai]` is a
fallback rather than a requirement.

### Installing a prebuilt wheel

If you have `myriaxon-<version>-py3-none-any.whl` — from a release, or built yourself as below —
install it directly. It is a single file with no build step, which makes it the fastest and most
predictable route:

```bash
python -m pip install myriaxon_harness-*.whl
```

Add the extras in the same command if you want them:

```bash
python -m pip install "myriaxon_harness-*.whl[all]"
```

### The `.tar.gz` alone: possible, with one real caveat

`python -m pip install myriaxon_harness-*.tar.gz` works, but **a source distribution needs a build
backend, and `pip` fetches one over the network even when `setuptools` is already installed.** That
is standard PEP 517 behaviour, not a defect in this package, but it is surprising enough to state
plainly:

| Situation | `.whl` | `.tar.gz` |
|---|---|---|
| Online | works | works (pip downloads `setuptools>=77`) |
| **Offline** | **works** | **fails** — `Could not find a version that satisfies the requirement setuptools>=77` |
| Needs a build backend | no | yes |
| Size | 824 KB | 878 KB |

Measured on a clean virtualenv: a bare `pip install myriaxon_harness-*.tar.gz --no-index` exits 1 with
`Installing build dependencies: finished with status 'error'`. The same venv installs the wheel with
`--no-index` and no build step at all.

**Two ways to install from the `.tar.gz` with no network:**

```bash
# 1. Use the backend already on the machine. Needs setuptools>=77 in the ACTIVE environment.
python -m pip install --no-build-isolation myriaxon_harness-*.tar.gz

# 2. Unpack it and build in place -- what the project's own verifiers do, and it needs nothing
#    beyond a setuptools that is already present.
tar -xzf myriaxon_harness-*.tar.gz && cd myriaxon_harness-*
python -c "from setuptools import build_meta as bm; print(bm.build_wheel('../dist'))"
python -m pip install ../dist/myriaxon_harness-*.whl
```

`--no-build-isolation` with an older setuptools fails with
`configuration error: 'project.license' must be valid exactly by one definition` — the SPDX string
form of `project.license` needs setuptools **77** or newer, which is why `pyproject.toml` states
`setuptools>=77` rather than the `>=68` it used to claim. That older floor advertised compatibility
with interpreters on which the build could not succeed, and the failure it produced named a licence
field instead of a version.

**For an end user, use the `.whl`.** The `.tar.gz` exists for a rebuild, an audited install, or a
platform with no wheel — none of which need it to be the first thing you try.

### Building the wheel and sdist yourself

```bash
python -m pip install build
python -m build                    # writes dist/myriaxon-<version>-py3-none-any.whl and .tar.gz
```

`python -m build` needs the `wheel` package too. If it is missing, the build stops with
`Unmet dependencies ... wheel` rather than falling back, so either install it
(`python -m pip install wheel`) or skip the build frontend and call the backend directly, which
is what the build frontend does anyway:

```bash
python -c "from setuptools import build_meta as bm; print(bm.build_wheel('dist'))"
python -c "from setuptools import build_meta as bm; print(bm.build_sdist('dist'))"
```

That second form takes about a second here and needs nothing beyond `setuptools`, which is why the
project's own verifiers use it.

### What must NOT reach a release

`MANIFEST.in` bounds the source distribution. This repository keeps roughly 1 GB of extracted
rival-harness trees, several virtualenvs, package-manager caches and a `.myriaxon/` directory
holding real transcripts — none of which belongs in a release, and the last of which would be a
privacy failure rather than a packaging one. `tools/verify_packaging.py` and
`tools/verify_sdist.py` assert their absence on the built artifacts rather than trusting the file.

---

## 3. First run

```bash
python -m myriaxon auth       # choose a provider and paste an API key
python -m myriaxon            # terminal agent
python -m myriaxon web        # web dashboard at http://127.0.0.1:8765
```

Configuration lives in `~/.myriaxon/` (override with `MYRIAXON_CONFIG_DIR`). If you used an
earlier build of this tool, a pre-existing `~/.iohcode/` is adopted automatically on first run —
your provider, API key, MCP servers, messaging credentials and memory database all carry over.

---

## 4. Standalone binary

Builds a single file that needs no Python on the target machine:

```bash
pip install -e ".[build]"
python scripts/build_binary.py
```

The result is `dist/myriaxon` (`dist\myriaxon.exe` on Windows), roughly 60–90 MB depending on
which provider SDKs are present. It bundles the built-in skill, the IQB-14 task suite and the
brand assets.

---

## 5. Provider setup

`python -m myriaxon auth` walks through this interactively. For a non-interactive environment,
set environment variables instead — these always win over the stored config:

| Variable | Meaning |
|---|---|
| `MYRIAXON_PROVIDER` | `anthropic`, `openai_compat`, `openrouter`, `deepseek`, `netra`, `gemini` |
| `MYRIAXON_API_KEY` | The key for whichever provider is selected |
| `MYRIAXON_BASE_URL` | Endpoint override (required for `openai_compat`) |
| `MYRIAXON_MODEL` | Model id |
| `MYRIAXON_FALLBACK_MODEL` | Retried once on a rate-limit error |
| `MYRIAXON_CONFIG_DIR` | Where state lives (default `~/.myriaxon`) |

Provider-native key names are also read as a fallback: `OPENROUTER_API_KEY`,
`DEEPSEEK_API_KEY`, `NETRA_API_KEY`, `GEMINI_API_KEY`.

For Google Vertex AI (workforce identity, corporate credentials) see
[docs/VERTEX-SETUP.md](docs/VERTEX-SETUP.md).

---

## 6. Verifying an install

```bash
python -m pytest tests/ -o addopts= -q        # 470 offline tests, no network, no API key
python scripts/run_dashboard_checks.py        # the full dashboard suite
```

Both must pass with no credentials present. That property is deliberate: a harness that cannot
be tested without spending money does not get tested.

---

## Troubleshooting

### `No Python at '...\python.exe'` when running anything in `.venv`

The virtualenv was built by an interpreter that no longer exists — usually because the folder
was copied from another machine. Delete `.venv` and re-run the installer:

```powershell
Remove-Item -Recurse -Force .venv; .\install.ps1
```

### Non-ASCII folder names break `pip install -e .`

```
UnicodeEncodeError: 'charmap' codec can't encode characters in position 24-29
```

setuptools writes its editable redirect in the console's locale encoding, which cannot represent
the path. Run the installer instead — it detects a non-ASCII path and reaches the same end state
without going through that step:

```powershell
.\install.ps1
```

To do it by hand, install the dependencies and write the redirect yourself:

```powershell
python -m pip install "anthropic>=0.60,<1.0" "rich>=13.7" "pygments>=2.17" "prompt_toolkit>=3.0" "httpx>=0.27"
$repo = (Get-Location).Path
$sp = ".venv\Lib\site-packages"          # POSIX: .venv/lib/python3.x/site-packages
[System.IO.File]::WriteAllText("$sp\myriaxon.pth", "$repo`n", (New-Object System.Text.UTF8Encoding($false)))
```

> Note the `site-packages` path. Inside a virtualenv, `site.getsitepackages()[0]` can return the
> virtualenv **root**, not `site-packages` — a `.pth` written there is never read, and the install
> silently does nothing. If you script this, verify the directory contains other packages (a
> `rich` folder, for example) before writing to it. `tools/install_dev.py` does exactly that.

### The install is extremely slow, or appears to hang

A project directory on OneDrive, Dropbox, or a network share makes pip's build step pathologically
slow: it writes thousands of small files and the filesystem amplifies every one. Measured on this
machine, a full install took **91 seconds** on a OneDrive-hosted virtualenv.

The installer bounds this with a timeout (default 600 s) and falls back to installing the
dependencies plus a source redirect, which reaches the same end state. Tune it if you need to:

```powershell
.\install.ps1                                    # uses the default budget
python tools\install_dev.py --pip-timeout 1800   # more patience
```

The genuinely fast fix is to keep the virtualenv off the synchronised folder — it does not need to
be inside the project:

```powershell
python -m venv $env:LOCALAPPDATA\myriaxon-venv
& "$env:LOCALAPPDATA\myriaxon-venv\Scripts\python.exe" -m pip install -e .
```

### `myriaxon.exe` is blocked by an Application Control policy

Some managed Windows machines block newly created executables. This does not affect the
product: use the module entry point, which is always permitted and is the same code path.

```powershell
.\.venv\Scripts\python.exe -m myriaxon
```

### The setup wizard appears even though a config exists

Myriaxon reads `~/.myriaxon` — or `./.myriaxon` next to the code when the home directory is not
writable, which is common on managed machines. Check where it is actually looking:

```bash
python -c "from myriaxon.constants import CONFIG_DIR; print(CONFIG_DIR)"
```

Set `MYRIAXON_CONFIG_DIR` to pin it somewhere you control.

### `ModuleNotFoundError: No module named 'myriaxon'`

You are running a different interpreter from the one Myriaxon was installed into. Either use
the venv's interpreter by full path (`.venv\Scripts\python.exe -m myriaxon`) or activate the
virtualenv first.
