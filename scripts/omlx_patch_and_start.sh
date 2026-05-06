#!/bin/bash
# oMLX Metal safety patches — auto-applied before each server start.
# Survives `brew upgrade omlx` because launchd runs this wrapper.
#
# Patches:
#   1. engine_core.py: mx.async_eval = mx.eval (prevent async Metal race)
#   2. embedding.py:   route embed() through mlx_executor
#   3. reranker.py:    route rerank() through mlx_executor

set -euo pipefail

SITE="/opt/homebrew/opt/omlx/libexec/lib/python3.11/site-packages"
LOG="/opt/homebrew/var/log/omlx_patch.log"
BASE_PATH="${OMLX_BASE_PATH:-/Users/ai/.omlx}"
SETTINGS="${BASE_PATH}/settings.json"
MODEL_DIR="${OMLX_MODEL_DIR:-/Users/ai/.omlx/models-text}"
PAGED_CACHE_DIR="${OMLX_PAGED_CACHE_DIR:-${BASE_PATH}/cache}"
PORT="${OMLX_PORT:-8080}"
DISABLE_CACHE="${OMLX_DISABLE_CACHE:-0}"
MAX_MODEL_MEMORY="${OMLX_MAX_MODEL_MEMORY:-10GB}"
MAX_PROCESS_MEMORY="${OMLX_MAX_PROCESS_MEMORY:-auto}"
MAX_NUM_SEQS="${OMLX_MAX_NUM_SEQS:-1}"
COMPLETION_BATCH_SIZE="${OMLX_COMPLETION_BATCH_SIZE:-1}"
INITIAL_CACHE_BLOCKS="${OMLX_INITIAL_CACHE_BLOCKS:-32}"
HOT_CACHE_MAX_SIZE="${OMLX_HOT_CACHE_MAX_SIZE:-0}"

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "$LOG"; }

# ---------- Patch 1: engine_core.py — mx.async_eval = mx.eval ----------
FILE="$SITE/omlx/engine_core.py"
if [ -f "$FILE" ] && ! grep -q 'mx\.async_eval = mx\.eval' "$FILE"; then
    sed -i '' '/^import mlx\.core as mx$/a\
\
# [MAGI patch] Force synchronous Metal eval to prevent GPU command buffer races.\
mx.async_eval = mx.eval
' "$FILE"
    log "PATCHED engine_core.py (async_eval → eval)"
else
    log "engine_core.py already patched or not found"
fi

# ---------- Patch 2: embedding.py — embed() via mlx_executor ----------
FILE="$SITE/omlx/engine/embedding.py"
if [ -f "$FILE" ] && ! grep -q 'get_mlx_executor' "$FILE"; then
    # Add imports
    sed -i '' 's/^import gc$/import asyncio\nimport gc/' "$FILE"
    sed -i '' '/^from \.\.models\.embedding/i\
from ..engine_core import get_mlx_executor
' "$FILE"
    # Replace the embed body
    python3 -c "
import re, pathlib
p = pathlib.Path('$FILE')
src = p.read_text()
old = '''        if self._model is None:
            raise RuntimeError(\"Engine not started. Call start() first.\")

        return self._model.embed(
            texts=texts,
            max_length=max_length,
            padding=padding,
            truncation=truncation,
        )'''
new = '''        if self._model is None:
            raise RuntimeError(\"Engine not started. Call start() first.\")

        # [MAGI patch] Route through MLX executor to prevent Metal races.
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            get_mlx_executor(),
            lambda: self._model.embed(
                texts=texts,
                max_length=max_length,
                padding=padding,
                truncation=truncation,
            ),
        )'''
if old in src:
    p.write_text(src.replace(old, new))
    print('OK')
else:
    print('SKIP')
"
    log "PATCHED embedding.py (embed → executor)"
else
    log "embedding.py already patched or not found"
fi

# ---------- Patch 3: reranker.py — rerank() via mlx_executor ----------
FILE="$SITE/omlx/engine/reranker.py"
if [ -f "$FILE" ] && ! grep -q 'get_mlx_executor' "$FILE"; then
    sed -i '' 's/^import gc$/import asyncio\nimport gc/' "$FILE"
    sed -i '' '/^from \.\.models\.reranker/i\
from ..engine_core import get_mlx_executor
' "$FILE"
    python3 -c "
import re, pathlib
p = pathlib.Path('$FILE')
src = p.read_text()
old = '''        if self._model is None:
            raise RuntimeError(\"Engine not started. Call start() first.\")

        output = self._model.rerank(
            query=query,
            documents=documents,
            max_length=max_length,
        )'''
new = '''        if self._model is None:
            raise RuntimeError(\"Engine not started. Call start() first.\")

        # [MAGI patch] Route through MLX executor to prevent Metal races.
        loop = asyncio.get_running_loop()
        output = await loop.run_in_executor(
            get_mlx_executor(),
            lambda: self._model.rerank(
                query=query,
                documents=documents,
                max_length=max_length,
            ),
        )'''
if old in src:
    p.write_text(src.replace(old, new))
    print('OK')
else:
    print('SKIP')
"
    log "PATCHED reranker.py (rerank → executor)"
else
    log "reranker.py already patched or not found"
fi

python3 - <<'PY'
import json
import os
from pathlib import Path

base_path = Path(os.environ.get("OMLX_BASE_PATH", "/Users/ai/.omlx"))
settings = base_path / "settings.json"
settings.parent.mkdir(parents=True, exist_ok=True)

try:
    data = json.loads(settings.read_text()) if settings.exists() else {}
except Exception:
    data = {}

data.setdefault("server", {})
data.setdefault("model", {})
data.setdefault("memory", {})
data.setdefault("scheduler", {})
data.setdefault("cache", {})
data.setdefault("sampling", {})

data["server"]["port"] = int(os.environ.get("OMLX_PORT", "8080"))
data["model"]["model_dir"] = os.environ.get("OMLX_MODEL_DIR", "/Users/ai/.omlx/models-text")
data["model"]["model_dirs"] = [data["model"]["model_dir"]]
data["model"]["max_model_memory"] = os.environ.get("OMLX_MAX_MODEL_MEMORY", "10GB")
data["memory"]["max_process_memory"] = os.environ.get("OMLX_MAX_PROCESS_MEMORY", "auto")
data["scheduler"]["max_num_seqs"] = int(os.environ.get("OMLX_MAX_NUM_SEQS", "1"))
data["scheduler"]["completion_batch_size"] = int(os.environ.get("OMLX_COMPLETION_BATCH_SIZE", "1"))
data["cache"]["enabled"] = os.environ.get("OMLX_DISABLE_CACHE", "0") not in {"1", "true", "yes", "on"}
data["cache"]["ssd_cache_dir"] = os.environ.get("OMLX_PAGED_CACHE_DIR", str(base_path / "cache"))
data["cache"]["initial_cache_blocks"] = int(os.environ.get("OMLX_INITIAL_CACHE_BLOCKS", "32"))
data["cache"]["hot_cache_max_size"] = os.environ.get("OMLX_HOT_CACHE_MAX_SIZE", "0") or "0"
data["sampling"]["max_tokens"] = int(os.environ.get("OMLX_MAX_TOKENS", "32768"))
data["sampling"]["max_context_window"] = int(os.environ.get("OMLX_MAX_CONTEXT_WINDOW", "32768"))

settings.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
PY
log "Synchronized settings.json (base=${BASE_PATH}, model_dir=${MODEL_DIR}, model=${MAX_MODEL_MEMORY}, process=${MAX_PROCESS_MEMORY}, cache_blocks=${INITIAL_CACHE_BLOCKS}, hot_cache=${HOT_CACHE_MAX_SIZE}, max_tokens=${OMLX_MAX_TOKENS:-32768}, context=${OMLX_MAX_CONTEXT_WINDOW:-32768}, port=${PORT}, disable_cache=${DISABLE_CACHE})"

log "Patch check complete, starting oMLX server"

mkdir -p "${BASE_PATH}"

OMLX_ARGS=(
    serve
    --base-path "${BASE_PATH}"
    --model-dir "${MODEL_DIR}"
    --max-model-memory "${MAX_MODEL_MEMORY}"
    --max-process-memory "${MAX_PROCESS_MEMORY}"
    --port "${PORT}"
    --max-concurrent-requests "${MAX_NUM_SEQS}"
)

if [ "${DISABLE_CACHE}" = "1" ]; then
    OMLX_ARGS+=(--no-cache)
else
    mkdir -p "${PAGED_CACHE_DIR}"
    OMLX_ARGS+=(--paged-ssd-cache-dir "${PAGED_CACHE_DIR}" --initial-cache-blocks "${INITIAL_CACHE_BLOCKS}")
    if [ "${HOT_CACHE_MAX_SIZE}" != "0" ]; then
        OMLX_ARGS+=(--hot-cache-max-size "${HOT_CACHE_MAX_SIZE}")
    fi
fi

exec /opt/homebrew/opt/omlx/bin/omlx "${OMLX_ARGS[@]}"
