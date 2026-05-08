#!/bin/bash
# ═══════════════════════════════════════════════════════════════
# fix_all_services.sh — MAGI 一鍵修復所有服務
# ═══════════════════════════════════════════════════════════════
# 在 MAGI 根目錄下執行：
#   bash scripts/fix_all_services.sh
# ═══════════════════════════════════════════════════════════════

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MAGI_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${MAGI_ROOT}"

# ── 環境變數修復 (MAC) ──
export PATH="/opt/homebrew/bin:/usr/local/bin:${PATH}"

echo "═══════════════════════════════════════"
echo "  MAGI 服務修復工具"
echo "═══════════════════════════════════════"
echo ""

# ── 1. MariaDB ──
echo "🔍 [1/5] 檢查 MariaDB..."
if brew services list 2>/dev/null | grep -q 'mariadb.*started'; then
    echo "✅ MariaDB 已在運行"
else
    echo "⚠️ MariaDB 未運行，正在啟動..."
    brew services start mariadb 2>/dev/null || true
    sleep 3
    if brew services list 2>/dev/null | grep -q 'mariadb.*started'; then
        echo "✅ MariaDB 已啟動"
    else
        echo "❌ MariaDB 啟動失敗，請手動檢查"
    fi
fi

# ── 2. oMLX Inference ──
echo ""
echo "🔍 [2/5] 檢查 oMLX 推理服務..."
if curl -sf http://127.0.0.1:8080/v1/models >/dev/null 2>&1; then
    echo "✅ oMLX 推理服務正常 (port 8080)"
else
    echo "⚠️ oMLX 未回應，嘗試 kickstart..."
    launchctl kickstart -k "gui/$(id -u)/com.magi.omlx" 2>/dev/null || true
    echo "   等待 15 秒讓模型載入..."
    sleep 15
    if curl -sf http://127.0.0.1:8080/v1/models >/dev/null 2>&1; then
        echo "✅ oMLX 已恢復"
    else
        echo "❌ oMLX 仍未回應"
    fi
fi

# ── 3. oMLX Embeddings ──
echo ""
echo "🔍 [3/5] 檢查 oMLX Embeddings 服務..."
if curl -sf http://127.0.0.1:8081/v1/models >/dev/null 2>&1; then
    echo "✅ Embeddings 服務正常 (port 8081)"
else
    echo "⚠️ Embeddings 未回應，嘗試 kickstart..."
    launchctl kickstart -k "gui/$(id -u)/com.magi.omlx-embed" 2>/dev/null || true
    sleep 8
    if curl -sf http://127.0.0.1:8081/v1/models >/dev/null 2>&1; then
        echo "✅ Embeddings 已恢復"
    else
        echo "⚠️ kickstart 失敗，安裝 Embeddings LaunchAgent..."
        python3 "${MAGI_ROOT}/scripts/install_omlx_embed.py" 2>/dev/null
        echo "   等待 15 秒讓模型載入..."
        sleep 15
        if curl -sf http://127.0.0.1:8081/v1/models >/dev/null 2>&1; then
            echo "✅ Embeddings 已恢復（新安裝 LaunchAgent）"
        else
            echo "❌ Embeddings 仍未回應（語意搜尋功能將降級）"
        fi
    fi
fi

# ── 4. oMLX Watchdog / Restore ──
echo ""
echo "🔍 [4/5] 檢查 oMLX Watchdog / Restore..."
if launchctl print "gui/$(id -u)/com.magi.omlx-watchdog" &>/dev/null; then
    echo "✅ Watchdog 正在監控"
else
    echo "⚠️ Watchdog 未運行，重新安裝..."
    python3 "${MAGI_ROOT}/scripts/install_omlx_watchdog.py" 2>/dev/null
    sleep 2
    if launchctl print "gui/$(id -u)/com.magi.omlx-watchdog" &>/dev/null; then
        echo "✅ Watchdog 已啟動"
    else
        echo "❌ Watchdog 安裝失敗"
    fi
fi
if launchctl print "gui/$(id -u)/com.magi.omlx-restore" &>/dev/null; then
    echo "✅ Restore LaunchAgent 已安裝"
else
    echo "⚠️ Restore LaunchAgent 未安裝，重新安裝..."
    python3 "${MAGI_ROOT}/scripts/install_omlx_restore.py" 2>/dev/null
    if launchctl print "gui/$(id -u)/com.magi.omlx-restore" &>/dev/null; then
        echo "✅ Restore LaunchAgent 已啟動"
    else
        echo "❌ Restore LaunchAgent 安裝失敗"
    fi
fi

# ── 5. MAGI Daemon ──
echo ""
echo "🔍 [5/5] 檢查 MAGI Daemon..."
if curl -sf http://127.0.0.1:5002/health >/dev/null 2>&1; then
    echo "✅ MAGI Daemon 正常 (port 5002)"
elif curl -sf http://127.0.0.1:5003/sages >/dev/null 2>&1; then
    echo "✅ Tools API 正常 (port 5003)，主服務可能需要重啟"
    echo "   執行 ./start_magi.sh 重啟主服務"
else
    echo "⚠️ MAGI 服務未回應"
    echo "   執行 ./start_magi.sh 啟動服務"
fi

# ── 總結 ──
echo ""
echo "═══════════════════════════════════════"
echo "  修復完成！狀態摘要："
echo "═══════════════════════════════════════"
echo ""

check() {
    local name=$1 url=$2
    if curl -sf "$url" >/dev/null 2>&1; then
        echo "  ✅ $name"
    else
        echo "  ❌ $name"
    fi
}

check "oMLX 推理 (8080)" "http://127.0.0.1:8080/v1/models"
check "oMLX Embeddings (8081)" "http://127.0.0.1:8081/v1/models"
check "MAGI Server (5002)" "http://127.0.0.1:5002/health"
check "Tools API (5003)" "http://127.0.0.1:5003/sages"

# MariaDB check (ping or process)
if mysqladmin ping -h 127.0.0.1 --silent 2>/dev/null || pgrep -x mariadbd >/dev/null; then
    echo "  ✅ MariaDB"
else
    echo "  ❌ MariaDB"
fi

# Watchdog check
if launchctl print "gui/$(id -u)/com.magi.omlx-watchdog" &>/dev/null; then
    echo "  ✅ oMLX Watchdog"
else
    echo "  ❌ oMLX Watchdog"
fi

echo ""
