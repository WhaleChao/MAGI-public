#!/bin/bash
# DB Sync 守護：自動重啟，永不停止
export PATH="/opt/homebrew/bin:$PATH"
MAGI_DIR="${MAGI_ROOT_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
cd "$MAGI_DIR"

while true; do
    echo "[$(date)] Starting db_sync_to_remote.py..." >> db_sync.log
    python3 -u scripts/db_sync_to_remote.py >> db_sync.log 2>&1
    echo "[$(date)] Process exited, restarting in 30s..." >> db_sync.log
    sleep 30
done
