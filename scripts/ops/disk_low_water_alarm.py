#!/usr/bin/env python3
"""
磁碟低水位 alarm（A2，2026-04-25）

每小時 cron 觸發，讀根分割區可用空間：
- ≥ 30 GB：靜默
- 10-30 GB：log_issue(High) 推 self_repair
- < 10 GB：log_issue(Critical) 推 self_repair + 同步試用 red_phone TG 推送

Dedup 由 issue_tracker 的 5 分鐘 TTL 內建處理；連續低水位每 5 分鐘最多一筆。

設計原則：永不 raise，永不阻塞。出問題自己 swallow。

使用：
    python3 scripts/ops/disk_low_water_alarm.py            # 一次檢查
    python3 scripts/ops/disk_low_water_alarm.py --threshold-warn 50  # 自訂閾值
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

MAGI_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(MAGI_ROOT))


def get_disk_free_gb(path: str = "/") -> float:
    """回傳指定路徑可用空間（GB）。失敗回 -1.0。"""
    try:
        usage = shutil.disk_usage(path)
        return round(usage.free / 1024 / 1024 / 1024, 2)
    except OSError:
        return -1.0


def _push_self_repair(severity: str, free_gb: float, threshold_gb: float) -> None:
    """寫 issue agenda；critical 時也試 TG 推送。"""
    msg = (
        f"磁碟低水位告警：可用空間 {free_gb} GB（閾值 {threshold_gb} GB）。"
        f"建議檢查 ~/.omlx-vision/cache/、~/.cache/huggingface/hub/，"
        f"或執行 scripts/ops/weekly_cache_cleanup.py。"
    )
    try:
        from skills.management.issue_tracker import log_issue
        log_issue(
            command="cron:job_disk_low_water_alarm",
            error_msg=msg,
            context={
                "free_gb": free_gb,
                "threshold_gb": threshold_gb,
                "severity": severity,
            },
            severity=severity,
            source="disk_low_water_alarm",
        )
    except Exception:
        pass

    if severity == "Critical":
        # 額外走 red_phone TG 推送，繞開 dedup（critical 必達）
        try:
            from skills.ops.red_phone import send_telegram_push_with_status
            send_telegram_push_with_status(
                message=f"🚨 [MAGI] {msg}",
                topic_key="self_repair",
                source="disk_low_water_alarm",
            )
        except Exception:
            pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--path", default="/", help="檢查路徑（預設 /）")
    parser.add_argument(
        "--threshold-warn",
        type=float,
        default=30.0,
        help="High 警告閾值 GB（預設 30）",
    )
    parser.add_argument(
        "--threshold-critical",
        type=float,
        default=10.0,
        help="Critical 告警閾值 GB（預設 10）",
    )
    args = parser.parse_args()

    free_gb = get_disk_free_gb(args.path)
    severity = "OK"
    triggered = False

    if free_gb < 0:
        severity = "Unknown"
    elif free_gb < args.threshold_critical:
        severity = "Critical"
        triggered = True
        _push_self_repair("Critical", free_gb, args.threshold_critical)
    elif free_gb < args.threshold_warn:
        severity = "High"
        triggered = True
        _push_self_repair("High", free_gb, args.threshold_warn)

    result = {
        "success": True,
        "ts": time.time(),
        "iso": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "path": args.path,
        "free_gb": free_gb,
        "threshold_warn_gb": args.threshold_warn,
        "threshold_critical_gb": args.threshold_critical,
        "severity": severity,
        "alarm_triggered": triggered,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        _rc = main()
    except Exception:
        _rc = 0  # 設計原則：永不 raise，永不阻塞
    sys.stdout.flush()
    sys.stderr.flush()
    sys.exit(int(_rc) if isinstance(_rc, int) else 0)
