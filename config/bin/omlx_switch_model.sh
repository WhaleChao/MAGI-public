#!/bin/bash
# oMLX 模型切換腳本 — 日間（三模型）/ 離峰（26B 單模型）
# 建立時間：2026-04-14
# 2026-04-19 強化：flock 互斥鎖 / 記憶體下修 / port-closed wait / preflight / heartbeat
set -euo pipefail

MODE="${1:-day}"

# ---- auto 模式：依當前時間自動選 day / night（在 lock 之前解析）----
# day 窗口：06:55-21:49（對齊 job_omlx_switch_day=06:55 / job_omlx_switch_night=21:50）
# 重要：auto 模式有冪等檢查 — 需「實際 API 模型」與 models-text 都對應正確才跳過切換
if [ "$MODE" = "auto" ]; then
    current_hour=$((10#$(date +%H)))
    current_minute=$((10#$(date +%M)))
    current_total_min=$((current_hour * 60 + current_minute))
    if [ "$current_total_min" -ge 415 ] && [ "$current_total_min" -lt 1310 ]; then
        MODE="day"
        EXPECTED_MODEL_KEYWORD="e4b"
    else
        MODE="night"
        EXPECTED_MODEL_KEYWORD="26b"
    fi
    printf '%s [switch] auto → %s (time=%02d:%02d)\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$MODE" "$current_hour" "$current_minute" | tee -a "/opt/homebrew/var/log/omlx_switch.log"
    # 冪等檢查：若 API 實際模型與 models-text 都正確且 oMLX 已在線，跳過切換
    current_model_in_dir=$(ls "/Users/ai/.omlx/models-text/" 2>/dev/null | tr '[:upper:]' '[:lower:]' | head -1)
    current_model_api=$(
        curl -sf --max-time 3 http://127.0.0.1:8080/v1/models 2>/dev/null | \
        python3 -c 'import json,sys; data=json.load(sys.stdin); print(((data.get("data") or [{}])[0].get("id") or "").lower())' 2>/dev/null || true
    )
    omlx_online=$(curl -sf --max-time 3 http://127.0.0.1:8080/v1/models >/dev/null 2>&1 && echo "yes" || echo "no")
    if echo "$current_model_in_dir" | grep -qi "$EXPECTED_MODEL_KEYWORD" && \
       echo "$current_model_api" | grep -qi "$EXPECTED_MODEL_KEYWORD" && \
       [ "$omlx_online" = "yes" ]; then
        printf '%s [switch] auto: 已是 %s 模式（api=%s, dir=%s），跳過切換\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$MODE" "$current_model_api" "$current_model_in_dir" | tee -a "/opt/homebrew/var/log/omlx_switch.log"
        exit 0
    fi
    printf '%s [switch] auto: 需切換（api=%s, dir=%s, online=%s）\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$current_model_api" "$current_model_in_dir" "$omlx_online" | tee -a "/opt/homebrew/var/log/omlx_switch.log"
fi

PROFILE_FILE="/Users/ai/.omlx/active_profile"
MODELS_TEXT_DIR="/Users/ai/.omlx/models-text"
E4B_SRC="/Users/ai/.omlx/models/gemma-4-e4b-it-4bit"
B26_SRC="/Users/ai/.omlx/models/gemma-4-26b-a4b-it-4bit"
B26_LEGACY_SRC="/Users/ai/.omlx/models/gemma-4-26b-a4b-it-UD-4bit"
UID_NUM=$(id -u)
LOG="/opt/homebrew/var/log/omlx_switch.log"
LOCKDIR="/tmp/omlx_switch.lock.d"
LOCK_STALE_SEC=600   # 超過 10 分鐘視為 stale（night 切換含 sleep 120+heartbeat 60，正常 3-5 分鐘內完成）
ADMIN_NOTIFY_FILE="/tmp/omlx_switch_alert.txt"

log() { printf '%s [switch] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" | tee -a "$LOG"; }

plist_set_env() {
    local key="$1"
    local value="$2"
    local plist="$HOME/Library/LaunchAgents/com.magi.omlx.plist"
    /usr/libexec/PlistBuddy -c "Set :EnvironmentVariables:${key} ${value}" "$plist" 2>/dev/null || \
        /usr/libexec/PlistBuddy -c "Add :EnvironmentVariables:${key} string ${value}" "$plist" 2>/dev/null || true
}

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

# ---- Layer 3: 檢查 pause 狀態（人工介入或反覆 abort 已觸發 TTL pause）----
# status / auto 模式不受 pause 影響（前者為唯讀，後者有冪等檢查）
GATEKEEPER="/Users/ai/Desktop/MAGI_v2/scripts/ops/omlx_switch_gatekeeper.py"
GATEKEEPER_PY="/Users/ai/Desktop/MAGI_v2/venv/bin/python3"
if [ "$MODE" != "status" ] && [ -x "$GATEKEEPER" ] && [ -x "$GATEKEEPER_PY" ]; then
    if ! MAGI_USE_RUNTIME_DIR=1 "$GATEKEEPER_PY" "$GATEKEEPER" check-paused 2>&1 | while read ln; do log "$ln"; done; then
        :  # while read wraps around pipeline; real exit code fetched below
    fi
    if ! MAGI_USE_RUNTIME_DIR=1 "$GATEKEEPER_PY" "$GATEKEEPER" check-paused >/dev/null 2>&1; then
        log "⚠️  omlx switch 處於 pause 狀態，跳過本次 $MODE 觸發"
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

clear_stale_8080_owner() {
    # A previous Docker/llama-server placeholder can survive with an empty
    # `-m` argument, returning /health OK but /v1/models=[] and blocking oMLX.
    local pids pid cmd
    pids=$(lsof -tiTCP:8080 -sTCP:LISTEN 2>/dev/null || true)
    [ -z "$pids" ] && return 0
    for pid in $pids; do
        cmd=$(ps -p "$pid" -o command= 2>/dev/null || true)
        [ -z "$cmd" ] && continue
        if echo "$cmd" | grep -q "omlx serve"; then
            continue
        fi
        if echo "$cmd" | grep -q "llama-server"; then
            log "⚠️  port 8080 被 stale llama-server 佔用，清除後再啟動 oMLX（pid=$pid）"
            kill "$pid" 2>/dev/null || true
            sleep 2
            if kill -0 "$pid" 2>/dev/null; then
                log "⚠️  stale llama-server 未退出，強制清除（pid=$pid）"
                kill -9 "$pid" 2>/dev/null || true
                sleep 1
            fi
        else
            log "⚠️  port 8080 被非 oMLX 程序佔用，保守不清除: pid=$pid cmd=$cmd"
        fi
    done
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
        if [ -x "$GATEKEEPER" ] && [ -x "$GATEKEEPER_PY" ]; then
            MAGI_USE_RUNTIME_DIR=1 "$GATEKEEPER_PY" "$GATEKEEPER" register-abort \
                --reason mem_insufficient --mode "$mode_name" \
                --extra "avail=${avail}GB,required=${required_gb}GB" >/dev/null 2>&1 || true
        fi
        exit 2
    fi
}

# ---- Layer 3: 檢查既有 omlx serve 的 RSS 是否已經失控 ----
preflight_oomlx_rss_check() {
    local max_gb="$1"
    local mode_name="$2"
    if [ ! -x "$GATEKEEPER" ] || [ ! -x "$GATEKEEPER_PY" ]; then
        return 0
    fi
    MAGI_USE_RUNTIME_DIR=1 "$GATEKEEPER_PY" "$GATEKEEPER" check-rss-before-switch \
        --max-model-memory-gb "$max_gb" --mode "$mode_name" 2>&1 | while read ln; do log "$ln"; done || true
    MAGI_USE_RUNTIME_DIR=1 "$GATEKEEPER_PY" "$GATEKEEPER" check-rss-before-switch \
        --max-model-memory-gb "$max_gb" --mode "$mode_name" >/dev/null 2>&1
    local rc=$?
    if [ "$rc" -eq 3 ]; then
        log "⚠️  Layer 3 RSS 檢查觸發 abort（rc=3），不進行 $mode_name 切換"
        exit 3
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
        notify_admin "${mode_name} 切換後偵測到 ${count} 個 MLX process（上限 ${upper_limit}），疑似重複實例，啟動 Layer 1 reaper"
    fi
    local reaper="/Users/ai/Desktop/MAGI_v2/scripts/ops/omlx_heartbeat_reaper.py"
    local py="/Users/ai/Desktop/MAGI_v2/venv/bin/python3"
    if [ -x "$reaper" ] && [ -x "$py" ]; then
        "$py" "$reaper" --expected-ports "$expected_ports" --mode-name "$mode_name" 2>&1 | while read ln; do log "$ln"; done || true
    else
        log "Layer 1 reaper 不可用（path=$reaper），跳過"
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

bootstrap_omlx_main() {
    local label="$1"
    local plist="$HOME/Library/LaunchAgents/com.magi.omlx.plist"
    launchctl bootstrap "gui/$UID_NUM" "$plist" 2>&1 | grep -v "^$" | while read line; do log "$label bootstrap: $line"; done || true
    sleep 2
    launchctl kickstart -kp "gui/$UID_NUM/com.magi.omlx" 2>&1 | grep -v "^$" | while read line; do log "$label kickstart: $line"; done || true
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

    preflight_oomlx_rss_check 6 "DAY"

    # 重啟 oMLX E4B（降低記憶體）
    launchctl bootout "gui/$UID_NUM/com.magi.omlx" 2>/dev/null || true
    clear_stale_8080_owner
    wait_port_closed 8080 15 || true
    # bootout 後才檢查記憶體（避免舊 process 佔用干擾判斷）
    preflight_memory_check 4 "DAY"
    # 日間：E4B 5.10GB + KV cache need 6.38GB → MODEL=8GB / PROCESS=10GB
    # 之前 6GB/6GB 會被 process_memory_enforcer 在啟動時強殺（2026-04-26 09:03/09:07 兩次 SIGABRT）
    plist_set_env OMLX_TEXT_MAX_MODEL_MEMORY 8GB
    plist_set_env OMLX_TEXT_MAX_PROCESS_MEMORY 10GB
    plist_set_env OMLX_TEXT_INITIAL_CACHE_BLOCKS 8
    plist_set_env OMLX_TEXT_HOT_CACHE_MAX_SIZE 512MB
    plist_set_env OMLX_TEXT_MAX_TOKENS 8192
    plist_set_env OMLX_TEXT_MAX_CONTEXT_WINDOW 8192
    plist_set_env OMLX_PAGED_CACHE_DIR /Users/ai/.omlx/cache-e4b
    bootstrap_omlx_main "DAY"

    # 啟動 Phi-4 和 SmolLM3（若模型已下載）
    if [ -d "/Users/ai/.omlx/models/Phi-4-mini-instruct-4bit" ]; then
        rm -f /Users/ai/.omlx/models-text-phi4/*
        ln -sf "/Users/ai/.omlx/models/Phi-4-mini-instruct-4bit" \
               "/Users/ai/.omlx/models-text-phi4/Phi-4-mini-instruct-4bit"
        launchctl bootout "gui/$UID_NUM/com.magi.omlx-phi4" 2>/dev/null || true
        wait_port_closed 8082 10 || true
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
        wait_port_closed 8083 10 || true
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
    if [ ! -d "$B26_SRC" ] && [ -d "$B26_LEGACY_SRC" ]; then
        B26_SRC="$B26_LEGACY_SRC"
    fi
    check_model_src "$B26_SRC"

    # 停止 Phi-4 和 SmolLM3
    launchctl bootout "gui/$UID_NUM/com.magi.omlx-phi4" 2>/dev/null || true
    launchctl bootout "gui/$UID_NUM/com.magi.omlx-smol" 2>/dev/null || true
    wait_port_closed 8082 15 || true
    wait_port_closed 8083 15 || true

    # 更新 models-text symlink → 26B
    rm -f "$MODELS_TEXT_DIR"/*
    ln -sf "$B26_SRC" "$MODELS_TEXT_DIR/gemma-4-26b-a4b-it-4bit"
    echo "night" > "$PROFILE_FILE"

    preflight_oomlx_rss_check 16 "NIGHT"

    # 重啟 oMLX 26B（模型實際約 14.63GB；MODEL 需高於模型大小，否則 completion 回 507）
    launchctl bootout "gui/$UID_NUM/com.magi.omlx" 2>/dev/null || true
    clear_stale_8080_owner
    wait_port_closed 8080 30 || true
    log "等待記憶體回收（10s）..."
    sleep 10
    # 所有舊 process 都 bootout 後才檢查記憶體（門檻 8GB：26B ceiling=16GB，系統本身 6-8GB）
    preflight_memory_check 8 "NIGHT"
    plist_set_env OMLX_TEXT_MAX_MODEL_MEMORY 16GB
    plist_set_env OMLX_TEXT_MAX_PROCESS_MEMORY 17GB
    plist_set_env OMLX_TEXT_INITIAL_CACHE_BLOCKS 2
    plist_set_env OMLX_TEXT_HOT_CACHE_MAX_SIZE 512MB
    plist_set_env OMLX_TEXT_MAX_TOKENS 8192
    plist_set_env OMLX_TEXT_MAX_CONTEXT_WINDOW 8192
    plist_set_env OMLX_PAGED_CACHE_DIR /Users/ai/.omlx/cache-26b
    bootstrap_omlx_main "NIGHT"

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
