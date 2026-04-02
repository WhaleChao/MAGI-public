#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
exec "${PYTHON:-python3}" "$ROOT/scripts/ops/check_openclaw_runtime_mode.py" "$@"
