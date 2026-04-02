#!/bin/bash
# 司法院 API 夜間拉取守護：每天 00:00-06:00 自動拉取
export PATH="/opt/homebrew/bin:$PATH"
cd /Users/ai/Desktop/MAGI

while true; do
    HOUR=$(date +%H)
    if [ "$HOUR" -ge 0 ] && [ "$HOUR" -lt 6 ]; then
        echo "[$(date)] Starting judicial API night pull..."
        python3 -u -c "
import sys; sys.path.insert(0,'skills/judgment-collector')
import action as jc
r = jc.official_api_night_pull(max_jdocs=25000, max_days=0, force=False, notify=False)
import json; print(json.dumps({k:v for k,v in r.items() if k != 'detail'}, ensure_ascii=False, default=str))
" 2>&1
        echo "[$(date)] Night pull done. Running ingest..."
        python3 -u scripts/ingest_raw_judgments.py 2>&1
        echo "[$(date)] Ingest done. Sleeping until tomorrow..."
        # 拉完後睡到明天 00:00
        sleep 21600
    else
        # 不在服務時段，每 10 分鐘檢查一次
        sleep 600
    fi
done
