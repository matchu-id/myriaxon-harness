#!/usr/bin/env bash
# Wrapper: run the adapter import check with the WSL-side Harbor environment.
set -u
REPO=/mnt/c/Users/arian/OneDrive/ドキュメント/Myriaxon
cd "$REPO" || exit 2
exec "$REPO/.venv-harbor-linux/bin/python" "$REPO/tools/check_adapter_import.py"
