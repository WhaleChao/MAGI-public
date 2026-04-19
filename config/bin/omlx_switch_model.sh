#!/bin/bash
# oMLX 模型切換腳本 — 日間（三模型）/ 離峰（26B 單模型）
# 建立時間：2026-04-14
# 2026-04-19 強化：flock 互斥鎖 / 記憶體下修 / port-closed wait / preflight / heartbeat
set -euo pipefail

MODE="${1:-day}"

# ---- auto 模式：依當前時間自動選 day / night（在 lock 之前解析）----
# day 窗口：07:00-21:59（對齊 job_omlx_switch_day=07:00 / job_omlx_switch_night=21:50）
# 重要：auto 模式有冪等檢查 — 若 models-text 已對應正確模型則跳過切換（避免重開機 90s 後不必要 bootout）
if [ "$MODE" = "auto" ]; then
    current_hour=$(date +%H | sed 's/^0*//' | awk '{if($0=="") print 0; else print $0+0}')
    if [ "$current_hour" -ge 7 ] && [ "$current_hour" -lt 22 ]; then
        MODE="day"
        EXPECTED_MODEL_KEYWORD="e4b"
    else
        MODE="night"
        EXPECTED_MODEL_KEYWORD="26b"
    fi
    printf '%s [switch] auto → %s (hour=%02d)\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$MODE" "$current_hour" | tee -a "/opt/homebrew/var/log/omlx_switch.log"
    # 冪等檢查：若 models-text 已含正確模型且 oMLX 已在線，跳過切換
    current_model_in_dir=$(ls "/Users/ai/.omlx/models-text/" 2>/dev/null | tr '[:upper:]' '[:lower:]' | head -1)
    omlx_online=$(curl -sf --max-time 3 http://127.0.0.1:8080/v1/models >/dev/null 2>&1 && echo "yes" || echo "no")
    if echo "$current_model_in_dir" | grep -qi "$EXPECTED_MODEL_KEYWORD" && [ "$omlx_online" = "yes" ]; then
        printf '%s [switch] auto: 已是 %s 模式且 oMLX 在線，跳過切換\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$MODE" | tee -a "/opt/homebrew/var/log/omlx_switch.log"
        exit 0
    fi
    printf '%s [switch] auto: 需切換（current_model=%s, online=%s）\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$current_model_in_dir" "$omlx_online" | tee -a "/opt/homebrew/var/log/omlx_switch.log"
fi

PROFILE_FILE="/Users/ai/.omlx/active_profile"
MODELS_TEXT_DIR="/Users/ai/.omlx/models-text"
E4B_SRC="/Users/ai/.omlx/models/gemma-4-e4b-it-4bit"
B26_SRC="/Users/ai/.omlx/models/gemma-4-26b-a4b-it-UD-4bit"
UID_NUM=$(id -u)
LOG="/opt/homebrew/var/log/omlx_switch.log"
LOCKDIR="/tmp/omlx_switch.lock.d"
LOCK_STALE_SEC=600   # 超過 10 分鐘視為 stale（night 切換含 sleep 120+heartbeat 60，正常 3-5 分鐘內完成）
ADMIN_NOTIFY_FILE="/tmp/omlx_switch_alert.txt"

log() { printf '%s [switch] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" | tee -a "$LOG"; }

# ---- A1: mkdir 原子互斥鎖（macOS 無 flock CLI）----
# mkdir 對已存在目錄會失敗，作為原子性互斥；PID 檔記錄持有者以便 stale 清理
acquire_lock() {
    if mkdir "$LOCKDIR" 2>/dev/null; then
        echo $$ > "$LOCKDIR/pid"
        date +%s > "$LOCKDIR/ts"
        # 註冊退出時自動釋放
        trap 'release_lock' EXIT INT TERM
        return 0
    fi
    # 已有鎖 — 檢查是否 stale
    local holder_pid holder_ts now age
    holder_pid=$(cat "$LOCKDIR/pid" 2>/dev/null || echo "")
    holder_ts=$(cat "$LOCKDIR/ts" 2>/dev/null || echo "0")
    now=$(date +%s)
    age=$(( now - holder_ts ))
    if [ -n "$holder_pid" ] && kill -0 "$holder_pid" 2>/dev/null && [ "$age" -lt "$LOCK_STALE_SEC" ]; then
        # 持有者還活著且未超時 → 正常互斥，跳過
        return 1
    fi
    # stale：清掉重搶
    log "⚠️  偵測到 stale lock（pid=$holder_pid, age=${age}s），清理後重試"
    rm -rf "$LOCKDIR"
    if mkdir "$LOCKDIR" 2>/dev/null; then
        echo $$ > "$LOCKDIR/pid"
        date +%s > "$LOCKDIR/ts"
        trap 'release_lock' EXIT INT TERM
        return 0
    fi
    return 1
}

release_lock() {
    # 只有自己是持有者才清理
    local holder_pid
    holder_pid=$(cat "$LOCKDIR/pid" 2>/dev/null || echo "")
    if [ "$holder_pid" = "$$" ]; then
        rm -rf "$LOCKDIR"
    fi
}

if [ "$MODE" != "status" ]; then
    if ! acquire_lock; then
        log "⚠️  另一個 omlx_switch 正在執行（pid=$(cat "$LOCKDIR/pid" 2>/dev/null)），跳過本次 $MODE 觸發"
        exit 0
    fi
fi

# ---- 通知管理員（寫旗標檔，由 MAGI daemon 掃到後發 DC）----
notify_admin() {
    local msg="$1"
    log "🚨 ALERT: $msg"
    printf '%s [omlx_switch alert] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$msg" >> "$ADMIN_NOTIFY_FILE"
}

# ---- A3: 等 port 關閉（bootout 後確保舊 process 真的死了）----
wait_port_closed() {
    local port="$1"
    local timeout="${2:-30}"
    local waited=0
    while [ "$waited" -lt "$timeout" ]; do
        if ! nc -z 127.0.0.1 "$port" 2>/dev/null; then
            return 0
        fi
        sleep 1
        waited=$((waited + 1))
    done
    log "⚠️  port $port 經過 ${timeout}s 仍未關閉，強制繼續"
    return 1
}

# ---- B1: 啟動前記憶體守門 ----
# 輸出可用記憶體（GB），用 vm_stat 算 free + inactive
available_memory_gb() {
    local page_size
    page_size=$(vm_stat | awk '/page size of/{print $8}')
    [ -z "$page_size" ] && page_size=16384
    local free inactive
    free=$(vm_stat | awk '/Pages free/{gsub("\\.","",$3); print $3}')
    inactive=$(vm_stat | awk '/Pages inactive/{gsub("\\.","",$3); print $3}')
    [ -z "$free" ] && free=0
    [ -z "$inactive" ] && inactive=0
    echo $(( (free + inactive) * page_size / 1024 / 1024 / 1024 ))
}

preflight_memory_check() {
    local required_gb="$1"
    local mode_name="$2"
    local avail
    avail=$(available_memory_gb)
    log "preflight: 可用記憶體 ${avail}GB，${mode_name} 需求 ${required_gb}GB"
    if [ "$avail" -lt "$required_gb" ]; then
        notify_admin "$mode_name 切換前可用記憶體不足（${avail}GB < ${required_gb}GB），已中止以避免當機"
        exit 2
    fi
}

# ---- B2: 切換後 heartbeat 驗證 MLX process 數量 ----
count_mlx_processes() {
    pgrep -f 'omlx serve' 2>/dev/null | wc -l | tr -d ' '
}

heartbeat_check() {
    # omlx serve 每實例可能 spawn parent+worker，所以用上限門檻而非精確比對
    # 參數：$1=期望端口數（1=night / 3=day），$2=mode 名稱
    local expected_ports="$1"
    local mode_name="$2"
    local upper_limit=$(( expected_ports * 2 + 1 ))
    sleep 60
    local count
    count=$(count_mlx_processes)
    log "heartbeat: ${mode_name} 實際 MLX process 數 = ${count}（上限 ${upper_limit}）"
    if [ "$count" -gt "$upper_limit" ]; then
        notify_admin "${mode_name} 切換後偵測到 ${count} 個 MLX process（上限 ${upper_limit}），疑似重複實例，請手動確認"
    fi
}

# 驗證必要模型存在
check_model_src() {
    local src="$1"
    if [ ! -d "$src" ]; then
        log "❌ ERROR: 模型目錄不存在: $src"
        exit 1
    fi
}

get_active_profile() {
    cat "$PROFILE_FILE" 2>/dev/null || echo "unknown"
}

case "$MODE" in
  day)
    log "→ DAY mode (E4B + Phi4 + SmolLM3)"
    check_model_src "$E4B_SRC"

    # 更新 models-text symlink → E4B
    rm -f "$MODELS_TEXT_DIR"/*
    ln -sf "$E4B_SRC" "$MODELS_TEXT_DIR/gemma-4-e4b-it-4bit"

    # 更新 models-text-e4b symlink（確保 E4B LaunchAgent 指向正確）
    rm -f /Users/ai/.omlx/models-text-e4b/*
    ln -sf "$E4B_SRC" "/Users/ai/.omlx/models-text-e4b/gemma-4-e4b-it-4bit"

    echo "day" > "$PROFILE_FILE"

    # 重啟 oMLX E4B（降低記憶體）
    launchctl bootout "gui/$UID_NUM/com.magi.omlx" 2>/dev/null || true
    wait_port_closed 8080 15
    # bootout 後才檢查記憶體（避免舊 process 佔用干擾判斷）
    preflight_memory_check 4 "DAY"
    # 日間：設定小記憶體限制
    /usr/libexec/PlistBuddy -c "Set :EnvironmentVariables:OMLX_TEXT_MAX_MODEL_MEMORY 6GB" \
        ~/Library/LaunchAgents/com.magi.omlx.plist 2>/dev/null || true
    /usr/libexec/PlistBuddy -c "Set :EnvironmentVariables:OMLX_TEXT_MAX_PROCESS_MEMORY 6GB" \
        ~/Library/LaunchAgents/com.magi.omlx.plist 2>/dev/null || true
    launchctl bootstrap "gui/$UID_NUM" ~/Library/LaunchAgents/com.magi.omlx.plist

    # 啟動 Phi-4 和 SmolLM3（若模型已下載）
    if [ -d "/Users/ai/.omlx/models/Phi-4-mini-instruct-4bit" ]; then
        rm -f /Users/ai/.omlx/models-text-phi4/*
        ln -sf "/Users/ai/.omlx/models/Phi-4-mini-instruct-4bit" \
               "/Users/ai/.omlx/models-text-phi4/Phi-4-mini-instruct-4bit"
        launchctl bootout "gui/$UID_NUM/com.magi.omlx-phi4" 2>/dev/null || true
        wait_port_closed 8082 10
        launchctl bootstrap "gui/$UID_NUM" ~/Library/LaunchAgents/com.magi.omlx-phi4.plist 2>/dev/null || true
        sleep 2
        launchctl kickstart -kp "gui/$UID_NUM/com.magi.omlx-phi4" 2>/dev/null || true
        log "Phi-4 啟動中..."
    else
        log "⚠️  Phi-4 模型尚未下載，跳過"
    fi

    if ls /Users/ai/.omlx/models/ | grep -q "SmolLM3"; then
        SMOL_MODEL=$(ls /Users/ai/.omlx/models/ | grep SmolLM3 | head -1)
        rm -f /Users/ai/.omlx/models-text-smol/*
        ln -sf "/Users/ai/.omlx/models/$SMOL_MODEL" \
               "/Users/ai/.omlx/models-text-smol/$SMOL_MODEL"
        launchctl bootout "gui/$UID_NUM/com.magi.omlx-smol" 2>/dev/null || true
        wait_port_closed 8083 10
        launchctl bootstrap "gui/$UID_NUM" ~/Library/LaunchAgents/com.magi.omlx-smol.plist 2>/dev/null || true
        sleep 2
        launchctl kickstart -kp "gui/$UID_NUM/com.magi.omlx-smol" 2>/dev/null || true
        log "SmolLM3 ($SMOL_MODEL) 啟動中..."
    else
        log "⚠️  SmolLM3 模型尚未下載，跳過"
    fi

    # 等待服務啟動
    sleep 20
    curl -sf http://127.0.0.1:8080/v1/models >/dev/null 2>&1 && log "8080 (E4B) OK" || log "8080 FAIL（可能還在啟動中）"
    curl -sf http://127.0.0.1:8082/v1/models >/dev/null 2>&1 && log "8082 (Phi-4) OK" || log "8082 未就緒（模型可能仍在載入）"
    curl -sf http://127.0.0.1:8083/v1/models >/dev/null 2>&1 && log "8083 (SmolLM3) OK" || log "8083 未就緒（模型可能仍在載入）"

    # heartbeat 背景執行，不阻塞腳本完成
    ( heartbeat_check 3 "DAY" ) &
    ;;

  night)
    log "→ NIGHT mode (26B only)"
    check_model_src "$B26_SRC"

    # 停止 Phi-4 和 SmolLM3
    launchctl bootout "gui/$UID_NUM/com.magi.omlx-phi4" 2>/dev/null || true
    launchctl bootout "gui/$UID_NUM/com.magi.omlx-smol" 2>/dev/null || true
    wait_port_closed 8082 15
    wait_port_closed 8083 15

    # 更新 models-text symlink → 26B
    rm -f "$MODELS_TEXT_DIR"/*
    ln -sf "$B26_SRC" "$MODELS_TEXT_DIR/gemma-4-26b-a4b-it-4bit"
    echo "night" > "$PROFILE_FILE"

    # 重啟 oMLX 26B（大記憶體，但下修為 14GB/12GB 保留系統緩衝）
    launchctl bootout "gui/$UID_NUM/com.magi.omlx" 2>/dev/null || true
    wait_port_closed 8080 30
    # 所有舊 process 都 bootout 後才檢查記憶體
    preflight_memory_check 10 "NIGHT"
    /usr/libexec/PlistBuddy -c "Set :EnvironmentVariables:OMLX_TEXT_MAX_MODEL_MEMORY 14GB" \
        ~/Library/LaunchAgents/com.magi.omlx.plist 2>/dev/null || true
    /usr/libexec/PlistBuddy -c "Set :EnvironmentVariables:OMLX_TEXT_MAX_PROCESS_MEMORY 12GB" \
        ~/Library/LaunchAgents/com.magi.omlx.plist 2>/dev/null || true
    launchctl bootstrap "gui/$UID_NUM" ~/Library/LaunchAgents/com.magi.omlx.plist || true
    sleep 3
    launchctl kickstart -kp "gui/$UID_NUM/com.magi.omlx" 2>&1 | grep -v "^$" | while read line; do log "kickstart: $line"; done || true

    sleep 120
    curl -sf http://127.0.0.1:8080/v1/models >/dev/null 2>&1 && log "8080 OK (26B)" || log "8080 FAIL — still loading, will be ready in ~1min"

    # heartbeat 背景執行
    ( heartbeat_check 1 "NIGHT" ) &
    ;;

  status)
    PROFILE=$(get_active_profile)
    log "Active profile: $PROFILE"
    log "Available memory: $(available_memory_gb)GB"
    log "MLX processes: $(count_mlx_processes)"
    curl -sf http://127.0.0.1:8080/v1/models >/dev/null 2>&1 && log "8080 UP" || log "8080 DOWN"
    curl -sf http://127.0.0.1:8082/v1/models >/dev/null 2>&1 && log "8082 UP" || log "8082 DOWN/OFF"
    curl -sf http://127.0.0.1:8083/v1/models >/dev/null 2>&1 && log "8083 UP" || log "8083 DOWN/OFF"
    ;;

  *)
    echo "Usage: $0 [day|night|status]"
    exit 1
    ;;
esac

log "Switch to $MODE complete (active_profile=$(get_active_profile))"
