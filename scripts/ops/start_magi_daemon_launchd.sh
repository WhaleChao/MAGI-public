#!/bin/zsh
set -euo pipefail

MAGI_ROOT="/Users/ai/Desktop/MAGI_v2"
PYTHON="$MAGI_ROOT/venv/bin/python3"

cd "$MAGI_ROOT"

export HOME="/Users/ai"
export LANG="${LANG:-en_US.UTF-8}"
export MAGI_ROOT
export PYTHONPATH="$MAGI_ROOT${PYTHONPATH:+:$PYTHONPATH}"
export PATH="$MAGI_ROOT/venv/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

# Keep the MySQL connector on pure Python so MAGI does not load a second
# OpenSSL/libcrypto stack inside daemon child processes.
export MAGI_MYSQL_USE_PURE="${MAGI_MYSQL_USE_PURE:-1}"
export MYSQL_USE_PURE="${MYSQL_USE_PURE:-1}"

exec "$PYTHON" "$MAGI_ROOT/daemon.py"
