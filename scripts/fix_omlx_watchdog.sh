#!/bin/bash
# ═══════════════════════════════════════════════════════════════
# fix_omlx_watchdog.sh — 修復 oMLX Watchdog LaunchAgent
# ═══════════════════════════════════════════════════════════════
# 問題：LaunchAgent 指向 Desktop 路徑，macOS 安全策略阻擋執行
# 修法：重新安裝 watchdog 到 ~/Library/Application Support/MAGI/bin/
#
# 在 MAGI 根目錄下執行：
#   bash scripts/fix_omlx_watchdog.sh
# ═══════════════════════════════════════════════════════════════

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MAGI_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

echo "🔧 修復 oMLX Watchdog / Profile Restore..."
echo "   MAGI_ROOT: ${MAGI_ROOT}"

# Step 1: 執行 installer
echo "📦 重新安裝 watchdog LaunchAgent..."
python3 "${MAGI_ROOT}/scripts/install_omlx_watchdog.py"
echo "📦 重新安裝 restore LaunchAgent..."
python3 "${MAGI_ROOT}/scripts/install_omlx_restore.py"

# Step 2: 驗證
PLIST="$HOME/Library/LaunchAgents/com.magi.omlx-watchdog.plist"
RESTORE_PLIST="$HOME/Library/LaunchAgents/com.magi.omlx-restore.plist"
RUNTIME_SCRIPT="$HOME/Library/Application Support/MAGI/bin/omlx_watchdog.sh"

if [ -f "${PLIST}" ]; then
    echo "✅ LaunchAgent plist 已安裝：${PLIST}"
else
    echo "❌ LaunchAgent plist 未找到"
    exit 1
fi

if [ -f "${RESTORE_PLIST}" ]; then
    echo "✅ Restore LaunchAgent 已安裝：${RESTORE_PLIST}"
else
    echo "❌ Restore LaunchAgent 未找到"
    exit 1
fi

if [ -x "${RUNTIME_SCRIPT}" ]; then
    echo "✅ Watchdog 腳本已就位：${RUNTIME_SCRIPT}"
else
    echo "❌ Watchdog 腳本不存在或缺少執行權限"
    exit 1
fi

# Step 3: 確認 watchdog 在跑
sleep 3
if launchctl print "gui/$(id -u)/com.magi.omlx-watchdog" &>/dev/null; then
    echo "✅ oMLX Watchdog 已啟動"
else
    echo "⚠️ Watchdog 尚未啟動，嘗試手動 kickstart..."
    launchctl kickstart -k "gui/$(id -u)/com.magi.omlx-watchdog" 2>/dev/null || true
    sleep 2
    if launchctl print "gui/$(id -u)/com.magi.omlx-watchdog" &>/dev/null; then
        echo "✅ oMLX Watchdog 已啟動（手動 kickstart）"
    else
        echo "❌ Watchdog 啟動失敗，請檢查日誌"
    fi
fi

# Step 4: 驗證 oMLX inference 本身
echo ""
echo "🔍 檢查 oMLX 推理服務..."
if curl -sf http://127.0.0.1:8080/v1/models >/dev/null 2>&1; then
    echo "✅ oMLX 推理服務正常"
else
    echo "⚠️ oMLX 推理服務未回應，嘗試 kickstart..."
    launchctl kickstart -k "gui/$(id -u)/com.magi.omlx" 2>/dev/null || true
    echo "   等待 15 秒讓模型載入..."
    sleep 15
    if curl -sf http://127.0.0.1:8080/v1/models >/dev/null 2>&1; then
        echo "✅ oMLX 推理服務已恢復"
    else
        echo "❌ oMLX 推理服務仍未回應，可能需要手動排查"
    fi
fi

echo ""
echo "═══════════════════════════════════════"
echo "修復完成！變更摘要："
echo "  1. Watchdog 腳本複製到受信任路徑"
echo "  2. LaunchAgent plist 已更新"
echo "  3. 已嘗試重啟 watchdog / restore 和 oMLX"
echo "═══════════════════════════════════════"
