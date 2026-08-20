#!/bin/bash
# oMLX 模型切換腳本 — 日間（4B + 輔助模型）/ 離峰（26B 優先，12B fallback）
# 建立時間：2026-04-14
# 2026-04-19 強化：flock 互斥鎖 / 記憶體下修 / port-closed wait / preflight / heartbeat
set -euo pipefail

MODE="${1:-day}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MAGI_ROOT_DIR="${MAGI_ROOT_DIR:-$(cd "$SCRIPT_DIR/../.." && pwd)}"
LOG="${MAGI_OMLX_SWITCH_LOG:-/opt/homebrew/var/log/omlx_switch.log}"
MAGI_PYTHON="${MAGI_V3_EXECUTABLE_PATH:-${MAGI_SKILL_PYTHON:-$MAGI_ROOT_DIR/venv/bin/python3}}"
if { [ -n "${MAGI_V3_RELEASE_ID:-}" ] || [ -n "${MAGI_V3_DEPLOYMENT_MODE:-}" ]; } && \
   { [ -z "${MAGI_V3_EXECUTABLE_PATH:-}" ] || [ ! -x "$MAGI_V3_EXECUTABLE_PATH" ]; }; then
    printf 'sealed V3 oMLX switch requires its verified Python launcher\n' >&2
    exit 2
fi

probe_model_id_at_port() {
    local port="$1"
    curl -sf --max-time 3 "http://127.0.0.1:${port}/v1/models" 2>/dev/null | \
        python3 -c 'import json,sys; data=json.load(sys.stdin); print(((data.get("data") or [{}])[0].get("id") or "").lower())' 2>/dev/null || true
}

DAY_PRIMARY_MODEL_KEYWORD="${MAGI_DAY_MODEL_KEYWORD:-e4b}"
DAY_FALLBACK_MODEL_KEYWORD="${MAGI_DAY_FALLBACK_MODEL_KEYWORD:-e4b}"
NIGHT_PRIMARY_MODEL_KEYWORD="${MAGI_NIGHT_MODEL_KEYWORD:-26b}"
NIGHT_FALLBACK_MODEL_KEYWORD="${MAGI_NIGHT_FALLBACK_MODEL_KEYWORD:-12b}"
ACTIVE_PROFILE_FILE="${HOME}/.omlx/active_profile"
DAY_FALLBACK_STAMP_FILE="${HOME}/.omlx/day_fallback_stamp"
NIGHT_FALLBACK_STAMP_FILE="${HOME}/.omlx/night_fallback_stamp"
DAY_FALLBACK_RETRY_SEC="${MAGI_DAY_FALLBACK_RETRY_SEC:-3600}"
NIGHT_FALLBACK_RETRY_SEC="${MAGI_NIGHT_FALLBACK_RETRY_SEC:-21600}"

fallback_cooldown_active() {
    local stamp_file="$1"
    local retry_sec="$2"
    [ -f "$stamp_file" ] || return 1
    local stamp now age
    stamp=$(stat -f %m "$stamp_file" 2>/dev/null || echo 0)
    now=$(date +%s)
    age=$(( now - stamp ))
    [ "$age" -lt "$retry_sec" ]
}

day_fallback_cooldown_active() {
    fallback_cooldown_active "$DAY_FALLBACK_STAMP_FILE" "$DAY_FALLBACK_RETRY_SEC"
}

night_fallback_cooldown_active() {
    fallback_cooldown_active "$NIGHT_FALLBACK_STAMP_FILE" "$NIGHT_FALLBACK_RETRY_SEC"
}

# ---- auto 模式：依當前時間自動選 day / night（在 lock 之前解析）----
# day 窗口：06:35-21:49（06:35 排程重開後即應進入日間；06:55 只是安全重試）
# 重要：auto 模式有冪等檢查 — 需「實際 API 模型」與 models-text 都對應正確才跳過切換
if [ "$MODE" = "auto" ]; then
    current_hour=$((10#$(date +%H)))
    current_minute=$((10#$(date +%M)))
    current_total_min=$((current_hour * 60 + current_minute))
    if [ "$current_total_min" -ge 395 ] && [ "$current_total_min" -lt 1310 ]; then
        MODE="day"
        EXPECTED_MODEL_KEYWORD="$DAY_PRIMARY_MODEL_KEYWORD"
    else
        MODE="night"
        EXPECTED_MODEL_KEYWORD="$NIGHT_PRIMARY_MODEL_KEYWORD"
    fi
    printf '%s [switch] auto → %s (time=%02d:%02d)\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$MODE" "$current_hour" "$current_minute" | tee -a "$LOG"
    # 冪等檢查：若 API 實際模型與 models-text 都正確且 oMLX 已在線，跳過切換
    current_model_in_dir=$(ls "${HOME}/.omlx/models-text/" 2>/dev/null | tr '[:upper:]' '[:lower:]' | head -1)
    current_model_api=$(
        probe_model_id_at_port 8080
    )
    current_phi4_api=$(probe_model_id_at_port 8082)
    current_smol_api=$(probe_model_id_at_port 8083)
    active_profile_auto=$(cat "$ACTIVE_PROFILE_FILE" 2>/dev/null || echo "")
    omlx_online=$(curl -sf --max-time 3 http://127.0.0.1:8080/v1/models >/dev/null 2>&1 && echo "yes" || echo "no")
    sidecars_ok="yes"
    if [ "$MODE" = "day" ]; then
        if [ -d "${HOME}/.omlx/models/Phi-4-mini-instruct-4bit" ] && ! echo "$current_phi4_api" | grep -qi "phi"; then
            sidecars_ok="no"
        fi
        if ls "${HOME}/.omlx/models/" 2>/dev/null | grep -q "SmolLM3" && ! echo "$current_smol_api" | grep -qi "smol"; then
            sidecars_ok="no"
        fi
    else
        # 離峰夜間只保留主模型；若日間 sidecar 還活著，仍需執行切換將其關閉。
        if [ -n "$current_phi4_api" ] || [ -n "$current_smol_api" ]; then
            sidecars_ok="no"
        fi
    fi
    if echo "$current_model_in_dir" | grep -qi "$EXPECTED_MODEL_KEYWORD" && \
       echo "$current_model_api" | grep -qi "$EXPECTED_MODEL_KEYWORD" && \
       [ "$omlx_online" = "yes" ] && \
       [ "$sidecars_ok" = "yes" ]; then
        if [ "$MODE" = "day" ] && [ "$active_profile_auto" != "day" ]; then
            echo "day" > "$ACTIVE_PROFILE_FILE"
            rm -f "$DAY_FALLBACK_STAMP_FILE"
            rm -f "$NIGHT_FALLBACK_STAMP_FILE"
            active_profile_auto="day"
        fi
        printf '%s [switch] auto: 已是 %s 模式（api=%s, dir=%s, phi4=%s, smol=%s），跳過切換\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$MODE" "$current_model_api" "$current_model_in_dir" "${current_phi4_api:-off}" "${current_smol_api:-off}" | tee -a "$LOG"
        exit 0
    fi
    if [ "$MODE" = "day" ] && \
       [ "$active_profile_auto" = "day-e4b-degraded" ] && \
       echo "$current_model_in_dir" | grep -qi "$DAY_FALLBACK_MODEL_KEYWORD" && \
       echo "$current_model_api" | grep -qi "$DAY_FALLBACK_MODEL_KEYWORD" && \
       [ "$omlx_online" = "yes" ] && \
       [ "$sidecars_ok" = "yes" ] && \
       day_fallback_cooldown_active; then
        printf '%s [switch] auto: day fallback 冷卻中（api=%s, dir=%s），保留 E4B degraded，避免反覆重啟\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$current_model_api" "$current_model_in_dir" | tee -a "$LOG"
        exit 0
    fi
    if [ "$MODE" = "night" ] && \
       [ "$active_profile_auto" = "night-12b-degraded" ] && \
       echo "$current_model_in_dir" | grep -qi "$NIGHT_FALLBACK_MODEL_KEYWORD" && \
       echo "$current_model_api" | grep -qi "$NIGHT_FALLBACK_MODEL_KEYWORD" && \
       [ "$omlx_online" = "yes" ] && \
       [ "$sidecars_ok" = "yes" ] && \
       night_fallback_cooldown_active; then
        printf '%s [switch] auto: night 12B fallback 冷卻中（api=%s, dir=%s），暫緩重試 26B，避免反覆重啟\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$current_model_api" "$current_model_in_dir" | tee -a "$LOG"
        exit 0
    fi
    if [ "$MODE" = "night" ] && \
       [ "$active_profile_auto" = "night-e4b-degraded" ] && \
       echo "$current_model_in_dir" | grep -qi "$DAY_FALLBACK_MODEL_KEYWORD" && \
       echo "$current_model_api" | grep -qi "$DAY_FALLBACK_MODEL_KEYWORD" && \
       [ "$omlx_online" = "yes" ] && \
       [ "$sidecars_ok" = "yes" ] && \
       night_fallback_cooldown_active; then
        printf '%s [switch] auto: night E4B 最後保底冷卻中（api=%s, dir=%s），暫緩重試 26B/12B，避免每 15 分鐘中斷 8080\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$current_model_api" "$current_model_in_dir" | tee -a "$LOG"
        exit 0
    fi
    printf '%s [switch] auto: 需切換（api=%s, dir=%s, online=%s, phi4=%s, smol=%s, sidecars_ok=%s）\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$current_model_api" "$current_model_in_dir" "$omlx_online" "${current_phi4_api:-off}" "${current_smol_api:-off}" "$sidecars_ok" | tee -a "$LOG"
fi

PROFILE_FILE="$ACTIVE_PROFILE_FILE"
MODELS_TEXT_DIR="${HOME}/.omlx/models-text"
E4B_SRC="${HOME}/.omlx/models/gemma-4-e4b-it-4bit"
DAY12_SRC="${HOME}/.omlx/models/gemma-4-12B-it-4bit"
DAY_PRIMARY_SRC="${MAGI_DAY_MODEL_SRC:-$E4B_SRC}"
DAY_PRIMARY_LINK_NAME="${MAGI_DAY_MODEL_LINK_NAME:-$(basename "$DAY_PRIMARY_SRC")}"
DAY_PRIMARY_MAX_MODEL_MEMORY="${MAGI_DAY_MODEL_MAX_MODEL_MEMORY:-8GB}"
DAY_PRIMARY_MAX_PROCESS_MEMORY="${MAGI_DAY_MODEL_MAX_PROCESS_MEMORY:-10GB}"
DAY_PRIMARY_INITIAL_CACHE_BLOCKS="${MAGI_DAY_MODEL_INITIAL_CACHE_BLOCKS:-8}"
DAY_PRIMARY_HOT_CACHE_MAX_SIZE="${MAGI_DAY_MODEL_HOT_CACHE_MAX_SIZE:-512MB}"
DAY_PRIMARY_MAX_TOKENS="${MAGI_DAY_MODEL_MAX_TOKENS:-8192}"
DAY_PRIMARY_MAX_CONTEXT_WINDOW="${MAGI_DAY_MODEL_MAX_CONTEXT_WINDOW:-8192}"
DAY_PRIMARY_MIN_FREE_GB="${MAGI_DAY_MODEL_MIN_FREE_GB:-3}"
NIGHT_FALLBACK_12B_SRC="${MAGI_NIGHT_FALLBACK_MODEL_SRC:-$DAY12_SRC}"
NIGHT_FALLBACK_12B_LINK_NAME="${MAGI_NIGHT_FALLBACK_MODEL_LINK_NAME:-$(basename "$NIGHT_FALLBACK_12B_SRC")}"
NIGHT_FALLBACK_12B_MAX_MODEL_MEMORY="${MAGI_NIGHT_FALLBACK_12B_MAX_MODEL_MEMORY:-13GB}"
NIGHT_FALLBACK_12B_MAX_PROCESS_MEMORY="${MAGI_NIGHT_FALLBACK_12B_MAX_PROCESS_MEMORY:-18GB}"
NIGHT_FALLBACK_12B_INITIAL_CACHE_BLOCKS="${MAGI_NIGHT_FALLBACK_12B_INITIAL_CACHE_BLOCKS:-4}"
NIGHT_FALLBACK_12B_HOT_CACHE_MAX_SIZE="${MAGI_NIGHT_FALLBACK_12B_HOT_CACHE_MAX_SIZE:-0}"
NIGHT_FALLBACK_12B_MAX_TOKENS="${MAGI_NIGHT_FALLBACK_12B_MAX_TOKENS:-8192}"
NIGHT_FALLBACK_12B_MAX_CONTEXT_WINDOW="${MAGI_NIGHT_FALLBACK_12B_MAX_CONTEXT_WINDOW:-8192}"
GEMMA4_UNIFIED_WRAPPER="${MAGI_OMLX_GEMMA4_WRAPPER:-${HOME}/.omlx/bin/omlx-gemma4-unified-serve}"
# Unified Gemma4 is a host singleton.  It must use the verified, stable
# runtime Python rather than a release launcher: a LaunchAgent does not carry
# the release manifest/SHA bindings required by magi-v3-python, and an old
# release path becomes invalid after archival.
GEMMA4_UNIFIED_PYTHON="${MAGI_OMLX_GEMMA4_PYTHON:-${MAGI_V3_PYTHON_RUNTIME_REALPATH:-${MAGI_V3_PYTHON_RUNTIME:-${MAGI_SKILL_PYTHON:-$MAGI_PYTHON}}}}"
B26_SRC="${HOME}/.omlx/models/gemma-4-26b-a4b-it-4bit"
B26_LEGACY_SRC="${HOME}/.omlx/models/gemma-4-26b-a4b-it-UD-4bit"
UID_NUM=$(id -u)
LOCKDIR="${MAGI_OMLX_SWITCH_LOCKDIR:-/tmp/omlx_switch.lock.d}"
LOCK_STALE_SEC=600   # 超過 10 分鐘視為 stale（night 切換含 sleep 120+heartbeat 60，正常 3-5 分鐘內完成）
ADMIN_NOTIFY_FILE="${MAGI_OMLX_SWITCH_ALERT_FILE:-/tmp/omlx_switch_alert.txt}"
OMLX_SSD_CACHE_ROOT="${MAGI_OMLX_PAGED_CACHE_ROOT:-$HOME/.omlx/paged-cache}"
OMLX_CACHE_ON_SSD="${MAGI_OMLX_CACHE_ON_SSD:-1}"

log() { printf '%s [switch] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" | tee -a "$LOG"; }

plist_set_env() {
    local key="$1"
    local value="$2"
    local plist="$HOME/Library/LaunchAgents/com.magi.omlx.plist"
    /usr/libexec/PlistBuddy -c "Set :EnvironmentVariables:${key} ${value}" "$plist" 2>/dev/null || \
        /usr/libexec/PlistBuddy -c "Add :EnvironmentVariables:${key} string ${value}" "$plist" 2>/dev/null || true
}

plist_set_program_arg() {
    local plist="$1"
    local index="$2"
    local value="$3"
    /usr/libexec/PlistBuddy -c "Set :ProgramArguments:${index} ${value}" "$plist" 2>/dev/null || true
}

omlx_cache_dir() {
    local cache_name="$1"
    local fallback="$2"
    local root="$OMLX_SSD_CACHE_ROOT"
    if [ "$OMLX_CACHE_ON_SSD" != "0" ] && [ -n "$root" ]; then
        local candidate="$root/$cache_name"
        local probe="/tmp/magi_omlx_cache_probe.$$.$cache_name"
        (
            mkdir -p "$candidate" &&
            : > "$candidate/.magi_cache_probe" &&
            rm -f "$candidate/.magi_cache_probe" &&
            printf 'ok' > "$probe"
        ) >/dev/null 2>&1 &
        local pid=$!
        local waited=0
        while [ "$waited" -lt 3 ]; do
            if ! kill -0 "$pid" 2>/dev/null; then
                wait "$pid" 2>/dev/null || true
                break
            fi
            sleep 1
            waited=$((waited + 1))
        done
        if kill -0 "$pid" 2>/dev/null; then
            kill "$pid" 2>/dev/null || true
        elif [ -f "$probe" ]; then
            rm -f "$probe"
            printf '%s\n' "$candidate"
            return 0
        fi
        rm -f "$probe" 2>/dev/null || true
    fi
    mkdir -p "$fallback" 2>/dev/null || true
    printf '%s\n' "$fallback"
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
GATEKEEPER="$MAGI_ROOT_DIR/scripts/ops/omlx_switch_gatekeeper.py"
GATEKEEPER_PY="$MAGI_PYTHON"
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
            log "⚠️  port 8080 被 stale llama-server 佔用，清除後再啟動 oMLX（pid=${pid}）"
            kill "$pid" 2>/dev/null || true
            sleep 2
            if kill -0 "$pid" 2>/dev/null; then
                log "⚠️  stale llama-server 未退出，強制清除（pid=${pid}）"
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
    local on_fail="${3:-exit}"
    local avail
    avail=$(available_memory_gb)
    log "preflight: 可用記憶體 ${avail}GB，${mode_name} 需求 ${required_gb}GB"
    if [ "$avail" -lt "$required_gb" ]; then
        local governor="$MAGI_ROOT_DIR/scripts/ops/resource_governor.py"
        if [ -x "$GATEKEEPER_PY" ] && [ -f "$governor" ]; then
            log "preflight: 記憶體不足，先執行 resource_governor safe cleanup 後重試"
            MAGI_USE_RUNTIME_DIR=1 "$GATEKEEPER_PY" "$governor" --json prepare-switch \
                --mode "$mode_name" --required-free-gb "$required_gb" --enforce 2>&1 | \
                while read ln; do log "[resource_governor] $ln"; done || true
            sleep 15
            avail=$(available_memory_gb)
            log "preflight retry: 可用記憶體 ${avail}GB，${mode_name} 需求 ${required_gb}GB"
        fi
    fi
    if [ "$avail" -lt "$required_gb" ]; then
        notify_admin "$mode_name 切換前可用記憶體不足（${avail}GB < ${required_gb}GB），已中止以避免當機"
        if [ -x "$GATEKEEPER" ] && [ -x "$GATEKEEPER_PY" ]; then
            MAGI_USE_RUNTIME_DIR=1 "$GATEKEEPER_PY" "$GATEKEEPER" register-abort \
                --reason mem_insufficient --mode "$mode_name" \
                --extra "avail=${avail}GB,required=${required_gb}GB" >/dev/null 2>&1 || true
        fi
        if [ "$on_fail" = "return" ]; then
            return 2
        fi
        exit 2
    fi
    return 0
}

configure_e4b_runtime_env() {
    local paged_cache_dir
    paged_cache_dir=$(omlx_cache_dir cache-e4b "${HOME}/.omlx/cache-e4b")
    plist_set_env OMLX_TEXT_MAX_MODEL_MEMORY 8GB
    plist_set_env OMLX_TEXT_MAX_PROCESS_MEMORY 10GB
    plist_set_env OMLX_TEXT_INITIAL_CACHE_BLOCKS 8
    plist_set_env OMLX_TEXT_HOT_CACHE_MAX_SIZE 512MB
    plist_set_env OMLX_TEXT_MAX_TOKENS 8192
    plist_set_env OMLX_TEXT_MAX_CONTEXT_WINDOW 8192
    plist_set_env OMLX_TEXT_DISABLE_CACHE 0
    plist_set_env OMLX_GEMMA4_UNIFIED_RUNTIME 0
    plist_set_env OMLX_GEMMA4_UNIFIED_WRAPPER "$GEMMA4_UNIFIED_WRAPPER"
    plist_set_env MAGI_OMLX_GEMMA4_PYTHON "$GEMMA4_UNIFIED_PYTHON"
    plist_set_env OMLX_PAGED_CACHE_DIR "$paged_cache_dir"
    log "oMLX paged cache (E4B): $paged_cache_dir"
}

configure_day_primary_runtime_env() {
    if echo "$DAY_PRIMARY_MODEL_KEYWORD" | grep -qi "e4b"; then
        configure_e4b_runtime_env
        return
    fi
    configure_12b_runtime_env "day primary"
}

configure_12b_runtime_env() {
    local profile_label="${1:-12B fallback}"
    local paged_cache_dir
    paged_cache_dir=$(omlx_cache_dir cache-gemma4-12b "${HOME}/.omlx/cache-gemma4-12b")
    plist_set_env OMLX_TEXT_MAX_MODEL_MEMORY "$NIGHT_FALLBACK_12B_MAX_MODEL_MEMORY"
    plist_set_env OMLX_TEXT_MAX_PROCESS_MEMORY "$NIGHT_FALLBACK_12B_MAX_PROCESS_MEMORY"
    plist_set_env OMLX_TEXT_INITIAL_CACHE_BLOCKS "$NIGHT_FALLBACK_12B_INITIAL_CACHE_BLOCKS"
    plist_set_env OMLX_TEXT_HOT_CACHE_MAX_SIZE "$NIGHT_FALLBACK_12B_HOT_CACHE_MAX_SIZE"
    plist_set_env OMLX_TEXT_MAX_TOKENS "$NIGHT_FALLBACK_12B_MAX_TOKENS"
    plist_set_env OMLX_TEXT_MAX_CONTEXT_WINDOW "$NIGHT_FALLBACK_12B_MAX_CONTEXT_WINDOW"
    plist_set_env OMLX_TEXT_DISABLE_CACHE 1
    plist_set_env OMLX_GEMMA4_UNIFIED_RUNTIME 1
    plist_set_env OMLX_GEMMA4_UNIFIED_WRAPPER "$GEMMA4_UNIFIED_WRAPPER"
    plist_set_env MAGI_OMLX_GEMMA4_PYTHON "$GEMMA4_UNIFIED_PYTHON"
    plist_set_env OMLX_PAGED_CACHE_DIR "$paged_cache_dir"
    log "oMLX paged cache (${profile_label}): $paged_cache_dir"
}

configure_night_runtime_env() {
    local paged_cache_dir
    paged_cache_dir=$(omlx_cache_dir cache-26b "${HOME}/.omlx/cache-26b")
    plist_set_env OMLX_TEXT_MAX_MODEL_MEMORY 16GB
    plist_set_env OMLX_TEXT_MAX_PROCESS_MEMORY 17GB
    plist_set_env OMLX_TEXT_INITIAL_CACHE_BLOCKS 2
    plist_set_env OMLX_TEXT_HOT_CACHE_MAX_SIZE 512MB
    plist_set_env OMLX_TEXT_MAX_TOKENS 8192
    plist_set_env OMLX_TEXT_MAX_CONTEXT_WINDOW 8192
    plist_set_env OMLX_TEXT_DISABLE_CACHE 0
    plist_set_env OMLX_GEMMA4_UNIFIED_RUNTIME 0
    plist_set_env OMLX_GEMMA4_UNIFIED_WRAPPER "$GEMMA4_UNIFIED_WRAPPER"
    plist_set_env MAGI_OMLX_GEMMA4_PYTHON "$GEMMA4_UNIFIED_PYTHON"
    plist_set_env OMLX_PAGED_CACHE_DIR "$paged_cache_dir"
    log "oMLX paged cache (night): $paged_cache_dir"
}

start_night_e4b_last_resort() {
    local reason="${1:-26B 與 12B 都不可用}"
    log "⚠️  NIGHT ${reason}，最後保底啟動 E4B 主模型，避免 MAGI 無主模型"
    check_model_src "$E4B_SRC"
    rm -f "$MODELS_TEXT_DIR"/*
    ln -sf "$E4B_SRC" "$MODELS_TEXT_DIR/gemma-4-e4b-it-4bit"
    rm -f "${HOME}"/.omlx/models-text-e4b/*
    ln -sf "$E4B_SRC" "${HOME}/.omlx/models-text-e4b/gemma-4-e4b-it-4bit"
    configure_e4b_runtime_env
    launchctl bootout "gui/$UID_NUM/com.magi.omlx" 2>/dev/null || true
    wait_launchctl_unloaded "com.magi.omlx" 12 || true
    clear_stale_8080_owner
    wait_port_closed 8080 20 || true
    if ! bootstrap_omlx_main "NIGHT-LAST-RESORT-E4B"; then
        notify_admin "NIGHT last-resort E4B launchd 啟動失敗，請檢查 /opt/homebrew/var/log/omlx.log"
        exit 4
    fi
    if ! wait_model_ready 8080 "e4b" 150; then
        notify_admin "NIGHT last-resort E4B 也未載入，請檢查 launchd/oMLX log"
        exit 4
    fi
    echo "night-e4b-degraded" > "$PROFILE_FILE"
    mkdir -p "$(dirname "$NIGHT_FALLBACK_STAMP_FILE")"
    date +%s > "$NIGHT_FALLBACK_STAMP_FILE"
    notify_admin "NIGHT ${reason}，12B fallback 不可用，已最後保底啟動 E4B；待資源恢復後下次夜間切換會再嘗試 26B"
}

preserve_current_e4b_for_night() {
    local reason="${1:-本機資源低水位}"
    local current_model
    current_model=$(probe_model_id_at_port 8080)
    echo "$current_model" | grep -qi "$DAY_FALLBACK_MODEL_KEYWORD" || return 1

    # A healthy E4B is already serving traffic.  Under resource pressure the
    # safest night transition is to stop only the day sidecars and retain the
    # main model.  Restarting 8080 merely to attempt a larger fallback creates
    # several minutes of avoidable downtime and can amplify memory pressure.
    launchctl bootout "gui/$UID_NUM/com.magi.omlx-phi4" 2>/dev/null || true
    launchctl bootout "gui/$UID_NUM/com.magi.omlx-smol" 2>/dev/null || true
    wait_launchctl_unloaded "com.magi.omlx-phi4" 12 || true
    wait_launchctl_unloaded "com.magi.omlx-smol" 12 || true
    wait_port_closed 8082 15 || true
    wait_port_closed 8083 15 || true
    configure_e4b_runtime_env
    echo "night-e4b-degraded" > "$PROFILE_FILE"
    mkdir -p "$(dirname "$NIGHT_FALLBACK_STAMP_FILE")"
    date +%s > "$NIGHT_FALLBACK_STAMP_FILE"
    log "NIGHT ${reason}；保留既有健康 E4B，未中斷 8080，六小時後再評估大型模型"
    return 0
}

start_night_12b_fallback() {
    local reason="${1:-26B 資源不足}"
    log "⚠️  NIGHT ${reason}，改啟動 12B fallback，避免退回 E4B"
    if [ ! -d "$NIGHT_FALLBACK_12B_SRC" ]; then
        start_night_e4b_last_resort "${reason}；12B 模型目錄不存在: $NIGHT_FALLBACK_12B_SRC"
        return
    fi
    if [ ! -x "$GEMMA4_UNIFIED_WRAPPER" ]; then
        start_night_e4b_last_resort "${reason}；Gemma4 unified wrapper 不存在或不可執行: $GEMMA4_UNIFIED_WRAPPER"
        return
    fi

    launchctl bootout "gui/$UID_NUM/com.magi.omlx-phi4" 2>/dev/null || true
    launchctl bootout "gui/$UID_NUM/com.magi.omlx-smol" 2>/dev/null || true
    wait_launchctl_unloaded "com.magi.omlx-phi4" 12 || true
    wait_launchctl_unloaded "com.magi.omlx-smol" 12 || true
    wait_port_closed 8082 15 || true
    wait_port_closed 8083 15 || true

    rm -f "$MODELS_TEXT_DIR"/*
    ln -sf "$NIGHT_FALLBACK_12B_SRC" "$MODELS_TEXT_DIR/$NIGHT_FALLBACK_12B_LINK_NAME"
    rm -f "${HOME}"/.omlx/models-text-e4b/*
    ln -sf "$E4B_SRC" "${HOME}/.omlx/models-text-e4b/gemma-4-e4b-it-4bit"
    configure_12b_runtime_env "night 12B fallback"

    launchctl bootout "gui/$UID_NUM/com.magi.omlx" 2>/dev/null || true
    wait_launchctl_unloaded "com.magi.omlx" 12 || true
    clear_stale_8080_owner
    wait_port_closed 8080 20 || true
    if ! bootstrap_omlx_main "NIGHT-FALLBACK-12B"; then
        start_night_e4b_last_resort "${reason}；12B launchd 啟動失敗"
        return
    fi
    if ! wait_model_ready 8080 "$NIGHT_FALLBACK_MODEL_KEYWORD" 180; then
        start_night_e4b_last_resort "${reason}；12B 未於時限內載入"
        return
    fi
    echo "night-12b-degraded" > "$PROFILE_FILE"
    mkdir -p "$(dirname "$NIGHT_FALLBACK_STAMP_FILE")"
    date +%s > "$NIGHT_FALLBACK_STAMP_FILE"
    notify_admin "NIGHT ${reason}，已啟動 12B fallback；E4B 僅保留最後保底"
}

start_day_e4b_fallback() {
    local reason="${1:-12B 啟動失敗}"
    DAY_MAIN_DEGRADED=1
    log "⚠️  DAY ${reason}，降級啟動 E4B 主模型，避免 MAGI 無主模型"
    check_model_src "$E4B_SRC"
    rm -f "$MODELS_TEXT_DIR"/*
    ln -sf "$E4B_SRC" "$MODELS_TEXT_DIR/gemma-4-e4b-it-4bit"
    rm -f "${HOME}"/.omlx/models-text-e4b/*
    ln -sf "$E4B_SRC" "${HOME}/.omlx/models-text-e4b/gemma-4-e4b-it-4bit"
    configure_e4b_runtime_env
    launchctl bootout "gui/$UID_NUM/com.magi.omlx" 2>/dev/null || true
    wait_launchctl_unloaded "com.magi.omlx" 12 || true
    clear_stale_8080_owner
    wait_port_closed 8080 20 || true
    if ! bootstrap_omlx_main "DAY-FALLBACK-E4B"; then
        notify_admin "DAY fallback E4B launchd 啟動失敗，請檢查 /opt/homebrew/var/log/omlx.log"
        exit 4
    fi
    if ! wait_model_ready 8080 "$DAY_FALLBACK_MODEL_KEYWORD" 90; then
        notify_admin "DAY fallback E4B 也未載入，請檢查 launchd/oMLX log"
        exit 4
    fi
    echo "day-e4b-degraded" > "$PROFILE_FILE"
    mkdir -p "$(dirname "$DAY_FALLBACK_STAMP_FILE")"
    date +%s > "$DAY_FALLBACK_STAMP_FILE"
    notify_admin "DAY ${reason}，已降級啟動 E4B；下一次日間切換會再嘗試 12B"
}

resource_guard_allows_night_26b() {
    local governor="$MAGI_ROOT_DIR/scripts/ops/resource_governor.py"
    [ -x "$GATEKEEPER_PY" ] || return 0
    [ -f "$governor" ] || return 0
    local payload level disk_free
    payload=$(MAGI_USE_RUNTIME_DIR=1 "$GATEKEEPER_PY" "$governor" --json status 2>/dev/null || true)
    [ -n "$payload" ] || return 0
    level=$(printf '%s' "$payload" | "$GATEKEEPER_PY" -c 'import json,sys; print(json.load(sys.stdin).get("level","unknown"))' 2>/dev/null || echo "unknown")
    disk_free=$(printf '%s' "$payload" | "$GATEKEEPER_PY" -c 'import json,sys; print((json.load(sys.stdin).get("snapshot") or {}).get("disk_free_gb",-1))' 2>/dev/null || echo "-1")
    log "resource guard: level=${level}, disk_free=${disk_free}GB"
    if [ "$level" = "core_only" ] || [ "$level" = "critical" ]; then
        return 1
    fi
    "$GATEKEEPER_PY" - "$disk_free" <<'PY'
import sys
try:
    disk_free = float(sys.argv[1])
except Exception:
    disk_free = 999.0
raise SystemExit(0 if disk_free >= 35.0 else 1)
PY
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
    local reaper="$MAGI_ROOT_DIR/scripts/ops/omlx_heartbeat_reaper.py"
    local py="$MAGI_PYTHON"
    if [ -x "$reaper" ] && [ -x "$py" ]; then
        "$py" "$reaper" --expected-ports "$expected_ports" --mode-name "$mode_name" 2>&1 | while read ln; do log "$ln"; done || true
    else
        log "Layer 1 reaper 不可用（path=${reaper}），跳過"
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

run_launchctl_logged() {
    local tag="$1"
    shift
    local out rc
    set +e
    out=$("$@" 2>&1)
    rc=$?
    set -e
    if [ -n "$out" ]; then
        while IFS= read -r line; do
            [ -n "$line" ] && log "$tag: $line"
        done <<< "$out"
    fi
    return "$rc"
}

launchctl_service_loaded() {
    local label="$1"
    launchctl print "gui/$UID_NUM/$label" >/dev/null 2>&1
}

wait_launchctl_unloaded() {
    local label="$1"
    local timeout="${2:-10}"
    local waited=0
    while [ "$waited" -lt "$timeout" ]; do
        if ! launchctl_service_loaded "$label"; then
            return 0
        fi
        sleep 1
        waited=$((waited + 1))
    done
    log "⚠️  launchd service $label 經過 ${timeout}s 仍未卸載，稍後嘗試重新載入"
    return 1
}

start_launch_agent() {
    local label="$1"
    local plist="$2"
    local tag="$3"
    run_launchctl_logged "$tag enable" launchctl enable "gui/$UID_NUM/$label" || true
    if ! launchctl_service_loaded "$label"; then
        if ! run_launchctl_logged "$tag bootstrap" launchctl bootstrap "gui/$UID_NUM" "$plist"; then
            sleep 2
            if ! launchctl_service_loaded "$label"; then
                run_launchctl_logged "$tag bootstrap retry" launchctl bootstrap "gui/$UID_NUM" "$plist" || return 1
            fi
        fi
    fi
    sleep 2
    run_launchctl_logged "$tag kickstart" launchctl kickstart -kp "gui/$UID_NUM/$label" || return 1
}

restart_launch_agent() {
    local label="$1"
    local plist="$2"
    local tag="$3"
    run_launchctl_logged "$tag bootout" launchctl bootout "gui/$UID_NUM/$label" || true
    wait_launchctl_unloaded "$label" 12 || true
    start_launch_agent "$label" "$plist" "$tag"
}

bootstrap_omlx_main() {
    local label="$1"
    local plist="$HOME/Library/LaunchAgents/com.magi.omlx.plist"
    # 主模型必須明確解除 Disabled 狀態；這台電腦排程重開後，
    # launchd 可能保留 disabled bit，單純 bootstrap/kickstart 會失敗。
    run_launchctl_logged "$label enable-main" launchctl enable "gui/$UID_NUM/com.magi.omlx" || true
    start_launch_agent "com.magi.omlx" "$plist" "$label"
}

wait_model_ready() {
    local port="$1"
    local keyword="$2"
    local timeout="${3:-90}"
    local waited=0
    local model_id=""
    while [ "$waited" -lt "$timeout" ]; do
        model_id=$(
            curl -sf --max-time 3 "http://127.0.0.1:${port}/v1/models" 2>/dev/null | \
            python3 -c 'import json,sys; data=json.load(sys.stdin); print(((data.get("data") or [{}])[0].get("id") or "").lower())' 2>/dev/null || true
        )
        if echo "$model_id" | grep -qi "$keyword"; then
            log "${port} OK (${model_id})"
            return 0
        fi
        sleep 5
        waited=$((waited + 5))
    done
    log "❌ ${port} model not ready or wrong after ${timeout}s (expected=${keyword}, actual=${model_id:-down})"
    return 1
}

get_active_profile() {
    cat "$PROFILE_FILE" 2>/dev/null || echo "unknown"
}

already_in_requested_mode() {
    local requested="$1"
    local active current_main current_phi4 current_smol
    active=$(get_active_profile)
    current_main=$(probe_model_id_at_port 8080)
    current_phi4=$(probe_model_id_at_port 8082)
    current_smol=$(probe_model_id_at_port 8083)
    if [ "$requested" = "day" ]; then
        if [ "$active" != "day" ] || ! echo "$current_main" | grep -qi "$DAY_PRIMARY_MODEL_KEYWORD"; then
            return 1
        fi
        if [ -d "${HOME}/.omlx/models/Phi-4-mini-instruct-4bit" ] && ! echo "$current_phi4" | grep -qi "phi"; then
            return 1
        fi
        if ls "${HOME}/.omlx/models/" 2>/dev/null | grep -q "SmolLM3" && ! echo "$current_smol" | grep -qi "smol"; then
            return 1
        fi
        log "DAY mode already healthy（api=$current_main, phi4=${current_phi4:-off}, smol=${current_smol:-off}），跳過重啟"
        return 0
    fi
    if [ "$requested" = "night" ]; then
        if [ "$active" != "night" ] || ! echo "$current_main" | grep -qi "26b"; then
            return 1
        fi
        if [ -n "$current_phi4" ] || [ -n "$current_smol" ]; then
            return 1
        fi
        log "NIGHT mode already healthy（api=${current_main}），跳過重啟"
        return 0
    fi
    return 1
}

if [ "${MAGI_OMLX_FORCE_SWITCH:-0}" != "1" ] && [ "$MODE" != "auto" ] && [ "$MODE" != "status" ]; then
    if already_in_requested_mode "$MODE"; then
        exit 0
    fi
fi

case "$MODE" in
  day)
    log "→ DAY mode (${DAY_PRIMARY_MODEL_KEYWORD} + Phi4 + SmolLM3)"
    DAY_MAIN_DEGRADED=0
    if [ ! -d "$DAY_PRIMARY_SRC" ]; then
        start_day_e4b_fallback "日間主模型目錄不存在: $DAY_PRIMARY_SRC"
    elif [ ! -x "$GEMMA4_UNIFIED_WRAPPER" ]; then
        start_day_e4b_fallback "Gemma4 unified wrapper 不存在或不可執行: $GEMMA4_UNIFIED_WRAPPER"
    fi

    if [ "$DAY_MAIN_DEGRADED" != "1" ]; then
        # 更新 models-text symlink → day primary（預設 E4B）
        rm -f "$MODELS_TEXT_DIR"/*
        ln -sf "$DAY_PRIMARY_SRC" "$MODELS_TEXT_DIR/$DAY_PRIMARY_LINK_NAME"

        # 更新 models-text-e4b symlink（讓日間 4B 與 fallback 入口一致）
        rm -f "${HOME}"/.omlx/models-text-e4b/*
        ln -sf "$E4B_SRC" "${HOME}/.omlx/models-text-e4b/gemma-4-e4b-it-4bit"

        preflight_oomlx_rss_check 8 "DAY"

        # 重啟 oMLX day primary（Gemma4 unified overlay）
        launchctl bootout "gui/$UID_NUM/com.magi.omlx" 2>/dev/null || true
        wait_launchctl_unloaded "com.magi.omlx" 12 || true
        clear_stale_8080_owner
        wait_port_closed 8080 15 || true
        # bootout 後才檢查記憶體（避免舊 process 佔用干擾判斷）
        if ! preflight_memory_check "$DAY_PRIMARY_MIN_FREE_GB" "DAY" return; then
            start_day_e4b_fallback "日間主模型記憶體不足"
        fi
    fi
    if [ "$DAY_MAIN_DEGRADED" != "1" ]; then
        configure_day_primary_runtime_env
        if ! bootstrap_omlx_main "DAY"; then
            start_day_e4b_fallback "日間主模型 launchd 啟動失敗"
        fi
    fi

    # 啟動 Phi-4 和 SmolLM3（若模型已下載）
    if [ -d "${HOME}/.omlx/models/Phi-4-mini-instruct-4bit" ]; then
        PHI4_CACHE_DIR=$(omlx_cache_dir cache-phi4 "${HOME}/.omlx/cache-phi4")
        plist_set_program_arg "$HOME/Library/LaunchAgents/com.magi.omlx-phi4.plist" 15 "$PHI4_CACHE_DIR"
        log "oMLX paged cache (Phi-4): $PHI4_CACHE_DIR"
        rm -f "${HOME}"/.omlx/models-text-phi4/*
        ln -sf "${HOME}/.omlx/models/Phi-4-mini-instruct-4bit" \
               "${HOME}/.omlx/models-text-phi4/Phi-4-mini-instruct-4bit"
        wait_port_closed 8082 10 || true
        if ! restart_launch_agent "com.magi.omlx-phi4" "$HOME/Library/LaunchAgents/com.magi.omlx-phi4.plist" "Phi-4"; then
            notify_admin "DAY 切換時 Phi-4 launchd 啟動失敗，請檢查 /opt/homebrew/var/log/omlx-phi4.log"
            exit 4
        fi
        log "Phi-4 啟動中..."
    else
        log "⚠️  Phi-4 模型尚未下載，跳過"
    fi

    if ls "${HOME}/.omlx/models/" | grep -q "SmolLM3"; then
        SMOL_MODEL=$(ls "${HOME}/.omlx/models/" | grep SmolLM3 | head -1)
        SMOL_CACHE_DIR=$(omlx_cache_dir cache-smol "${HOME}/.omlx/cache-smol")
        plist_set_program_arg "$HOME/Library/LaunchAgents/com.magi.omlx-smol.plist" 15 "$SMOL_CACHE_DIR"
        log "oMLX paged cache (SmolLM3): $SMOL_CACHE_DIR"
        rm -f "${HOME}"/.omlx/models-text-smol/*
        ln -sf "${HOME}/.omlx/models/$SMOL_MODEL" \
               "${HOME}/.omlx/models-text-smol/$SMOL_MODEL"
        wait_port_closed 8083 10 || true
        if ! restart_launch_agent "com.magi.omlx-smol" "$HOME/Library/LaunchAgents/com.magi.omlx-smol.plist" "SmolLM3"; then
            notify_admin "DAY 切換時 SmolLM3 launchd 啟動失敗，請檢查 /opt/homebrew/var/log/omlx-smol.log"
            exit 4
        fi
        log "SmolLM3 ($SMOL_MODEL) 啟動中..."
    else
        log "⚠️  SmolLM3 模型尚未下載，跳過"
    fi

    if [ "$DAY_MAIN_DEGRADED" != "1" ]; then
        # 等待服務啟動；主 8080 必須符合日間主模型，避免 active_profile=day 但模型錯置的假成功。
        if ! wait_model_ready 8080 "$DAY_PRIMARY_MODEL_KEYWORD" 180; then
            start_day_e4b_fallback "日間主模型未於時限內載入"
        fi
    fi
    if [ -d "${HOME}/.omlx/models/Phi-4-mini-instruct-4bit" ] && ! wait_model_ready 8082 "phi" 90; then
        notify_admin "DAY 切換後 8082 未載入 Phi-4，交叉驗證不可用"
        exit 4
    fi
    if ls "${HOME}/.omlx/models/" 2>/dev/null | grep -q "SmolLM3" && ! wait_model_ready 8083 "smol" 90; then
        notify_admin "DAY 切換後 8083 未載入 SmolLM3，交叉驗證不可用"
        exit 4
    fi
    if [ "$DAY_MAIN_DEGRADED" != "1" ]; then
        echo "day" > "$PROFILE_FILE"
        rm -f "$DAY_FALLBACK_STAMP_FILE"
        rm -f "$NIGHT_FALLBACK_STAMP_FILE"
    fi

    # heartbeat 背景執行，不阻塞腳本完成
    ( heartbeat_check 3 "DAY" ) &
    ;;

  night)
    log "→ NIGHT mode (26B primary; 12B fallback; E4B last-resort)"
    if [ ! -d "$B26_SRC" ] && [ -d "$B26_LEGACY_SRC" ]; then
        B26_SRC="$B26_LEGACY_SRC"
    fi
    check_model_src "$B26_SRC"

    if ! resource_guard_allows_night_26b; then
        if preserve_current_e4b_for_night "本機資源低水位，暫不啟動 26B/12B"; then
            ( heartbeat_check 1 "NIGHT-LAST-RESORT-E4B" ) &
            log "Switch to $MODE complete (active_profile=$(get_active_profile))"
            exit 0
        fi
        start_night_12b_fallback "本機資源低水位，暫不啟動 26B"
        ( heartbeat_check 1 "NIGHT-FALLBACK-12B" ) &
        log "Switch to $MODE complete (active_profile=$(get_active_profile))"
        exit 0
    fi

    # 停止 Phi-4 和 SmolLM3
    launchctl bootout "gui/$UID_NUM/com.magi.omlx-phi4" 2>/dev/null || true
    launchctl bootout "gui/$UID_NUM/com.magi.omlx-smol" 2>/dev/null || true
    wait_launchctl_unloaded "com.magi.omlx-phi4" 12 || true
    wait_launchctl_unloaded "com.magi.omlx-smol" 12 || true
    wait_port_closed 8082 15 || true
    wait_port_closed 8083 15 || true

    # 更新 models-text symlink → 26B
    rm -f "$MODELS_TEXT_DIR"/*
    ln -sf "$B26_SRC" "$MODELS_TEXT_DIR/gemma-4-26b-a4b-it-4bit"

    preflight_oomlx_rss_check 16 "NIGHT"

    # 重啟 oMLX 26B（模型實際約 14.63GB；MODEL 需高於模型大小，否則 completion 回 507）
    launchctl bootout "gui/$UID_NUM/com.magi.omlx" 2>/dev/null || true
    wait_launchctl_unloaded "com.magi.omlx" 12 || true
    clear_stale_8080_owner
    wait_port_closed 8080 30 || true
    log "等待記憶體回收（10s）..."
    sleep 10
    # 所有舊 process 都 bootout 後才檢查記憶體（門檻 8GB：26B ceiling=16GB，系統本身 6-8GB）
    if ! preflight_memory_check 8 "NIGHT" return; then
        start_night_12b_fallback "26B 記憶體不足"
        ( heartbeat_check 1 "NIGHT-FALLBACK-12B" ) &
        log "Switch to $MODE complete (active_profile=$(get_active_profile))"
        exit 0
    fi
    configure_night_runtime_env
    if ! bootstrap_omlx_main "NIGHT"; then
        start_night_12b_fallback "26B launchd 啟動失敗"
        ( heartbeat_check 1 "NIGHT-FALLBACK-12B" ) &
        log "Switch to $MODE complete (active_profile=$(get_active_profile))"
        exit 0
    fi

    if ! wait_model_ready 8080 "26b" 180; then
        start_night_12b_fallback "26B 未於時限內載入"
        ( heartbeat_check 1 "NIGHT-FALLBACK-12B" ) &
        log "Switch to $MODE complete (active_profile=$(get_active_profile))"
        exit 0
    fi
    echo "night" > "$PROFILE_FILE"
    rm -f "$NIGHT_FALLBACK_STAMP_FILE"

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

# A release certification may run the complete switch orchestration against
# fixture-owned launchctl/curl/model providers.  Persist the observed handoff
# before removing fixture symlinks so the generic Seatbelt harness can keep its
# no-symlink invariant.  Production executions never enter this branch.
if [ "${MAGI_V3_SCHEDULE_ADAPTER:-}" = "real_entrypoint_fixture_v1" ]; then
    fixture_root="${MAGI_V3_SCHEDULE_FIXTURE_ROOT:-}"
    trace_path="${MAGI_OMLX_SWITCH_PROVIDER_TRACE:-}"
    if [ "${MAGI_V3_SCHEDULE_DRY_RUN:-}" != "1" ] || \
       [ ! -f "$fixture_root/.magi-v3-schedule-fixture" ] || \
       [ -z "$trace_path" ] || \
       [[ "$trace_path" != "$fixture_root"/* ]] || \
       [[ "$HOME" != "$fixture_root"/* ]]; then
        echo "oMLX schedule fixture binding failed closed" >&2
        exit 78
    fi
    mkdir -p "$(dirname "$trace_path")"
    main_model=$(basename "$(readlink "$MODELS_TEXT_DIR"/* 2>/dev/null | head -1)" 2>/dev/null || true)
    phi_model=$(basename "$(readlink "${HOME}/.omlx/models-text-phi4"/* 2>/dev/null | head -1)" 2>/dev/null || true)
    smol_model=$(basename "$(readlink "${HOME}/.omlx/models-text-smol"/* 2>/dev/null | head -1)" 2>/dev/null || true)
    printf '{"requested_mode":"%s","resolved_mode":"%s","active_profile":"%s","main_model":"%s","phi_model":"%s","smol_model":"%s"}\n' \
        "${1:-day}" "$MODE" "$(get_active_profile)" "$main_model" "$phi_model" "$smol_model" > "$trace_path"
    find "${HOME}/.omlx" -type l -delete
fi
