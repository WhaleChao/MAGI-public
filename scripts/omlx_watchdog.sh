#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MAGI_ROOT="${MAGI_ROOT_DIR:-$(cd "${SCRIPT_DIR}/.." && pwd)}"
MAGI_RUNTIME_DIR="${MAGI_RUNTIME_DIR:-${HOME}/Library/Application Support/MAGI}"
LOG_DIR="${MAGI_OMLX_WATCHDOG_LOG_DIR:-${MAGI_RUNTIME_DIR}/logs}"
STATE_DIR="${MAGI_OMLX_WATCHDOG_STATE_DIR:-${MAGI_RUNTIME_DIR}}"

mkdir -p "${LOG_DIR}" "${STATE_DIR}"

LOG_FILE="${MAGI_OMLX_WATCHDOG_LOG:-${LOG_DIR}/omlx_watchdog.log}"
STATE_FILE="${MAGI_OMLX_WATCHDOG_STATE_PATH:-${STATE_DIR}/omlx_watchdog_state.json}"
LOCK_FILE="${MAGI_OMLX_WATCHDOG_LOCK:-${STATE_DIR}/omlx_watchdog.pid}"

OMLX_HOST="${MAGI_OMLX_CHAT_HOST:-${MAGI_OMLX_HOST:-127.0.0.1}}"
OMLX_PORT="${MAGI_OMLX_CHAT_PORT:-${MAGI_OMLX_PORT:-8080}}"
OMLX_URL="${MAGI_OMLX_WATCHDOG_URL:-http://${OMLX_HOST}:${OMLX_PORT}}"
OMLX_LABEL="${MAGI_OMLX_LAUNCHD_LABEL:-com.magi.omlx}"

# Training lock: distill training 寫入此檔時 watchdog 暫停 oMLX 監控
TRAINING_LOCK="${MAGI_TRAINING_LOCK_PATH:-${MAGI_ROOT}/static/training.lock}"
TRAINING_LOCK_MAX_AGE=21600  # 6 hours — stale lock 自動忽略
LAUNCHD_TARGET="gui/$(id -u)/${OMLX_LABEL}"
PROCESS_PATTERN="${MAGI_OMLX_WATCHDOG_PROCESS_PATTERN:-omlx serve.*--port ${OMLX_PORT}}"

# Embed server (ModernBERT) 監控
EMBED_PORT="${MAGI_OMLX_EMBED_PORT:-8081}"
EMBED_URL="http://${OMLX_HOST}:${EMBED_PORT}"
EMBED_LABEL="${MAGI_OMLX_EMBED_LABEL:-com.magi.omlx-embed}"
EMBED_LAUNCHD_TARGET="gui/$(id -u)/${EMBED_LABEL}"
EMBED_PROCESS_PATTERN="omlx serve.*--port ${EMBED_PORT}"
embed_fail_count=0
EMBED_FAIL_THRESHOLD=2  # embed 更寬鬆：2 次失敗就重啟

CHECK_INTERVAL="${MAGI_OMLX_WATCHDOG_INTERVAL_SEC:-90}"
PROBE_TIMEOUT="${MAGI_OMLX_WATCHDOG_TIMEOUT_SEC:-30}"
CONNECT_TIMEOUT="${MAGI_OMLX_WATCHDOG_CONNECT_TIMEOUT_SEC:-3}"
FAIL_THRESHOLD="${MAGI_OMLX_WATCHDOG_FAIL_THRESHOLD:-3}"
COOLDOWN_SEC="${MAGI_OMLX_WATCHDOG_COOLDOWN_SEC:-150}"
RESTART_GRACE_SEC="${MAGI_OMLX_WATCHDOG_RESTART_GRACE_SEC:-25}"
# PROBE_MODEL kept for state JSON backward compatibility (no longer used for inference)
PROBE_MODEL="${MAGI_OMLX_WATCHDOG_MODEL:-${MAGI_OMLX_GENERAL_MODEL:-gemma-4-26b-a4b-it-4bit}}"

fail_count=0

log() {
    printf '%s [watchdog] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >>"${LOG_FILE}"
}

write_state() {
    local status="$1"
    local suspend_until="$2"
    local reason="${3:-}"
    local failure_count="${4:-$fail_count}"

    python3 - "$STATE_FILE" "$status" "$suspend_until" "$reason" "$failure_count" "$OMLX_URL" "$PROBE_MODEL" "$$" <<'PY'
import json
import os
import sys
import time
from pathlib import Path

state_path = Path(sys.argv[1])
payload = {
    "status": sys.argv[2],
    "suspend_until": float(sys.argv[3] or 0),
    "reason": sys.argv[4],
    "fail_count": int(sys.argv[5] or 0),
    "omlx_url": sys.argv[6],
    "probe_model": sys.argv[7],
    "updated_at": time.time(),
    "pid": int(sys.argv[8] or 0) or os.getpid(),
}
state_path.parent.mkdir(parents=True, exist_ok=True)
state_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY
}

clear_state() {
    write_state "healthy" "0" "probe_ok" "0"
}

acquire_lock() {
    if [ -f "${LOCK_FILE}" ]; then
        local existing_pid
        existing_pid="$(cat "${LOCK_FILE}" 2>/dev/null || true)"
        if [ -n "${existing_pid}" ] && kill -0 "${existing_pid}" 2>/dev/null; then
            log "another watchdog instance is already running (pid=${existing_pid})"
            exit 0
        fi
    fi
    printf '%s\n' "$$" >"${LOCK_FILE}"
}

cleanup() {
    if [ -f "${LOCK_FILE}" ] && [ "$(cat "${LOCK_FILE}" 2>/dev/null || true)" = "$$" ]; then
        rm -f "${LOCK_FILE}"
    fi
}

trap cleanup EXIT INT TERM

probe_inference() {
    # Use GET /v1/models instead of POST /v1/chat/completions to avoid
    # occupying the inference slot (max_num_seqs=1 means any inference probe
    # blocks real user requests).
    local response
    response="$(
        curl --silent --show-error \
            --connect-timeout "${CONNECT_TIMEOUT}" \
            --max-time 5 \
            "${OMLX_URL}/v1/models" 2>&1
    )" || {
        log "probe /v1/models failed: ${response:-curl_exit}"
        return 1
    }

    python3 -c '
import json, sys
data = json.load(sys.stdin)
models = data.get("data") or []
if not models or not isinstance(models, list):
    raise SystemExit(1)
' <<<"${response}" >/dev/null 2>&1
}

is_training_locked() {
    [ ! -f "${TRAINING_LOCK}" ] && return 1
    local lock_age
    lock_age=$(( $(date +%s) - $(stat -f %m "${TRAINING_LOCK}" 2>/dev/null || echo 0) ))
    if [ "${lock_age}" -gt "${TRAINING_LOCK_MAX_AGE}" ]; then
        log "stale training lock (age=${lock_age}s) — ignoring"
        rm -f "${TRAINING_LOCK}"
        return 1
    fi
    return 0
}

process_running() {
    pgrep -f "${PROCESS_PATTERN}" >/dev/null 2>&1
}

kickstart_omlx() {
    if launchctl print "${LAUNCHD_TARGET}" >/dev/null 2>&1; then
        launchctl kickstart -k "${LAUNCHD_TARGET}" >/dev/null 2>&1 && return 0
    fi
    return 1
}

restart_omlx() {
    local reason="${1:-probe_failed}"
    local now
    local suspend_until

    now="$(date +%s)"
    suspend_until="$((now + COOLDOWN_SEC + RESTART_GRACE_SEC))"
    write_state "restarting" "${suspend_until}" "${reason}" "${fail_count}"
    log "restarting oMLX after ${fail_count} consecutive failed probes (${reason})"

    if ! kickstart_omlx; then
        log "launchctl kickstart failed; falling back to SIGKILL"
        if process_running; then
            pkill -9 -f "${PROCESS_PATTERN}" >/dev/null 2>&1 || true
        fi
    fi

    local deadline=$(( $(date +%s) + RESTART_GRACE_SEC ))
    while [ "$(date +%s)" -lt "${deadline}" ]; do
        if curl --silent --connect-timeout 2 --max-time 4 "${OMLX_URL}/health" >/dev/null 2>&1; then
            break
        fi
        sleep 1
    done

    fail_count=0
    write_state "cooldown" "$(( $(date +%s) + COOLDOWN_SEC ))" "${reason}" "0"
    log "cooldown for ${COOLDOWN_SEC}s"
    sleep "${COOLDOWN_SEC}"
}

run_once() {
    if ! process_running; then
        write_state "missing_process" "0" "process_missing" "${fail_count}"
        log "oMLX process is not running"
        return 1
    fi

    if probe_inference; then
        clear_state
        return 0
    fi

    write_state "probe_failed" "0" "probe_failed" "${fail_count}"
    return 1
}

main_loop() {
    acquire_lock
    log "watchdog started (interval=${CHECK_INTERVAL}s timeout=${PROBE_TIMEOUT}s threshold=${FAIL_THRESHOLD} model=${PROBE_MODEL})"

    while true; do
        # Training lock: 訓練進行中暫停監控，避免與 distill training 互打
        if is_training_locked; then
            fail_count=0
            write_state "suspended" "0" "training_lock" "0"
            log "training lock active — suspending oMLX monitoring"
            sleep "${CHECK_INTERVAL}"
            continue
        fi

        if ! process_running; then
            fail_count=0
            log "oMLX process missing; requesting launchd restart"
            restart_omlx "process_missing"
            continue
        fi

        if probe_inference; then
            if [ "${fail_count}" -gt 0 ]; then
                log "probe recovered after ${fail_count} failure(s)"
            fi
            fail_count=0
            clear_state
        else
            fail_count=$((fail_count + 1))
            log "probe failed (${fail_count}/${FAIL_THRESHOLD})"
            write_state "probe_failed" "0" "probe_failed" "${fail_count}"
            if [ "${fail_count}" -ge "${FAIL_THRESHOLD}" ]; then
                restart_omlx "probe_timeout_or_invalid_response"
                continue
            fi
        fi

        # ── Embed server (8081) 監控 ──
        if pgrep -f "${EMBED_PROCESS_PATTERN}" >/dev/null 2>&1; then
            if curl --silent --connect-timeout 2 --max-time 4 "${EMBED_URL}/v1/models" >/dev/null 2>&1; then
                embed_fail_count=0
            else
                embed_fail_count=$((embed_fail_count + 1))
                log "embed probe failed (${embed_fail_count}/${EMBED_FAIL_THRESHOLD})"
                if [ "${embed_fail_count}" -ge "${EMBED_FAIL_THRESHOLD}" ]; then
                    log "restarting embed server via launchctl kickstart"
                    launchctl kickstart -k "${EMBED_LAUNCHD_TARGET}" >/dev/null 2>&1 || \
                        log "embed kickstart failed"
                    embed_fail_count=0
                    sleep 30  # 等 embed 重啟
                fi
            fi
        else
            log "embed process missing; kickstarting"
            launchctl kickstart -k "${EMBED_LAUNCHD_TARGET}" >/dev/null 2>&1 || \
                log "embed kickstart failed"
            embed_fail_count=0
            sleep 30
        fi

        sleep "${CHECK_INTERVAL}"
    done
}

case "${1:-}" in
    --once)
        if run_once; then
            exit 0
        fi
        exit 1
        ;;
    "")
        main_loop
        ;;
    *)
        echo "Usage: $0 [--once]" >&2
        exit 2
        ;;
esac
