#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
exec "${PYTHON:-python3}" "$ROOT/scripts/ops/toggle_codex_distributed_mode.py" "$@"
