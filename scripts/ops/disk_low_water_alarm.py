#!/usr/bin/env python3
"""
磁碟低水位 alarm（A2，2026-04-25）

每小時 cron 觸發，讀根分割區可用空間：
- ≥ 50 GB：靜默
- 15-50 GB：log_issue(High) 推 self_repair
- < 15 GB：log_issue(Critical) 推 self_repair + 同步試用 red_phone TG 推送

Dedup 由 issue_tracker 的 5 分鐘 TTL 內建處理；連續低水位每 5 分鐘最多一筆。

設計原則：永不 raise，永不阻塞。出問題自己 swallow。

使用：
    python3 scripts/ops/disk_low_water_alarm.py            # 一次檢查
    python3 scripts/ops/disk_low_water_alarm.py --threshold-warn 50  # 自訂閾值
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

MAGI_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(MAGI_ROOT))

HIGH_ALERT_COOLDOWN_SEC = int(os.environ.get("MAGI_DISK_LOW_WATER_HIGH_COOLDOWN_SEC", "21600"))
CRITICAL_ALERT_COOLDOWN_SEC = int(os.environ.get("MAGI_DISK_LOW_WATER_CRITICAL_COOLDOWN_SEC", "3600"))
ALERT_STATE_PATH = MAGI_ROOT / ".runtime" / "disk_low_water_alarm_state.json"


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
        f"MAGI 會先執行保守自動回收；若仍不足，請檢查 macOS swap、~/.omlx/cache-*、"
        f"以及 /opt/homebrew/var/mysql/magi_brain。"
    )
    try:
        from skills.management.issue_tracker import log_issue
        log_issue(
            # 2026-04-27：command 從 "cron:job_disk_low_water_alarm" 改為 "alarm:disk_low_water"。
            # script 自身 exit=0（設計原則：永不 raise），但主動 log alarm 給 issue tracker 紀錄。
            # 用 "cron:" 前綴會被 self-repair-reporter 誤判為 cron 故障；改 "alarm:" 前綴讓它
            # 仍進 issue agenda（律師關心磁碟空間），但不混進「持續性故障」週報。
            command="alarm:disk_low_water",
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


def _read_alert_state() -> dict:
    try:
        return json.loads(ALERT_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_alert_state(severity: str, free_gb: float, threshold_gb: float, *, emitted: bool) -> None:
    try:
        ALERT_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "ts": time.time(),
            "iso": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "severity": severity,
            "free_gb": free_gb,
            "threshold_gb": threshold_gb,
            "emitted": emitted,
        }
        tmp = ALERT_STATE_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(ALERT_STATE_PATH)
    except Exception:
        pass


def _should_emit_alert(severity: str, free_gb: float) -> bool:
    if severity not in {"High", "Critical"}:
        return False
    state = _read_alert_state()
    last_severity = str(state.get("severity") or "")
    if last_severity != severity:
        return True
    try:
        age = time.time() - float(state.get("ts") or 0)
        last_free = float(state.get("free_gb"))
    except Exception:
        return True
    if severity == "Critical":
        return age >= CRITICAL_ALERT_COOLDOWN_SEC or free_gb <= last_free - 1.0
    return age >= HIGH_ALERT_COOLDOWN_SEC or free_gb <= last_free - 2.0


def _auto_reclaim_enabled() -> bool:
    return os.environ.get("MAGI_DISK_LOW_WATER_AUTO_RECLAIM", "1").strip().lower() in {
        "1",
        "true",
        "on",
        "yes",
    }


def _run_auto_reclaim(path: str) -> dict:
    """Run guarded cleanup when low water is already confirmed.

    This intentionally delegates to disk_cleanup_healthcheck so the deletion
    rules stay centralized: no case folders, no NAS roots, no model/training
    roots, no standalone JSON state.
    """
    info = {
        "attempted": False,
        "enabled": _auto_reclaim_enabled(),
        "free_before_gb": get_disk_free_gb(path),
        "free_after_gb": None,
    }
    if not info["enabled"]:
        return info
    try:
        from scripts.ops import disk_cleanup_healthcheck

        old = os.environ.get("MAGI_DISK_CLEANUP_DRY_RUN")
        os.environ["MAGI_DISK_CLEANUP_DRY_RUN"] = "0"
        try:
            rc = disk_cleanup_healthcheck.main(["--apply"])
        finally:
            if old is None:
                os.environ.pop("MAGI_DISK_CLEANUP_DRY_RUN", None)
            else:
                os.environ["MAGI_DISK_CLEANUP_DRY_RUN"] = old
        info.update({"attempted": True, "rc": rc, "free_after_gb": get_disk_free_gb(path)})
    except Exception as e:
        info.update({"attempted": True, "error": str(e), "free_after_gb": get_disk_free_gb(path)})
    return info


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--path", default="/", help="檢查路徑（預設 /）")
    parser.add_argument(
        "--threshold-warn",
        type=float,
        default=50.0,
        help="High 警告閾值 GB（預設 50）",
    )
    parser.add_argument(
        "--threshold-critical",
        type=float,
        default=15.0,
        help="Critical 告警閾值 GB（預設 15）",
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
    elif free_gb < args.threshold_warn:
        severity = "High"
        triggered = True

    alert_emitted = False
    if triggered and _should_emit_alert(severity, free_gb):
        _push_self_repair(severity, free_gb, args.threshold_critical if severity == "Critical" else args.threshold_warn)
        alert_emitted = True
    if triggered:
        _write_alert_state(
            severity,
            free_gb,
            args.threshold_critical if severity == "Critical" else args.threshold_warn,
            emitted=alert_emitted,
        )

    auto_reclaim = _run_auto_reclaim(args.path) if triggered else {"attempted": False, "enabled": _auto_reclaim_enabled()}

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
        "alert_emitted": alert_emitted,
        "auto_reclaim": auto_reclaim,
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
