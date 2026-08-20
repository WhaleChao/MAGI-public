#!/bin/zsh
set -eu
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"
if ! command -v python3 >/dev/null 2>&1; then
  echo "找不到 Python 3.12 以上版本。請先安裝 Python。"
  exit 2
fi
exec python3 scripts/magi_selfhost.py --target macos --source "$ROOT" install "$@"
