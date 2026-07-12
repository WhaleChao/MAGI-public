# -*- coding: utf-8 -*-
"""
Self-Repair Phase 2 — Nightly Issue Agenda Reporter.

Reads .runtime/issue_agenda.jsonl, groups failures by job/command + error
pattern, identifies persistent failures (≥3 occurrences in past 7 days),
and sends a Telegram summary to admin at 05:30 daily.

Iron Dome Audit: SAFE — reads .runtime/issue_agenda.jsonl (read-only),
writes .runtime/self_repair_last_report.json (own state only), sends via
red_phone (existing alert channel, PII-scrubbed before sending).
"""
from __future__ import annotations

import json as _json
import hashlib
import logging
import os
import re
import sys
import time
from urllib import request as _urlrequest
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv()
except Exception:
    pass

logger = logging.getLogger("SelfRepairReporter")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
_ENABLE = os.environ.get("MAGI_ISSUE_TRACKER_ENABLE", "0") == "1"
_LOOKBACK_DAYS = int(os.environ.get("MAGI_REPAIR_REPORTER_LOOKBACK_DAYS", "7"))
_PERSIST_THRESHOLD = int(os.environ.get("MAGI_REPAIR_REPORTER_PERSIST_THRESHOLD", "3"))
_REPORT_MAX_JOBS = int(os.environ.get("MAGI_REPAIR_REPORTER_MAX_JOBS", "15"))
_DRY_RUN = os.environ.get("MAGI_REPAIR_REPORTER_DRY_RUN", "0") == "1"
_STALE_HOURS = int(os.environ.get("MAGI_REPAIR_REPORTER_STALE_HOURS", "48") or "48")

try:
    from api.platforms.runtime_dir import root as _rt_root
    _AGENDA_PATH = _rt_root() / "issue_agenda.jsonl"
    _STATE_PATH = _rt_root() / "self_repair_last_report.json"
except Exception:
    _RUNTIME = Path(_PROJECT_ROOT) / ".runtime"
    _AGENDA_PATH = _RUNTIME / "issue_agenda.jsonl"
    _STATE_PATH = _RUNTIME / "self_repair_last_report.json"

_SKIP_SOURCES = frozenset({
    "opus.live_smoke",
    "disk_low_water_alarm",
})

# Error noise patterns to collapse (regex → label)
_ERROR_COLLAPSE: List[Tuple[re.Pattern, str]] = [
    (re.compile(r"timed?\s*out|timeout|TimeoutError", re.I), "Timeout"),
    (re.compile(r"connection\s+refused|ConnectionRefused|ECONNREFUSED", re.I), "ConnectionRefused"),
    (re.compile(r"No module named", re.I), "ModuleNotFound"),
    (re.compile(r"FileNotFoundError|No such file", re.I), "FileNotFound"),
    (re.compile(r"PermissionError|Access denied", re.I), "PermissionDenied"),
    (re.compile(r"OperationalError|MySQL|MariaDB|DB\s+error", re.I), "DBError"),
    (re.compile(r"SSL|certificate verify|CERTIFICATE_VERIFY_FAILED", re.I), "SSLError"),
    (re.compile(r"subprocess.*exit.*[1-9]\d*|returncode=[1-9]|non-zero exit", re.I), "SubprocessFailed"),
    (re.compile(r"OOM|out of memory|killed|SIGKILL", re.I), "OOM"),
    (re.compile(r"Exception|Error|Traceback", re.I), "GeneralError"),
]

_ERROR_DISPLAY_LABELS = {
    "Timeout": "逾時",
    "ConnectionRefused": "連線被拒",
    "ModuleNotFound": "缺少模組",
    "FileNotFound": "找不到檔案",
    "PermissionDenied": "權限不足",
    "DBError": "資料庫錯誤",
    "SSLError": "SSL 憑證錯誤",
    "SubprocessFailed": "子程序失敗",
    "OOM": "記憶體不足",
    "GeneralError": "一般錯誤",
    "Unknown": "未知錯誤",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _stdout_tail_payload(error_text: str) -> Dict[str, Any]:
    """Parse a JSON stdout_tail payload embedded in issue agenda errors."""
    marker = "stdout_tail="
    idx = str(error_text or "").find(marker)
    if idx < 0:
        return {}
    start = error_text.find("{", idx + len(marker))
    if start < 0:
        return {}
    try:
        payload, _end = _json.JSONDecoder().raw_decode(error_text[start:])
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _resource_governor_label(payload: Dict[str, Any]) -> str:
    reasons = [str(item) for item in (payload.get("reasons") or [])]
    if any(reason.startswith("disk_free") for reason in reasons):
        if any(reason.startswith(("swap_used", "free_plus_inactive")) for reason in reasons):
            return "資源壓力"
        return "磁碟空間不足"
    if any(reason.startswith(("swap_used", "free_plus_inactive")) for reason in reasons):
        return "記憶體壓力"
    if payload.get("level") in {"throttle", "core_only", "critical"}:
        return "資源壓力"
    return ""


def _resource_governor_detail(payload: Dict[str, Any]) -> str:
    snapshot = payload.get("snapshot") if isinstance(payload, dict) else {}
    if not isinstance(snapshot, dict):
        return ""
    parts = []
    swap = snapshot.get("swap_used_gb")
    free = snapshot.get("free_plus_inactive_gb")
    disk = snapshot.get("disk_free_gb")
    try:
        if swap is not None:
            parts.append(f"swap {float(swap):.2f}GB")
        if free is not None:
            parts.append(f"可用記憶體 {float(free):.2f}GB")
        if disk is not None:
            parts.append(f"磁碟 {float(disk):.2f}GB")
    except Exception:
        return ""
    return "、".join(parts[:3])


def _error_label(error_text: str) -> str:
    if not error_text:
        return "Unknown"
    payload = _stdout_tail_payload(error_text)
    resource_label = _resource_governor_label(payload)
    if resource_label:
        return resource_label
    snippet = error_text[:300]
    for pat, label in _ERROR_COLLAPSE:
        if pat.search(snippet):
            return label
    return "GeneralError"


def _error_detail(error_text: str) -> str:
    payload = _stdout_tail_payload(error_text)
    if _resource_governor_label(payload):
        return _resource_governor_detail(payload)
    return ""


def _display_error_label(label: str) -> str:
    return _ERROR_DISPLAY_LABELS.get(str(label or ""), str(label or "未知錯誤"))


def _job_label(command: str) -> str:
    """Shorten a cron command string to a recognisable job name."""
    if not command:
        return "unknown"
    # cron:job_xxx pattern
    m = re.search(r"cron:([a-z0-9_]+)", command, re.I)
    if m:
        return m.group(1)
    # python ... action.py --task xxx
    m2 = re.search(r"action\.py\s+--task\s+(\S+)", command)
    if m2:
        task = m2.group(1)
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", task):
            return task
        return "unknown"
    # Never publish an arbitrary command fragment: it can contain a case path,
    # query, token, or other operator-only context.
    return "unknown"


def _load_agenda(lookback_sec: float) -> List[Dict[str, Any]]:
    """Load records from issue_agenda.jsonl within the lookback window."""
    if not _AGENDA_PATH.exists():
        return []
    cutoff = time.time() - lookback_sec
    records = []
    try:
        with open(_AGENDA_PATH, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = _json.loads(line)
                    if float(rec.get("ts", 0)) < cutoff:
                        continue
                    if rec.get("source", "") in _SKIP_SOURCES:
                        continue
                    records.append(rec)
                except Exception:
                    continue
    except Exception as e:
        logger.warning("Failed to load issue_agenda: %s", e)
    return records


def _group_records(records: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Group records by (job_label, error_label), return dict keyed by group_key."""
    groups: Dict[str, Dict[str, Any]] = {}
    for rec in records:
        job = _job_label(rec.get("command", ""))
        error_text = rec.get("error", "") or ""
        err = _error_label(error_text)
        key = f"{job}|{err}"
        rec_ts = float(rec.get("ts", 0) or 0)
        if key not in groups:
            groups[key] = {
                "job": job,
                "error_label": err,
                "trace": hashlib.sha256(f"{job}|{err}".encode("utf-8")).hexdigest()[:12],
                "count": 0,
                "first_ts": rec_ts,
                "last_ts": rec_ts,
                "severity": rec.get("severity", "High"),
                "sample_error": error_text[:300],
                "last_detail": _error_detail(error_text),
                "days_seen": set(),
            }
        g = groups[key]
        g["count"] += 1
        if rec_ts >= float(g.get("last_ts") or 0):
            g["last_ts"] = rec_ts
            g["sample_error"] = error_text[:300]
            g["last_detail"] = _error_detail(error_text)
        g["first_ts"] = min(g["first_ts"], rec_ts)
        # Track which calendar dates this failure appeared on
        day = datetime.fromtimestamp(rec_ts, tz=timezone.utc).strftime("%Y-%m-%d")
        g["days_seen"].add(day)
    return groups


def _parse_ts(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value or "").strip()
    if not text:
        return 0.0
    try:
        return float(text)
    except Exception:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.strptime(text, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.now().astimezone().tzinfo)
            return dt.timestamp()
        except Exception:
            continue
    try:
        return datetime.fromisoformat(text).timestamp()
    except Exception:
        return 0.0


def _load_cron_last_run_ts() -> Dict[str, float]:
    """Return per-job last successful run timestamps.

    The function name is kept for compatibility with older tests/call sites.
    Recovery must be based on a successful completion, not merely a later
    dispatch or failed run.
    """
    state_path = _STATE_PATH.parent / "cron_state.json"
    try:
        data = _json.loads(state_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    out: Dict[str, float] = {}
    if not isinstance(data, dict):
        return out
    for job_id, value in data.items():
        if not isinstance(value, dict):
            continue
        ts = _parse_ts(value.get("last_success_at"))
        if not ts and value.get("last_success") is True:
            ts = _parse_ts(value.get("last_result_at")) or _parse_ts(value.get("last_run"))
        if not ts and "last_success_at" not in value and "last_success" not in value:
            ts = _parse_ts(value.get("last_run"))
        if ts:
            out[str(job_id)] = ts
    return out


def _load_cron_job_map() -> Dict[str, Dict[str, Any]]:
    cron_path = Path(_PROJECT_ROOT) / "cron_jobs.json"
    try:
        data = _json.loads(cron_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(data, list):
        return {}
    return {str(job.get("id") or ""): job for job in data if isinstance(job, dict) and job.get("id")}


def _guardian_unresolved_jobs() -> set[str]:
    """Return job ids still open in the newest guardian report.

    A later cron success proves only that one invocation completed.  It cannot
    clear a guardian finding until that finding is absent from the guardian's
    own latest result.
    """
    path = _STATE_PATH.parent / "magi_self_repair_guardian_latest.json"
    try:
        payload = _json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return set()
    if not isinstance(payload, dict):
        return set()

    unresolved_ids = payload.get("unresolved_issue_ids") or []
    if not unresolved_ids:
        unresolved_ids = [
            item.get("id")
            for item in (payload.get("issues") or [])
            if isinstance(item, dict)
            and str(item.get("status") or "") not in {"resolved", "ignored"}
            and str(item.get("severity") or "") != "info"
        ]
    joined = "\n".join(str(item or "") for item in unresolved_ids)
    return set(re.findall(r"\bjob_[A-Za-z0-9_\-]+\b", joined))


def _latest_operational_audit_is_green(issue_ts: float) -> bool:
    path = _STATE_PATH.parent / "operational_hardening_audit_latest.json"
    try:
        if not path.exists() or path.stat().st_mtime <= float(issue_ts or 0):
            return False
        data = _json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return False
    if not isinstance(data, dict):
        return False
    cron = data.get("cron") if isinstance(data.get("cron"), dict) else {}
    gmail = data.get("gmail_monitor") if isinstance(data.get("gmail_monitor"), dict) else {}
    if "parse_failure_count" not in cron or "collision_count" not in cron or "ok" not in gmail:
        return False
    try:
        cron_green = (
            int(cron.get("parse_failure_count")) == 0
            and int(cron.get("collision_count")) == 0
        )
    except Exception:
        cron_green = False
    gmail_green = gmail.get("ok") is True
    return cron_green and gmail_green


def _current_omlx_models(timeout_sec: float = 1.5) -> List[str]:
    try:
        with _urlrequest.urlopen("http://127.0.0.1:8080/v1/models", timeout=timeout_sec) as resp:
            payload = _json.loads(resp.read().decode("utf-8", errors="ignore"))
        return [str(item.get("id") or "") for item in payload.get("data") or [] if isinstance(item, dict)]
    except Exception:
        return []


def _annotate_group_status(groups: Dict[str, Dict[str, Any]], *, now_ts: Optional[float] = None) -> None:
    now_ts = float(now_ts or time.time())
    stale_sec = max(1, _STALE_HOURS) * 3600.0
    cron_last_run = _load_cron_last_run_ts()
    cron_jobs = _load_cron_job_map()
    current_omlx_models = _current_omlx_models()
    guardian_unresolved_jobs = _guardian_unresolved_jobs()
    for group in groups.values():
        job = str(group.get("job") or "")
        last_ts = float(group.get("last_ts") or 0)
        last_success_ts = cron_last_run.get(job, 0.0)
        sample_error = str(group.get("sample_error") or "")
        job_meta = cron_jobs.get(job) or {}
        if job == "job_omlx_switch_day" and any("e4b" in model.lower() for model in current_omlx_models):
            group["status"] = "recovered"
            group["status_reason"] = "目前 8080 已是日間 E4B"
        elif job == "job_omlx_switch_night" and any("26b" in model.lower() for model in current_omlx_models):
            group["status"] = "recovered"
            group["status_reason"] = "目前 8080 已是夜間 26B"
        elif job_meta and job_meta.get("enabled") is False:
            group["status"] = "recovered"
            group["status_reason"] = "工作已停用，不再排程"
        elif job == "job_operational_hardening_audit" and _latest_operational_audit_is_green(last_ts):
            group["status"] = "recovered"
            group["status_reason"] = "最新營運硬化健檢已轉綠"
        elif (
            job == "job_nightly_autopilot"
            and int(job_meta.get("timeout_sec") or 0) >= 28800
            and ("judicial_api_night_thread" in sample_error or "exit=-9" in sample_error)
        ):
            group["status"] = "recovered"
            group["status_reason"] = "nightly timeout 已調整為 8 小時"
        elif (
            job == "job_weekend_bookmark"
            and int(job_meta.get("timeout_sec") or 0) >= 21600
            and ("exit=-15" in sample_error or "timeout" in sample_error.lower())
        ):
            group["status"] = "recovered"
            group["status_reason"] = "週末書籤 timeout 已調整為 6 小時"
        elif last_success_ts > last_ts + 30 and job not in guardian_unresolved_jobs:
            group["status"] = "recovered"
            group["status_reason"] = "cron 後續已再執行"
        elif last_success_ts > last_ts + 30 and job in guardian_unresolved_jobs:
            group["status"] = "active"
            group["status_reason"] = "guardian 仍有未解問題，後續 cron 成功不足以結案"
        elif last_ts and (now_ts - last_ts) > stale_sec:
            group["status"] = "stale"
            group["status_reason"] = f"超過 {_STALE_HOURS} 小時未復發"
        else:
            group["status"] = "active"
            group["status_reason"] = "仍在觀察期"


def _is_persistent(group: Dict[str, Any]) -> bool:
    """True if this job has failed on ≥ PERSIST_THRESHOLD distinct calendar days."""
    return len(group["days_seen"]) >= _PERSIST_THRESHOLD


def _fmt_ts(ts: float) -> str:
    if not ts:
        return "N/A"
    try:
        from zoneinfo import ZoneInfo

        tz = ZoneInfo("Asia/Taipei")
    except Exception:
        tz = datetime.now().astimezone().tzinfo
    return datetime.fromtimestamp(ts, tz=tz).strftime("%m/%d %H:%M 台灣時間")


def _group_detail_suffix(group: Dict[str, Any]) -> str:
    detail = str(group.get("last_detail") or "").strip()
    return f"（{detail}）" if detail else ""


def _build_report(groups: Dict[str, Dict[str, Any]]) -> str:
    """Build the external report from allowlisted, non-sensitive fields only."""
    active_groups = [g for g in groups.values() if g.get("status", "active") == "active"]
    active_groups.sort(key=lambda g: (-int(g.get("count") or 0), str(g.get("job") or "")))
    lines: List[str] = [f"MAGI 自我修復狀態：待處理 {len(active_groups)} 項"]
    for group in active_groups[:_REPORT_MAX_JOBS]:
        lines.append(
            "• {job}：{error}（追蹤碼：{trace}）".format(
                job=str(group.get("job") or "unknown"),
                error=_display_error_label(str(group.get("error_label") or "Unknown")),
                trace=str(group.get("trace") or "unknown"),
            )
        )
    if not active_groups:
        lines.append("目前沒有待處理問題。")
    return "\n".join(lines)


def _redact_external_text(text: str) -> str:
    """Defence in depth before a report leaves the local process."""
    redacted = str(text or "")
    patterns = (
        (r"(?i)(?:token|api[_-]?key|password|secret)=[^\s&]+", "<REDACTED_SECRET>"),
        (r"[?&][A-Za-z0-9_.-]+=[^\s&]+", "<REDACTED_QUERY>"),
        (r"/(?:Users|Volumes|private|var|tmp)/[^\s]+", "<REDACTED_PATH>"),
        (r"\b\d{3,4}[-_]?\d{3,}[-_A-Za-z0-9]*\b", "<REDACTED_CASE>"),
    )
    for pattern, replacement in patterns:
        redacted = re.sub(pattern, replacement, redacted)
    return redacted


def _load_state() -> Dict[str, Any]:
    try:
        if _STATE_PATH.exists():
            return _json.loads(_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save_state(state: Dict[str, Any]) -> None:
    try:
        _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _STATE_PATH.with_suffix(".json.tmp")
        tmp.write_text(_json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(_STATE_PATH)
    except Exception as e:
        logger.warning("Failed to save state: %s", e)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_report(*, dry_run: bool = False, force: bool = False) -> Dict[str, Any]:
    """
    Generate and send the nightly self-repair report.

    Returns a result dict with: success, sent, groups_count, persistent_count,
    total_failures, dry_run, report_text.
    """
    dry_run = dry_run or _DRY_RUN

    if not _ENABLE and not force:
        return {"success": True, "sent": False, "reason": "MAGI_ISSUE_TRACKER_ENABLE=0"}

    lookback_sec = _LOOKBACK_DAYS * 86400.0
    records = _load_agenda(lookback_sec)

    groups = _group_records(records)
    _annotate_group_status(groups)
    # Convert days_seen sets to counts for serialisation
    for g in groups.values():
        g["days_seen_count"] = len(g["days_seen"])
        g["days_seen"] = sorted(g["days_seen"])

    active_groups = [g for g in groups.values() if g.get("status", "active") == "active"]
    persistent_count = sum(1 for g in active_groups if g["days_seen_count"] >= _PERSIST_THRESHOLD)
    total_failures = sum(g["count"] for g in groups.values())

    report_text = _redact_external_text(_build_report(
        # Re-add days_seen as a set for _build_report
        {k: {**g, "days_seen": set(g["days_seen"])} for k, g in groups.items()}
    ))

    result = {
        "success": True,
        "sent": False,
        "groups_count": len(groups),
        "persistent_count": persistent_count,
        "total_failures": total_failures,
        "active_groups_count": len(active_groups),
        "recovered_groups_count": sum(1 for g in groups.values() if g.get("status") == "recovered"),
        "stale_groups_count": sum(1 for g in groups.values() if g.get("status") == "stale"),
        "lookback_days": _LOOKBACK_DAYS,
        "dry_run": dry_run,
        "report_text": report_text,
        "ts": time.time(),
    }

    if dry_run:
        print(report_text)
        return result

    # --- Send via red_phone ---
    try:
        from skills.ops.red_phone import alert_admin
        sent = alert_admin(
            report_text,
            severity="info",
            source="self_repair_reporter",
            topic_key="self_repair",
        )
        result["sent"] = bool(sent)
    except Exception as e:
        logger.error("Failed to send report via red_phone: %s", e)
        result["sent"] = False
        result["send_error"] = _redact_external_text(str(e))

    _save_state(result)
    return result


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    parser = argparse.ArgumentParser(description="MAGI Self-Repair Phase 2 Reporter")
    parser.add_argument("--dry-run", action="store_true", help="Print report, do not send")
    parser.add_argument("--force", action="store_true", help="Run even if tracker is disabled")
    global _LOOKBACK_DAYS
    parser.add_argument("--lookback-days", type=int, default=_LOOKBACK_DAYS)
    args = parser.parse_args()

    _LOOKBACK_DAYS = args.lookback_days

    result = run_report(dry_run=args.dry_run, force=args.force)
    print(_json.dumps(
        {k: v for k, v in result.items() if k != "report_text"},
        ensure_ascii=False,
        indent=2,
    ))


if __name__ == "__main__":
    main()
