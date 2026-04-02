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

log "Patch check complete, starting oMLX server"

# Start oMLX with all safety flags
exec /opt/homebrew/opt/omlx/bin/omlx serve \
    --model-dir /Users/ai/.omlx/models \
    --paged-ssd-cache-dir /Users/ai/.omlx/cache \
    --max-process-memory 85 \
    --port 8080 \
    --max-num-seqs 1 \
    --completion-batch-size 1
