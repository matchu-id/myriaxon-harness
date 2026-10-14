#!/usr/bin/env bash
# Myriaxon installer for Linux and macOS.
#
#   ./install.sh                 install into a private virtualenv at ./.venv
#   ./install.sh --extras all    also install every provider SDK (openai, gemini, mcp, keyring)
#   ./install.sh --verify        run the offline test suite after installing
#   ./install.sh --no-venv       install into the current interpreter instead
#   ./install.sh --clean         discard any existing .venv first
#
# Everything is installed inside this folder. Nothing is written outside it except the Python
# packages themselves, and only ever into the virtualenv this script creates.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO"

EXTRAS=""
VERIFY=0
USE_VENV=1
CLEAN=0

while [ $# -gt 0 ]; do
  case "$1" in
    --extras)   EXTRAS="${2:-}"; shift 2 ;;
    --extras=*) EXTRAS="${1#*=}"; shift ;;
    --verify)   VERIFY=1; shift ;;
    --no-venv)  USE_VENV=0; shift ;;
    --clean)    CLEAN=1; shift ;;
    -h|--help)
      sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done

# Colour only when attached to a terminal, so piping the output to a log stays readable.
if [ -t 1 ]; then
  C_CYAN=$'\033[36m'; C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'; C_RED=$'\033[31m'
  C_DIM=$'\033[2m'; C_OFF=$'\033[0m'
else
  C_CYAN=""; C_GREEN=""; C_YELLOW=""; C_RED=""; C_DIM=""; C_OFF=""
fi
step() { printf '\n%s==> %s%s\n' "$C_CYAN" "$1" "$C_OFF"; }
ok()   { printf '    %s%s%s\n' "$C_GREEN" "$1" "$C_OFF"; }
warn() { printf '    %s%s%s\n' "$C_YELLOW" "$1" "$C_OFF"; }
err()  { printf '    %s%s%s\n' "$C_RED" "$1" "$C_OFF"; }

# Non-ASCII paths are supported, but the toolchain has to be told to use UTF-8 or pip and
# setuptools fail while writing paths they cannot encode.
export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8
export PIP_DISABLE_PIP_VERSION_CHECK=1

printf '\n  MYRIAXON\n'
printf '%s  Ten thousand paths. One mind.%s\n' "$C_DIM" "$C_OFF"
printf '%s  An advanced agentic harness.%s\n' "$C_DIM" "$C_OFF"

# --- locate an interpreter ------------------------------------------------------------------
step "Locating Python"
BASE_PYTHON=""
for candidate in python3 python; do
  if command -v "$candidate" >/dev/null 2>&1; then BASE_PYTHON="$candidate"; break; fi
done
if [ -z "$BASE_PYTHON" ]; then
  err "No python3 found on PATH."
  warn "Install Python 3.10 or newer, then run this script again."
  exit 1
fi

VERSION="$("$BASE_PYTHON" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])')"
"$BASE_PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' || {
  err "Python 3.10 or newer is required; found $VERSION."
  exit 1
}
ok "$(command -v "$BASE_PYTHON")  (Python $VERSION)"

# --- virtualenv -----------------------------------------------------------------------------
if [ "$USE_VENV" -eq 1 ]; then
  VENV_DIR="$REPO/.venv"
  if [ "$CLEAN" -eq 1 ] && [ -d "$VENV_DIR" ]; then
    step "Removing the existing virtualenv (clean install)"
    rm -rf "$VENV_DIR"
  fi

  if [ -x "$VENV_DIR/bin/python" ]; then
    # A moved or copied virtualenv points at the interpreter that built it. If that is gone the
    # venv is dead while still looking present, so test it rather than trusting the path.
    if ! "$VENV_DIR/bin/python" -c 'import sys' >/dev/null 2>&1; then
      warn "The existing .venv is broken (its base interpreter is missing). Rebuilding."
      rm -rf "$VENV_DIR"
    fi
  fi

  if [ ! -x "$VENV_DIR/bin/python" ]; then
    step "Creating a virtualenv at .venv"
    "$BASE_PYTHON" -m venv "$VENV_DIR"
    ok "created"
  else
    step "Reusing the virtualenv at .venv"
  fi
  TARGET_PYTHON="$VENV_DIR/bin/python"
else
  step "Using the current interpreter (no virtualenv requested)"
  TARGET_PYTHON="$BASE_PYTHON"
fi

# --- install --------------------------------------------------------------------------------
step "Installing Myriaxon"
"$TARGET_PYTHON" -m pip install --upgrade --quiet 'setuptools>=68' wheel \
  || warn "Could not upgrade setuptools; continuing with what is present."

if ! "$TARGET_PYTHON" tools/install_dev.py --extras "$EXTRAS"; then
  err "Installation failed. The output above says why."
  exit 1
fi

# --- verify ---------------------------------------------------------------------------------
step "Verifying"
ok "$("$TARGET_PYTHON" -m myriaxon --version 2>&1)"
if ! "$TARGET_PYTHON" -c 'import myriaxon.cli, myriaxon.dashboard.server, myriaxon.bench; print("imports OK")'; then
  err "The package does not import. See the error above."
  exit 1
fi

if [ "$VERIFY" -eq 1 ]; then
  step "Running the offline test suite"
  if ! "$TARGET_PYTHON" -m pytest tests/ -o addopts= -q; then
    err "Tests failed."
    exit 1
  fi
  ok "tests passed"
fi

# --- done -----------------------------------------------------------------------------------
printf '\n  %sInstalled.%s\n\n' "$C_GREEN" "$C_OFF"
printf '  Terminal agent     %s -m myriaxon\n' "$TARGET_PYTHON"
printf '  Web dashboard      %s -m myriaxon web\n' "$TARGET_PYTHON"
printf '  Configure          %s -m myriaxon auth\n\n' "$TARGET_PYTHON"
printf '%s  The virtualenv also provides a `myriaxon` command:\n' "$C_DIM"
printf '    ./.venv/bin/myriaxon            ./.venv/bin/myriaxon web%s\n\n' "$C_OFF"
