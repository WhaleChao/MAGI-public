#!/bin/bash
# Resummary 守護：自動重啟，永不停止
export PATH="/opt/homebrew/bin:$PATH"
MAGI_DIR="${MAGI_ROOT_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
cd "$MAGI_DIR"

while true; do
    echo "[$(date)] Starting resummary_batch.py..." >> resummary_progress.log
    python3 -u scripts/resummary_batch.py >> resummary_progress.log 2>&1
    EXIT_CODE=$?
    echo "[$(date)] Process exited (code=$EXIT_CODE), restarting in 30s..." >> resummary_progress.log
    sleep 30
done
