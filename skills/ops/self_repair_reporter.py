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
import logging
import os
import re
import sys
import time
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _error_label(error_text: str) -> str:
    if not error_text:
        return "Unknown"
    snippet = error_text[:300]
    for pat, label in _ERROR_COLLAPSE:
        if pat.search(snippet):
            return label
    return "GeneralError"


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
        return m2.group(1)
    # Truncate to 60 chars
    return command[:60].strip()


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
        err = _error_label(rec.get("error", ""))
        key = f"{job}|{err}"
        if key not in groups:
            groups[key] = {
                "job": job,
                "error_label": err,
                "count": 0,
                "first_ts": rec.get("ts", 0),
                "last_ts": rec.get("ts", 0),
                "severity": rec.get("severity", "High"),
                "sample_error": (rec.get("error", "") or "")[:300],
                "days_seen": set(),
            }
        g = groups[key]
        g["count"] += 1
        g["last_ts"] = max(g["last_ts"], rec.get("ts", 0))
        g["first_ts"] = min(g["first_ts"], rec.get("ts", 0))
        # Track which calendar dates this failure appeared on
        day = datetime.fromtimestamp(rec.get("ts", 0), tz=timezone.utc).strftime("%Y-%m-%d")
        g["days_seen"].add(day)
    return groups


def _is_persistent(group: Dict[str, Any]) -> bool:
    """True if this job has failed on ≥ PERSIST_THRESHOLD distinct calendar days."""
    return len(group["days_seen"]) >= _PERSIST_THRESHOLD


def _fmt_ts(ts: float) -> str:
    if not ts:
        return "N/A"
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%m/%d %H:%M UTC")


def _build_report(groups: Dict[str, Dict[str, Any]]) -> str:
    """Build human-readable Telegram report text."""
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    lookback = _LOOKBACK_DAYS

    persistent = [g for g in groups.values() if _is_persistent(g)]
    recurring = [g for g in groups.values() if not _is_persistent(g) and g["count"] >= 2]
    one_off = [g for g in groups.values() if g["count"] == 1]

    persistent.sort(key=lambda g: -g["count"])
    recurring.sort(key=lambda g: -g["count"])

    lines: List[str] = [
        f"🔧 MAGI 自我修復週報 ({now_str})",
        f"過去 {lookback} 天 issue agenda 分析",
        "",
    ]

    total_records = sum(g["count"] for g in groups.values())
    lines.append(f"📊 總計：{total_records} 筆失敗 / {len(groups)} 種問題 / {len(persistent)} 個持續性故障")
    lines.append("")

    # --- Persistent failures ---
    if persistent:
        lines.append("🚨 持續性故障（需人工介入）")
        for g in persistent[:_REPORT_MAX_JOBS]:
            days_str = f"{len(g['days_seen'])} 天"
            lines.append(
                f"  • {g['job']}"
                f" — {g['error_label']} × {g['count']} 次 ({days_str})"
                f"，最後：{_fmt_ts(g['last_ts'])}"
            )
        if len(persistent) > _REPORT_MAX_JOBS:
            lines.append(f"  …另 {len(persistent) - _REPORT_MAX_JOBS} 個持續性問題略")
        lines.append("")

    # --- Recurring (2+ times but not persistent) ---
    if recurring:
        lines.append("⚠️ 重複失敗（觀察中）")
        for g in recurring[:8]:
            lines.append(
                f"  • {g['job']}"
                f" — {g['error_label']} × {g['count']} 次"
                f"，最後：{_fmt_ts(g['last_ts'])}"
            )
        if len(recurring) > 8:
            lines.append(f"  …另 {len(recurring) - 8} 個重複問題略")
        lines.append("")

    # --- One-off ---
    if one_off:
        lines.append(f"ℹ️ 單次失敗：{len(one_off)} 個（略）")
        lines.append("")

    # --- Sample errors for persistent ---
    if persistent:
        lines.append("🔍 持續性故障樣本錯誤")
        for g in persistent[:3]:
            sample = g["sample_error"][:150].replace("\n", " ").strip()
            lines.append(f"  [{g['job']}] {sample}")
        lines.append("")

    # --- Recommendation ---
    if persistent:
        lines.append("📋 建議行動")
        for g in persistent[:5]:
            job = g["job"]
            err = g["error_label"]
            if err == "Timeout":
                action = f"增加 {job} 的 timeout 或改善網路/NAS 穩定性"
            elif err == "ModuleNotFound":
                action = f"重裝 {job} 的依賴，或確認 skill 路徑正確"
            elif err == "DBError":
                action = f"確認 MariaDB 連線與 {job} 的 DB 憑證"
            elif err == "SubprocessFailed":
                action = f"手動跑 {job}，查 stderr 確認 exit code"
            elif err == "OOM":
                action = f"{job} OOM — 考慮降低批次量或增加記憶體配置"
            else:
                action = f"查 .runtime/issue_agenda.jsonl 找 {job} 詳細 traceback"
            lines.append(f"  → {job}：{action}")
        lines.append("")

    if not groups:
        lines.append("✅ 過去 7 天無任何失敗記錄！")

    return "\n".join(lines)


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
    # Convert days_seen sets to counts for serialisation
    for g in groups.values():
        g["days_seen_count"] = len(g["days_seen"])
        g["days_seen"] = sorted(g["days_seen"])

    persistent_count = sum(1 for g in groups.values() if g["days_seen_count"] >= _PERSIST_THRESHOLD)
    total_failures = sum(g["count"] for g in groups.values())

    report_text = _build_report(
        # Re-add days_seen as a set for _build_report
        {k: {**g, "days_seen": set(g["days_seen"])} for k, g in groups.items()}
    )

    result = {
        "success": True,
        "sent": False,
        "groups_count": len(groups),
        "persistent_count": persistent_count,
        "total_failures": total_failures,
        "lookback_days": _LOOKBACK_DAYS,
        "dry_run": dry_run,
        "report_text": report_text,
        "ts": time.time(),
    }

    if dry_run:
        print(report_text)
        _save_state(result)
        return result

    # --- Send via red_phone ---
    try:
        from skills.ops.red_phone import alert_admin
        sent = alert_admin(
            report_text,
            severity="info",
            source="self_repair_reporter",
            topic_key="general",
        )
        result["sent"] = bool(sent)
    except Exception as e:
        logger.error("Failed to send report via red_phone: %s", e)
        result["sent"] = False
        result["send_error"] = str(e)

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
