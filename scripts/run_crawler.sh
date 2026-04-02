#!/bin/bash
# MAGI 法律資料爬蟲 - 每日 02:30 執行
# 包含: 法規、函釋、判決書、新聞、判決收集+摘要

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_FILE="$SCRIPT_DIR/crawler_$(date +%Y%m%d).log"

echo "========================================" >> "$LOG_FILE"
echo "🕷️ MAGI 爬蟲啟動 - $(date '+%Y-%m-%d %H:%M:%S')" >> "$LOG_FILE"
echo "========================================" >> "$LOG_FILE"

# 確保 Python 環境
source /Users/ai/Desktop/MAGI/venv/bin/activate 2>/dev/null || true

# 透過 wrapper 執行（含反爬保護 + 實務見解更新 + 判決收集）
cd /Users/ai/Desktop/MAGI/skills/law_firm
python3 legal_crawler_wrapper.py --task run_sync >> "$LOG_FILE" 2>&1

echo "" >> "$LOG_FILE"
echo "✓ 爬蟲執行完成 - $(date '+%Y-%m-%d %H:%M:%S')" >> "$LOG_FILE"
echo "" >> "$LOG_FILE"

# 保留最近 30 天的 log
find "$SCRIPT_DIR" -name "crawler_*.log" -mtime +30 -delete 2>/dev/null
