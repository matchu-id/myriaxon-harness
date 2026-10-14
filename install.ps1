# Myriaxon installer for Windows.
#
#   .\install.ps1                 install into a private virtualenv at .\.venv
#   .\install.ps1 -Extras all     also install every provider SDK (openai, gemini, mcp, keyring)
#   .\install.ps1 -Verify         run the offline test suite after installing
#   .\install.ps1 -Python python3.12   use a specific interpreter to build the venv
#
# If script execution is disabled on this machine, run it without changing any system policy:
#
#   powershell -ExecutionPolicy Bypass -File .\install.ps1
#
# Everything is installed inside this folder. Nothing is written to Program Files, to the
# registry, or to a system Python.

[CmdletBinding()]
param(
    [string]$Extras = "",
    [string]$Python = "",
    [switch]$Verify,
    [switch]$NoVenv,
    [switch]$Clean
)

$ErrorActionPreference = "Stop"
$Repo = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Repo

function Write-Step($text) { Write-Host "`n==> $text" -ForegroundColor Cyan }
function Write-Ok($text)   { Write-Host "    $text" -ForegroundColor Green }
function Write-Warn($text) { Write-Host "    $text" -ForegroundColor Yellow }
function Write-Err($text)  { Write-Host "    $text" -ForegroundColor Red }

# A non-ASCII path (for example a Japanese Documents folder) is supported, but the console
# encoding has to be told so: pip and setuptools otherwise crash with UnicodeEncodeError while
# writing paths they cannot represent. This is the single most common Windows install failure
# for this project, so it is set before anything else runs.
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$env:PIP_DISABLE_PIP_VERSION_CHECK = "1"
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

Write-Host ""
Write-Host "  MYRIAXON" -ForegroundColor White
Write-Host "  Ten thousand paths. One mind." -ForegroundColor DarkGray
Write-Host "  An advanced agentic harness." -ForegroundColor DarkGray

# --- locate an interpreter -----------------------------------------------------------------
Write-Step "Locating Python"
$basePython = $null
if ($Python) {
    $basePython = $Python
} else {
    foreach ($candidate in @("python", "python3", "py")) {
        $cmd = Get-Command $candidate -ErrorAction SilentlyContinue
        if ($cmd) { $basePython = $cmd.Source; break }
    }
}
if (-not $basePython) {
    Write-Err "No Python found on PATH."
    Write-Warn "Install Python 3.10 or newer from https://www.python.org/downloads/windows/"
    Write-Warn "and tick 'Add python.exe to PATH' in the installer."
    exit 1
}

$versionText = & $basePython -c "import sys; print('%d.%d.%d' % sys.version_info[:3])" 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Err "Could not run $basePython"
    exit 1
}
$major, $minor = ($versionText -split '\.')[0..1] | ForEach-Object { [int]$_ }
Write-Ok "$basePython  (Python $versionText)"
if ($major -lt 3 -or ($major -eq 3 -and $minor -lt 10)) {
    Write-Err "Python 3.10 or newer is required; found $versionText."
    exit 1
}

# --- virtualenv ----------------------------------------------------------------------------
$venvDir = Join-Path $Repo ".venv"
$venvPython = Join-Path $venvDir "Scripts\python.exe"

if ($NoVenv) {
    Write-Step "Using the system interpreter (no virtualenv requested)"
    $targetPython = $basePython
} else {
    if ($Clean -and (Test-Path $venvDir)) {
        Write-Step "Removing the existing virtualenv (clean install)"
        Remove-Item -Recurse -Force $venvDir
    }

    $venvHealthy = $false
    if (Test-Path $venvPython) {
        # A copied or relocated virtualenv keeps a pointer to the interpreter that built it. If
        # that interpreter is gone the venv is dead but still looks present, which produced the
        # most confusing failure this project had: "No Python at '...python.exe'". Test it.
        & $venvPython -c "import sys" 2>$null
        $venvHealthy = ($LASTEXITCODE -eq 0)
        if (-not $venvHealthy) {
            Write-Warn "The existing .venv is broken (its base interpreter is missing). Rebuilding."
            Remove-Item -Recurse -Force $venvDir -ErrorAction SilentlyContinue
        }
    }

    if (-not (Test-Path $venvPython)) {
        Write-Step "Creating a virtualenv at .venv"
        & $basePython -m venv $venvDir
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path $venvPython)) {
            Write-Err "Could not create the virtualenv."
            exit 1
        }
        Write-Ok "created"
    } else {
        Write-Step "Reusing the virtualenv at .venv"
    }
    $targetPython = $venvPython
}

# --- install --------------------------------------------------------------------------------
Write-Step "Installing Myriaxon"
& $targetPython -m pip install --upgrade --quiet "setuptools>=68" wheel
if ($LASTEXITCODE -ne 0) { Write-Warn "Could not upgrade setuptools; continuing with what is present." }

& $targetPython tools\install_dev.py --extras $Extras
$installCode = $LASTEXITCODE
if ($installCode -ne 0) {
    Write-Err "Installation failed. The output above says why."
    exit $installCode
}

# --- verify ---------------------------------------------------------------------------------
Write-Step "Verifying"
$version = & $targetPython -m myriaxon --version 2>&1
Write-Ok $version

& $targetPython -c "import myriaxon.cli, myriaxon.dashboard.server, myriaxon.bench; print('imports OK')"
if ($LASTEXITCODE -ne 0) {
    Write-Err "The package does not import. See the error above."
    exit 1
}
Write-Ok "imports OK"

if ($Verify) {
    Write-Step "Running the offline test suite"
    & $targetPython -m pytest tests\ -o addopts= -q
    if ($LASTEXITCODE -ne 0) {
        Write-Err "Tests failed."
        exit 1
    }
    Write-Ok "tests passed"
}

# --- done -----------------------------------------------------------------------------------
Write-Host ""
Write-Host "  Installed." -ForegroundColor Green
Write-Host ""
Write-Host "  Terminal agent     .\.venv\Scripts\python.exe -m myriaxon"
Write-Host "  Web dashboard      .\.venv\Scripts\python.exe -m myriaxon web"
Write-Host "  Configure          .\.venv\Scripts\python.exe -m myriaxon auth"
Write-Host ""
Write-Host "  Or use the launchers:" -ForegroundColor DarkGray
Write-Host "    myriaxon.cmd          terminal agent"
Write-Host "    myriaxon-web.cmd      web dashboard"
Write-Host ""
Write-Warn "If `myriaxon.exe` is blocked by an Application Control policy on this machine,"
Write-Warn "use `python -m myriaxon` instead -- it is always allowed and does the same thing."
Write-Host ""
