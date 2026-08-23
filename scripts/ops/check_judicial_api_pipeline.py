#!/usr/bin/env python3
import argparse
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

MAGI_ROOT = Path(__file__).resolve().parents[2]
if str(MAGI_ROOT) in sys.path:
    sys.path.remove(str(MAGI_ROOT))
sys.path.insert(0, str(MAGI_ROOT))
for _module_name, _module in list(sys.modules.items()):
    if _module_name == "api" or _module_name.startswith("api."):
        _module_file = str(getattr(_module, "__file__", "") or "")
        if _module_file and not _module_file.startswith(str(MAGI_ROOT)):
            sys.modules.pop(_module_name, None)

from api.domains.judicial_api_backlog import build_backlog_interpretation, format_backlog_notice
from api.domains.judicial_api_cache import (
    DEFAULT_JUDGMENT_CACHE_FALLBACK,
    DEFAULT_JUDGMENT_CACHE_ROOT,
    judicial_api_cache_root,
    nas_judgment_cache_candidates,
)
from api.domains.judicial_api_policy import judicial_api_env_default

# --- Load .env for subprocess/cron credential access ---
try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv()
except Exception:
    pass



SAFE_EXIT = 0
WARNING_EXIT = 10
RISK_EXIT = 20
UNKNOWN_EXIT = 30

DEFAULT_CACHE_ROOT = judicial_api_cache_root()
DEFAULT_PULL_STATE = DEFAULT_CACHE_ROOT / "pull_state.json"
DEFAULT_PROCESS_STATE = DEFAULT_CACHE_ROOT / "process_state.json"
DEFAULT_RAW_ROOT = DEFAULT_CACHE_ROOT / "raw"
DEFAULT_NORMALIZED_ROOT = DEFAULT_CACHE_ROOT / "normalized"
DEFAULT_CONFIG_PATH = MAGI_ROOT / "json" / "config.json"


def env_path(name: str, default: Path) -> Path:
    return Path(os.environ.get(name, str(default)))


def load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


def is_judicial_raw_payload(path: Path) -> bool:
    name = path.name
    if name.startswith("._"):
        return False
    if path.suffix != ".json":
        return False
    if ".json." in name:
        return False
    return True


def parse_iso(value: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).strip())
    except Exception:
        return None


def age_hours(dt: Optional[datetime]) -> Optional[float]:
    if dt is None:
        return None
    return max(0.0, (time.time() - dt.timestamp()) / 3600.0)


def list_files(root: Path, *, judicial_raw_only: bool = False) -> list[Path]:
    if not root.exists():
        return []
    files = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if judicial_raw_only and not is_judicial_raw_payload(path):
            continue
        if path.name.startswith("._"):
            continue
        files.append(path)
    return sorted(files)


def iso_or_empty(dt: Optional[datetime]) -> str:
    return dt.isoformat() if dt else ""


def rounded(value: Optional[float]) -> Optional[float]:
    if value is None:
        return None
    return round(float(value), 2)


def env_bool(name: str, default: bool = False) -> bool:
    raw = str(os.environ.get(name, "")).strip().lower()
    if not raw:
        return bool(default)
    return raw in {"1", "true", "yes", "on"}


def detect_credentials() -> dict:
    sources = []
    config_path = env_path("MAGI_CONFIG_PATH", DEFAULT_CONFIG_PATH)

    config = load_json(config_path)
    user = str(config.get("judicial_api_user") or "").strip()
    password = str(config.get("judicial_api_pass") or "").strip()
    if user and password:
        sources.append("config.judicial_api_*")

    env_user = str(os.environ.get("MAGI_JUDICIAL_API_USER") or os.environ.get("JUDICIAL_API_USER") or "").strip()
    env_pass = str(os.environ.get("MAGI_JUDICIAL_API_PASS") or os.environ.get("JUDICIAL_API_PASS") or "").strip()
    if env_user and env_pass:
        sources.append("env")

    return {
        "present": bool(sources),
        "sources": sources,
        "config_path": str(config_path),
    }


def backlog_status(
    cache_root: Path,
    process_state_path: Path,
    raw_root: Path,
    *,
    verify_hashes: Optional[bool] = None,
) -> dict:
    proc_state = load_json(process_state_path)
    processed_map = proc_state.get("processed") if isinstance(proc_state.get("processed"), dict) else {}
    raw_files = list_files(raw_root, judicial_raw_only=True)
    if verify_hashes is None:
        verify_hashes = env_bool("JUDICIAL_API_BACKLOG_HASH_VERIFY", False)

    backlog_count = 0
    unreadable_count = 0
    oldest_pending_dt: Optional[datetime] = None
    newest_pending_dt: Optional[datetime] = None
    pending_files: list[str] = []

    for raw_path in raw_files:
        rel = os.path.relpath(raw_path, cache_root)
        processed_hash = processed_map.get(rel)
        pending = False
        if processed_hash and not verify_hashes:
            pending = False
        else:
            raw_text = read_text(raw_path)
            if not raw_text:
                unreadable_count += 1
                pending = True
            else:
                raw_hash = hashlib.sha1(raw_text.encode("utf-8", errors="ignore")).hexdigest()
                if processed_hash != raw_hash:
                    pending = True
        if not pending:
            continue
        backlog_count += 1
        pending_files.append(rel)
        try:
            dt = datetime.fromtimestamp(raw_path.stat().st_mtime)
        except Exception:
            dt = None
        if dt is not None:
            oldest_pending_dt = dt if oldest_pending_dt is None else min(oldest_pending_dt, dt)
            newest_pending_dt = dt if newest_pending_dt is None else max(newest_pending_dt, dt)

    return {
        "raw_total": len(raw_files),
        "processed_entries": len(processed_map),
        "backlog_count": backlog_count,
        "unreadable_count": unreadable_count,
        "oldest_backlog_at": iso_or_empty(oldest_pending_dt),
        "newest_backlog_at": iso_or_empty(newest_pending_dt),
        "oldest_backlog_age_hours": rounded(age_hours(oldest_pending_dt)),
        "newest_backlog_age_hours": rounded(age_hours(newest_pending_dt)),
        "pending_examples": pending_files[:10],
    }


def latest_pull_summary(pull_state_path: Path) -> dict:
    pull_state = load_json(pull_state_path)
    runs = pull_state.get("runs") if isinstance(pull_state.get("runs"), list) else []
    latest = runs[0] if runs and isinstance(runs[0], dict) else {}
    previous = runs[1] if len(runs) > 1 and isinstance(runs[1], dict) else {}
    ts = parse_iso(str(latest.get("ts") or ""))
    return {
        "exists": pull_state_path.exists(),
        "path": str(pull_state_path),
        "run_count": len(runs),
        "latest": latest,
        "latest_ts": iso_or_empty(ts),
        "latest_age_hours": rounded(age_hours(ts)),
        "credentials_source": str(latest.get("credentials_source") or ""),
        "consecutive_failures": int(latest.get("consecutive_failures") or 0),
        "source_listed_count": int(latest.get("source_listed_count") or 0),
        "source_completed_count": int(latest.get("source_completed_count") or 0),
        "source_remaining_count": int(latest.get("source_remaining_count") or 0),
        "previous_source_remaining_count": int(previous.get("source_remaining_count") or 0),
    }


def latest_process_summary(process_state_path: Path) -> dict:
    process_state = load_json(process_state_path)
    updated_at = parse_iso(str(process_state.get("updated_at") or ""))
    processed_map = process_state.get("processed") if isinstance(process_state.get("processed"), dict) else {}
    last_run = process_state.get("last_run") if isinstance(process_state.get("last_run"), dict) else {}
    return {
        "exists": process_state_path.exists(),
        "path": str(process_state_path),
        "updated_at": iso_or_empty(updated_at),
        "updated_age_hours": rounded(age_hours(updated_at)),
        "processed_entries": len(processed_map),
        "last_run": last_run,
    }


def normalized_summary(normalized_root: Path) -> dict:
    files = list_files(normalized_root)
    newest_dt: Optional[datetime] = None
    for item in files:
        try:
            dt = datetime.fromtimestamp(item.stat().st_mtime)
        except Exception:
            continue
        newest_dt = dt if newest_dt is None else max(newest_dt, dt)
    return {
        "root": str(normalized_root),
        "count": len(files),
        "latest_at": iso_or_empty(newest_dt),
        "latest_age_hours": rounded(age_hours(newest_dt)),
    }


def _unique_paths(paths: list[Path]) -> list[Path]:
    seen: set[str] = set()
    out: list[Path] = []
    for path in paths:
        key = str(path.expanduser())
        if key in seen:
            continue
        seen.add(key)
        out.append(path.expanduser())
    return out


def _cache_root_activity_ts(root: Path) -> float:
    """Return the newest pipeline-state timestamp under a candidate cache root."""

    timestamps: list[float] = []
    pull = latest_pull_summary(root / "pull_state.json")
    process = latest_process_summary(root / "process_state.json")
    for value in (pull.get("latest_ts"), process.get("updated_at")):
        dt = parse_iso(str(value or ""))
        if dt is not None:
            timestamps.append(dt.timestamp())
    for state_file in (root / "pull_state.json", root / "process_state.json"):
        try:
            if state_file.exists():
                timestamps.append(state_file.stat().st_mtime)
        except Exception:
            pass
    return max(timestamps) if timestamps else 0.0


def _candidate_cache_roots(default_root: Path) -> list[Path]:
    candidates = [default_root, DEFAULT_JUDGMENT_CACHE_ROOT / "judicial_api"]
    for env_name in ("JUDGMENT_CACHE_ROOT", "JUDGMENT_CACHE_ROOT_FALLBACK"):
        value = str(os.environ.get(env_name) or "").strip()
        if value:
            candidates.append(Path(value).expanduser() / "judicial_api")
    for root in nas_judgment_cache_candidates():
        candidates.append(root.expanduser() / "judicial_api")
    candidates.append(DEFAULT_JUDGMENT_CACHE_FALLBACK / "judicial_api")
    return _unique_paths(candidates)


def _select_active_cache_root(default_root: Path) -> tuple[Path, list[dict]]:
    """Pick the freshest existing cache root when cron and checker defaults diverge."""

    candidates = _candidate_cache_roots(default_root)
    scored: list[tuple[float, Path]] = []
    details: list[dict] = []
    for root in candidates:
        activity_ts = _cache_root_activity_ts(root)
        exists = root.exists()
        details.append(
            {
                "path": str(root),
                "exists": exists,
                "activity_at": datetime.fromtimestamp(activity_ts).isoformat() if activity_ts else "",
            }
        )
        if exists and activity_ts > 0:
            scored.append((activity_ts, root))
    if not scored:
        return default_root, details
    scored.sort(key=lambda item: item[0], reverse=True)
    return scored[0][1], details


def scheduled_day_process_capacity(cron_path: Path) -> dict:
    try:
        jobs = json.loads(cron_path.read_text(encoding="utf-8"))
    except Exception:
        jobs = []
    if not isinstance(jobs, list):
        return {"runs_per_day": 0, "daily_max_docs": 0, "avg_batch": 0}
    runs = 0
    daily_max_docs = 0
    for job in jobs:
        if not isinstance(job, dict) or not job.get("enabled", True):
            continue
        cmd = str(job.get("command") or "")
        if "official_api_day_process" not in cmd:
            continue
        m = re.search(r"official_api_day_process\s+(\{.*?\})", cmd)
        if not m:
            continue
        try:
            payload = json.loads(m.group(1).replace('\\"', '"'))
        except Exception:
            payload = {}
        runs += 1
        try:
            daily_max_docs += int(payload.get("max_docs") or 0)
        except Exception:
            pass
    avg = int(round(daily_max_docs / runs)) if runs > 0 else 0
    return {"runs_per_day": runs, "daily_max_docs": daily_max_docs, "avg_batch": avg}


def build_report() -> dict:
    explicit_cache = bool(os.environ.get("JUDICIAL_API_CACHE_ROOT"))
    explicit_paths = any(
        os.environ.get(name)
        for name in (
            "JUDICIAL_API_PULL_STATE_PATH",
            "JUDICIAL_API_PROCESS_STATE_PATH",
            "JUDICIAL_API_RAW_ROOT",
            "JUDICIAL_API_NORMALIZED_ROOT",
        )
    )
    selected_cache_root = env_path("JUDICIAL_API_CACHE_ROOT", DEFAULT_CACHE_ROOT)
    cache_root_candidates: list[dict] = []
    if not explicit_cache and not explicit_paths:
        selected_cache_root, cache_root_candidates = _select_active_cache_root(selected_cache_root)

    cache_root = selected_cache_root
    pull_state_path = env_path("JUDICIAL_API_PULL_STATE_PATH", DEFAULT_PULL_STATE)
    process_state_path = env_path("JUDICIAL_API_PROCESS_STATE_PATH", DEFAULT_PROCESS_STATE)
    raw_root = env_path("JUDICIAL_API_RAW_ROOT", DEFAULT_RAW_ROOT)
    normalized_root = env_path("JUDICIAL_API_NORMALIZED_ROOT", DEFAULT_NORMALIZED_ROOT)
    if not explicit_cache and not explicit_paths:
        pull_state_path = cache_root / "pull_state.json"
        process_state_path = cache_root / "process_state.json"
        raw_root = cache_root / "raw"
        normalized_root = cache_root / "normalized"

    try:
        pull_stale_hours = float(os.environ.get("JUDICIAL_API_PULL_STALE_HOURS", "30") or "30")
    except Exception:
        pull_stale_hours = 30.0
    try:
        process_stale_hours = float(os.environ.get("JUDICIAL_API_PROCESS_STALE_HOURS", "18") or "18")
    except Exception:
        process_stale_hours = 18.0
    try:
        backlog_warn_count = int(os.environ.get("JUDICIAL_API_BACKLOG_WARN_COUNT", "1") or "1")
    except Exception:
        backlog_warn_count = 1
    try:
        backlog_risk_age_hours = float(os.environ.get("JUDICIAL_API_BACKLOG_RISK_AGE_HOURS", "8") or "8")
    except Exception:
        backlog_risk_age_hours = 8.0

    credentials = detect_credentials()
    pull = latest_pull_summary(pull_state_path)
    process = latest_process_summary(process_state_path)
    backlog = backlog_status(cache_root, process_state_path, raw_root)
    normalized = normalized_summary(normalized_root)
    scheduled_capacity = scheduled_day_process_capacity(MAGI_ROOT / "cron_jobs.json")
    now_hour = datetime.now().hour

    reasons: list[str] = []
    status = "PIPELINE_HEALTHY"
    exit_code = SAFE_EXIT

    if not credentials["present"]:
        status = "MISSING_CREDENTIALS"
        exit_code = RISK_EXIT
        reasons.append("找不到司法院裁判資料介接帳密（judicial_api_user/judicial_api_pass）。")

    raw_total = int(backlog.get("raw_total") or 0)
    normalized_count = int(normalized.get("count") or 0)
    process_has_run = bool(process.get("exists") and process.get("updated_at"))
    if not pull["exists"] or not pull["latest_ts"]:
        if raw_total > 0 and (process_has_run or normalized_count > 0):
            reasons.append(
                "尚未找到夜間拉取狀態檔，但裁判資料檔、整理狀態與轉換結果可證明資料流正在運作；改以待整理量判斷。"
            )
        elif raw_total > 0:
            reasons.append("尚未找到夜間拉取狀態檔，但已有裁判資料檔；將由晨間整理狀態判斷風險。")
        else:
            if status == "PIPELINE_HEALTHY":
                has_schedule = int(scheduled_capacity.get("runs_per_day") or 0) > 0
                if credentials["present"] and has_schedule and not (0 <= now_hour < 6):
                    status = "PULL_WAITING_WINDOW"
                    exit_code = WARNING_EXIT
                    reasons.append(
                        "尚未找到夜間拉取狀態檔；目前不在 00:00-06:00 司法院介接服務時段，"
                        "且待整理序列為空，等待下一次夜間排程建立拉取紀錄。"
                    )
                else:
                    status = "PULL_NEVER_RUN"
                    exit_code = RISK_EXIT
                    reasons.append("尚未找到夜間拉取狀態檔或成功紀錄。")
    elif (pull["latest_age_hours"] or 0.0) > pull_stale_hours:
        pull_age = float(pull["latest_age_hours"] or 0.0)
        if raw_total > 0 and normalized_count > 0 and raw_total == int(backlog.get("processed_entries") or 0):
            if status == "PIPELINE_HEALTHY":
                status = "PULL_STALE_CLEAR"
                exit_code = WARNING_EXIT
            reasons.append(
                f"最近一次夜間拉取已超過 {pull_stale_hours:.1f} 小時，但已下載裁判資料整理序列目前清空；"
                "若超過 72 小時仍未更新，才需要檢查夜拉排程。"
            )
            if pull_age >= 72.0:
                reasons.append("夜間拉取已超過 72 小時未更新；目前序列已清空，先列為提醒並由夜間排程續查。")
        else:
            if status == "PIPELINE_HEALTHY":
                status = "PULL_STALE"
                exit_code = WARNING_EXIT
            reasons.append(
                f"最近一次夜間拉取已超過 {pull_stale_hours:.1f} 小時。"
            )

    if int(pull.get("consecutive_failures") or 0) >= 2:
        status = "PULL_FAILING"
        exit_code = RISK_EXIT
        reasons.append("夜間拉取連續失敗次數過高。")

    source_remaining = int(pull.get("source_remaining_count") or 0)
    if source_remaining > 0 and status == "PIPELINE_HEALTHY":
        status = "SOURCE_PULL_CATCHING_UP"
        exit_code = WARNING_EXIT
        reasons.append(
            f"司法院本次 JList 尚有 {source_remaining} 筆官方全文未鏡像；"
            "本機 raw backlog 為零不再被解讀成來源已完整，夜間增量會續抓。"
        )

    backlog_count = int(backlog.get("backlog_count") or 0)
    oldest_backlog_age_hours = float(backlog.get("oldest_backlog_age_hours") or 0.0)
    last_run = process.get("last_run") if isinstance(process.get("last_run"), dict) else {}
    scheduled_avg_batch = int(scheduled_capacity.get("avg_batch") or 0)
    try:
        env_batch = os.environ.get("JUDICIAL_API_DAY_MAX_PROCESS") or os.environ.get("JDG_API_DAY_MAX_PROCESS")
        configured_batch = int(
            scheduled_avg_batch
            or (env_batch if env_batch else 0)
            or last_run.get("max_docs")
            or judicial_api_env_default("JUDICIAL_API_DAY_MAX_PROCESS", "60")
            or "60"
        )
    except Exception:
        configured_batch = scheduled_avg_batch or 60
    try:
        runs_per_day = int(os.environ.get("JUDICIAL_API_DAY_RUNS_PER_DAY") or scheduled_capacity.get("runs_per_day") or "5")
    except Exception:
        runs_per_day = 5
    backlog_interpretation = build_backlog_interpretation(
        backlog_before=last_run.get("backlog_before", backlog_count),
        backlog_remaining=backlog_count,
        handled=last_run.get("handled", 0),
        db_upserts=last_run.get("db_upserts", 0),
        archive_upserts=last_run.get("archive_upserts", 0),
        vector_ingested=last_run.get("vector_ingested", 0),
        summarized=last_run.get("summarized", 0),
        errors=last_run.get("errors", 0),
        oldest_age_hours=backlog.get("oldest_backlog_age_hours"),
        newest_age_hours=backlog.get("newest_backlog_age_hours"),
        raw_total=backlog.get("raw_total"),
        unreadable_count=backlog.get("unreadable_count"),
        skipped_low_value=last_run.get("skipped_low_value", 0),
        skipped_missing_text=last_run.get("skipped_missing_text", 0),
        max_docs=configured_batch,
        runs_per_day=runs_per_day,
        cache_root=str(raw_root),
    )
    interpretation_status = str(backlog_interpretation.get("status") or "")
    interpretation_reduced = int(backlog_interpretation.get("reduced") or 0)
    interpretation_handled = int(backlog_interpretation.get("handled") or 0)

    if backlog_count > 0 and (not process["exists"] or not process["updated_at"]):
        status = "PROCESS_NEVER_RUN"
        exit_code = RISK_EXIT
        reasons.append("已有待整理裁判資料，但尚未找到晨間整理狀態檔。")
    elif backlog_count >= max(1, backlog_warn_count):
        if interpretation_status in {"CATCHING_UP", "AGING"} and (interpretation_reduced > 0 or interpretation_handled > 0):
            if status == "PIPELINE_HEALTHY":
                status = "BACKLOG_CATCHING_UP"
                exit_code = WARNING_EXIT
            aging_clause = "且已有跨日老化，" if interpretation_status == "AGING" else ""
            reasons.append(
                f"裁判資料尚有 {backlog_count} 份待整理，{aging_clause}本輪正在處理（消化 {interpretation_reduced}，處理 {interpretation_handled}）。"
            )
        elif oldest_backlog_age_hours >= backlog_risk_age_hours:
            status = "BACKLOG_STALE"
            exit_code = RISK_EXIT
            reasons.append(
                f"裁判資料共有 {backlog_count} 份待整理，最老積壓已 {oldest_backlog_age_hours:.2f} 小時。"
            )
        elif status == "PIPELINE_HEALTHY":
            status = "BACKLOG_WARNING"
            exit_code = WARNING_EXIT
            reasons.append(f"裁判資料尚有 {backlog_count} 份待晨間整理。")

    updated_age_hours = float(process.get("updated_age_hours") or 0.0)
    if backlog_count > 0 and process.get("updated_at") and updated_age_hours > process_stale_hours:
        status = "PROCESS_STALE"
        exit_code = RISK_EXIT
        reasons.append(
            f"晨間整理最後更新已超過 {process_stale_hours:.1f} 小時，且待整理量尚未清空。"
        )

    if status == "PIPELINE_HEALTHY":
        reasons.append("夜間拉取、白天整理與裁判資料待整理量目前看起來健康。")

    return {
        "status": status,
        "exit_code": exit_code,
        "summary": {
            "cache_root": str(cache_root),
            "cache_root_candidates": cache_root_candidates,
            "pull_stale_hours": pull_stale_hours,
            "process_stale_hours": process_stale_hours,
            "backlog_warn_count": backlog_warn_count,
            "backlog_risk_age_hours": backlog_risk_age_hours,
            "scheduled_day_capacity": scheduled_capacity,
        },
        "credentials": credentials,
        "pull": pull,
        "process": process,
        "backlog": backlog,
        "backlog_interpretation": backlog_interpretation,
        "normalized": normalized,
        "reasons": reasons,
    }


def print_human(report: dict) -> None:
    print("司法院裁判資料流程檢查")
    print(f"狀態：{report['status']}")
    print(f"快取根目錄：{report['summary']['cache_root']}")
    print(
        "帳密："
        + ("已就緒" if report["credentials"]["present"] else "缺少")
        + f"｜來源：{','.join(report['credentials']['sources']) or '-'}"
    )

    pull = report["pull"]
    latest = pull.get("latest") or {}
    print(
        "夜間拉取："
        f"最新時間={pull.get('latest_ts') or '-'}｜距今小時={pull.get('latest_age_hours') if pull.get('latest_age_hours') is not None else '-'}"
        f"｜新抓={latest.get('fetched', '-')}"
        f"｜略過={latest.get('skipped', '-')}"
        f"｜失敗={latest.get('failed', '-')}"
        f"｜連續失敗={pull.get('consecutive_failures', '-')}"
        f"｜帳密來源={pull.get('credentials_source') or '-'}"
    )

    process = report["process"]
    print(
        "白天整理："
        f"更新時間={process.get('updated_at') or '-'}｜距今小時={process.get('updated_age_hours') if process.get('updated_age_hours') is not None else '-'}"
        f"｜已整理={process.get('processed_entries', '-')}"
    )

    backlog = report["backlog"]
    interpretation = report.get("backlog_interpretation") if isinstance(report.get("backlog_interpretation"), dict) else {}
    if interpretation:
        print(format_backlog_notice("待整理量：", interpretation))
    else:
        print(
            "待整理量："
            f"資料檔總數={backlog.get('raw_total', '-')}"
            f"｜待整理={backlog.get('backlog_count', '-')}"
            f"｜不可讀={backlog.get('unreadable_count', '-')}"
            f"｜最久未整理小時={backlog.get('oldest_backlog_age_hours') if backlog.get('oldest_backlog_age_hours') is not None else '-'}"
        )
    if backlog.get("pending_examples"):
        print("待整理資料檔示例：")
        for item in backlog["pending_examples"]:
            print(f"  - {str(item).replace('raw/', '資料檔/')}")

    normalized = report["normalized"]
    print(
        "已轉換文字檔："
        f"數量={normalized.get('count', '-')}"
        f"｜最新時間={normalized.get('latest_at') or '-'}"
        f"｜距今小時={normalized.get('latest_age_hours') if normalized.get('latest_age_hours') is not None else '-'}"
    )

    print("判讀：")
    for item in report["reasons"]:
        print(f"  - {item}")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Check Judicial API night-pull/day-process pipeline health.")
    parser.add_argument("--json", action="store_true", help="Print JSON report.")
    args = parser.parse_args(argv)

    report = build_report()
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print_human(report)
    exit_code = report.get("exit_code")
    if exit_code is None:
        return UNKNOWN_EXIT
    return int(exit_code)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
