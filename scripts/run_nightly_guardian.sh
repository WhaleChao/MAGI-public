#!/bin/bash
# MAGI 夜間守護：取代所有 crontab 排程
# 所有夜間任務由此腳本統一管理，不依賴 macOS cron
export PATH="/opt/homebrew/bin:$PATH"
MAGI_DIR="${MAGI_ROOT_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
cd "$MAGI_DIR"

VENV_PY="$MAGI_DIR/venv/bin/python3"
LOG_DIR="$MAGI_DIR/logs"
mkdir -p "$LOG_DIR"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG_DIR/nightly_guardian.log"
}

# 記錄上次執行日期，避免同一天重複跑
STATE_FILE="$MAGI_DIR/.nightly_guardian_state"

already_ran_today() {
    local task=$1
    local today=$(date +%Y-%m-%d)
    grep -q "${task}:${today}" "$STATE_FILE" 2>/dev/null
}

mark_done() {
    local task=$1
    local today=$(date +%Y-%m-%d)
    echo "${task}:${today}" >> "$STATE_FILE"
}

# 清理超過 7 天的狀態記錄
cleanup_state() {
    if [ -f "$STATE_FILE" ]; then
        local cutoff=$(date -v-7d +%Y-%m-%d 2>/dev/null || date -d '7 days ago' +%Y-%m-%d 2>/dev/null)
        if [ -n "$cutoff" ]; then
            grep -v ":[0-9]*-[0-9]*-[0-9]*$" "$STATE_FILE" > /dev/null 2>&1 || true
            # macOS: 保留最近 7 天
            tail -50 "$STATE_FILE" > "${STATE_FILE}.tmp" 2>/dev/null && mv "${STATE_FILE}.tmp" "$STATE_FILE"
        fi
    fi
}

# === 每小時任務 ===
run_hourly() {
    # DB sync（每小時）
    log "hourly: db_sync"
    MAGI_PREFER_LOCAL_DB=0 MAGI_NO_DELETE=1 $VENV_PY skills/ops/db_sync.py --task sync >> "$LOG_DIR/db_sync_cron.log" 2>&1 &
}

# === 夜間任務（22:00 觸發）===
run_nightly_22() {
    if already_ran_today "nightly_22"; then return; fi
    log "nightly_22: autopilot nightly 開始"
    MAGI_PREFER_LOCAL_DB=0 MAGI_NO_DELETE=1 timeout 21600 $VENV_PY skills/magi-autopilot/action.py --task nightly >> "$LOG_DIR/cron_nightly.log" 2>&1
    log "nightly_22: autopilot nightly 完成"
    mark_done "nightly_22"
}

# === 司法院 API 夜間拉取（00:30 觸發）===
run_judicial_pull() {
    if already_ran_today "judicial_pull"; then return; fi
    log "judicial_pull: 開始拉取"
    MAGI_NO_DELETE=1 JUDICIAL_API_ALLOW_INSECURE_SSL=1 \
    JUDICIAL_API_WINDOW_START_HOUR=0 JUDICIAL_API_WINDOW_END_HOUR=6 \
    timeout 5400 $VENV_PY skills/judgment-collector/action.py \
        --task 'official_api_night_pull {"max_jdocs":25000,"max_days":0,"force":false,"notify":true}' \
        >> "$LOG_DIR/cron_judicial_night_pull.log" 2>&1
    log "judicial_pull: 拉取完成，開始入庫"
    $VENV_PY scripts/ingest_raw_judgments.py >> "$LOG_DIR/cron_judicial_ingest.log" 2>&1
    log "judicial_pull: 入庫完成"
    mark_done "judicial_pull"
}

# === 夜間巡邏（03:00）===
run_night_patrol() {
    if already_ran_today "night_patrol"; then return; fi
    log "night_patrol: 開始"
    timeout 1800 $VENV_PY scripts/casper_night_patrol.py >> "$LOG_DIR/night_patrol.log" 2>&1
    log "night_patrol: 完成"
    mark_done "night_patrol"
}

# === 三哲人議會（03:30）===
run_nightly_council() {
    if already_ran_today "nightly_council"; then return; fi
    log "nightly_council: 開始"
    timeout 3600 $VENV_PY scripts/nightly_council.py >> "$LOG_DIR/nightly_council.log" 2>&1
    log "nightly_council: 完成"
    mark_done "nightly_council"
}

# === 晨報入庫（06:30）===
run_morning_ingest() {
    if already_ran_today "morning_ingest"; then return; fi
    log "morning_ingest: 開始"
    MAGI_PREFER_LOCAL_DB=0 MAGI_NO_DELETE=1 \
    $VENV_PY scripts/ingest_raw_judgments.py >> "$LOG_DIR/cron_judicial_ingest.log" 2>&1
    log "morning_ingest: 完成"
    mark_done "morning_ingest"
}

# === 法扶夜間巡檢（02:30）===
run_laf_audit() {
    if already_ran_today "laf_audit"; then return; fi
    log "laf_audit: 開始"
    timeout 1200 $VENV_PY scripts/laf_nightly_audit.py >> "$LOG_DIR/laf_nightly_audit.log" 2>&1
    log "laf_audit: 完成"
    mark_done "laf_audit"
}

# === PDF Namer 夜間訓練（23:00）===
run_pdf_namer_train() {
    if already_ran_today "pdf_namer_train"; then return; fi
    log "pdf_namer_train: 開始"
    PYTHONPATH="$MAGI_DIR" $VENV_PY "$MAGI_DIR/skills/pdf-namer/nightly_train.py" --max-files 200 >> "$LOG_DIR/pdf_namer_nightly.log" 2>&1
    log "pdf_namer_train: 完成"
    mark_done "pdf_namer_train"
}

# === 夜間健康報告（06:30）===
run_health_report() {
    if already_ran_today "health_report"; then return; fi
    log "health_report: 開始"
    timeout 600 $VENV_PY scripts/nightly_health_report.py >> "$LOG_DIR/nightly_health_report.log" 2>&1
    log "health_report: 完成"
    mark_done "health_report"
}

# === 週末見解庫回填 ===
run_weekend_backfill() {
    local dow=$(date +%u)  # 1=Mon, 6=Sat, 7=Sun
    if [ "$dow" != "6" ] && [ "$dow" != "7" ]; then return; fi
    local hour_key="weekend_backfill_$(date +%H)"
    if already_ran_today "$hour_key"; then return; fi
    log "weekend_backfill: 開始"
    $VENV_PY skills/judgment-collector/action.py \
        --task 'backfill_archive_summaries {"max_items":20}' \
        >> "$LOG_DIR/backfill_weekend.log" 2>&1
    log "weekend_backfill: 完成"
    mark_done "$hour_key"
}

# === PDF 更名學習器（背景常駐）===
start_rename_watcher() {
    if pgrep -f "rename_watcher.py" > /dev/null 2>&1; then return; fi
    log "rename_watcher: 啟動"
    PYTHONPATH="$MAGI_DIR" $VENV_PY "$MAGI_DIR/skills/pdf-namer/rename_watcher.py" >> "$LOG_DIR/rename_watcher.log" 2>&1 &
    log "rename_watcher: PID $!"
}

# === 主循環 ===
log "=== MAGI Nightly Guardian 啟動 ==="
cleanup_state
start_rename_watcher

LAST_HOURLY_HOUR=""

while true; do
    HOUR=$(date +%H)
    MINUTE=$(date +%M)

    # 每小時任務（整點觸發）
    if [ "$HOUR" != "$LAST_HOURLY_HOUR" ]; then
        run_hourly
        LAST_HOURLY_HOUR="$HOUR"
    fi

    # 時間觸發
    case "$HOUR" in
        22)
            [ "$MINUTE" -ge 0 ] && [ "$MINUTE" -lt 5 ] && run_nightly_22
            ;;
        23)
            [ "$MINUTE" -ge 0 ] && [ "$MINUTE" -lt 5 ] && run_pdf_namer_train
            ;;
        00)
            [ "$MINUTE" -ge 25 ] && [ "$MINUTE" -lt 35 ] && run_judicial_pull
            ;;
        02)
            [ "$MINUTE" -ge 25 ] && [ "$MINUTE" -lt 35 ] && run_laf_audit
            ;;
        03)
            [ "$MINUTE" -ge 0 ] && [ "$MINUTE" -lt 5 ] && run_night_patrol
            [ "$MINUTE" -ge 25 ] && [ "$MINUTE" -lt 35 ] && run_nightly_council
            ;;
        06)
            [ "$MINUTE" -ge 25 ] && [ "$MINUTE" -lt 35 ] && run_morning_ingest
            [ "$MINUTE" -ge 25 ] && [ "$MINUTE" -lt 35 ] && run_health_report
            ;;
        07|08|09|10|11|12|13|14|15|16|17|18|19|20|21)
            run_weekend_backfill
            ;;
    esac

    # 每天清理一次舊狀態
    [ "$HOUR" = "00" ] && [ "$MINUTE" -ge 0 ] && [ "$MINUTE" -lt 5 ] && cleanup_state

    # 每 60 秒檢查一次
    sleep 60
done
