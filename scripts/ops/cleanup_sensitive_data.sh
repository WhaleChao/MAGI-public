#!/bin/bash
# ============================================================
# MAGI P0-03: 敏感資料清理腳本
# ============================================================
# 用途：移除 repo 中不應存在於 release tree 的敏感資料
# 使用前請先備份，確認後再執行。
#
# Usage:
#   bash scripts/ops/cleanup_sensitive_data.sh --dry-run   # 預覽要刪除的內容
#   bash scripts/ops/cleanup_sensitive_data.sh --execute    # 實際執行刪除
# ============================================================

set -euo pipefail

MAGI_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$MAGI_ROOT"

DRY_RUN=true
if [[ "${1:-}" == "--execute" ]]; then
    DRY_RUN=false
    echo "⚠️  執行模式：將實際刪除檔案"
elif [[ "${1:-}" == "--dry-run" ]]; then
    echo "🔍 預覽模式：僅列出要刪除的檔案"
else
    echo "Usage: $0 [--dry-run | --execute]"
    exit 1
fi

echo ""
echo "=== MAGI Sensitive Data Cleanup ==="
echo "Root: $MAGI_ROOT"
echo ""

TOTAL=0

remove_item() {
    local path="$1"
    local reason="$2"
    if [[ -e "$path" ]]; then
        TOTAL=$((TOTAL + 1))
        if $DRY_RUN; then
            echo "  [WOULD DELETE] $path  ($reason)"
        else
            echo "  [DELETING] $path  ($reason)"
            rm -rf "$path"
        fi
    fi
}

echo "--- 1. Browser Profiles ---"
remove_item "casper_ecosystem/law_firm_orchestrators/.laf_chrome_profile" "瀏覽器 profile (含 cookies/session)"
find . -maxdepth 4 -name ".laf_chrome_profile" -not -path "./.git/*" -not -path "./backups/*" 2>/dev/null | while read f; do
    remove_item "$f" "瀏覽器 profile"
done

echo ""
echo "--- 2. Sensitive Training Data ---"
remove_item "skills/pdf-namer/training_data.json" "含真實案件個資的訓練資料"

echo ""
echo "--- 3. Database Backups ---"
remove_item "_db_backups" "生產環境 DB 備份"

echo ""
echo "--- 4. Apply Form Captures ---"
find . -maxdepth 1 -name "apply_form_*.png" -o -name "apply_form_*.html" -o -name "apply_form_inside_*.html" 2>/dev/null | while read f; do
    remove_item "$f" "司法表單截圖"
done

echo ""
echo "--- 5. Debug Screenshots ---"
find . -maxdepth 1 -name "debug_*.png" -o -name "debug_*.html" 2>/dev/null | while read f; do
    remove_item "$f" "Debug 截圖"
done

echo ""
echo "--- 6. Runtime Outputs ---"
remove_item "_autopilot_runs" "Autopilot 執行記錄"
remove_item "_debug_reports" "Debug 報告"
remove_item "_eventlog.jsonl" "事件日誌"
remove_item "_judicial_smoke" "司法 API 煙霧測試"
remove_item "_crawl_targets.json" "爬蟲目標"
remove_item "_statutes_vdb_state.json" "法條 VDB 狀態"
remove_item "_autopilot_state.json" "Autopilot 狀態"
remove_item "_autopilot.lock" "Autopilot 鎖定"
remove_item "active_tasks.json" "活躍任務"

echo ""
echo "--- 7. Formal Capture Profiles ---"
find casper_ecosystem/law_firm_orchestrators/ -maxdepth 1 -name "_laf_formal_capture_profile_*" -type d 2>/dev/null | while read f; do
    remove_item "$f" "LAF 正式擷取 profile"
done
find casper_ecosystem/law_firm_orchestrators/ -maxdepth 1 -name "laf_guided_capture_*" -type d 2>/dev/null | while read f; do
    remove_item "$f" "LAF 引導擷取資料"
done

echo ""
echo "==================================="
if $DRY_RUN; then
    echo "🔍 預覽完成。共 $TOTAL 個項目待清理。"
    echo "   確認後執行: $0 --execute"
else
    echo "✅ 清理完成。已刪除 $TOTAL 個項目。"
    echo "   請確認 git status 並 commit 變更。"
fi
