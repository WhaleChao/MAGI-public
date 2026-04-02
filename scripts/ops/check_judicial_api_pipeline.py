#!/usr/bin/env python3
import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

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

MAGI_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CACHE_ROOT = Path.home() / ".cache" / "judgment_collector" / "judicial_api"
DEFAULT_PULL_STATE = DEFAULT_CACHE_ROOT / "pull_state.json"
DEFAULT_PROCESS_STATE = DEFAULT_CACHE_ROOT / "process_state.json"
DEFAULT_RAW_ROOT = DEFAULT_CACHE_ROOT / "raw"
DEFAULT_NORMALIZED_ROOT = DEFAULT_CACHE_ROOT / "normalized"
DEFAULT_CONFIG_PATH = MAGI_ROOT / "json" / "config.json"
DEFAULT_WORKSPACE_AI_CONFIG_PATH = Path.home() / ".openclaw" / "workspace" / "ai_config.json"


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


def list_files(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(path for path in root.rglob("*") if path.is_file())


def iso_or_empty(dt: Optional[datetime]) -> str:
    return dt.isoformat() if dt else ""


def rounded(value: Optional[float]) -> Optional[float]:
    if value is None:
        return None
    return round(float(value), 2)


def detect_credentials() -> dict:
    sources = []
    config_path = env_path("MAGI_CONFIG_PATH", DEFAULT_CONFIG_PATH)
    workspace_ai_config_path = env_path("OPENCLAW_AI_CONFIG_PATH", DEFAULT_WORKSPACE_AI_CONFIG_PATH)

    config = load_json(config_path)
    user = str(config.get("judicial_api_user") or "").strip()
    password = str(config.get("judicial_api_pass") or "").strip()
    if user and password:
        sources.append("config.judicial_api_*")

    workspace_cfg = load_json(workspace_ai_config_path)
    workspace_user = str(workspace_cfg.get("judicial_api_user") or "").strip()
    workspace_pass = str(workspace_cfg.get("judicial_api_pass") or "").strip()
    if workspace_user and workspace_pass:
        sources.append("workspace.ai_config")

    env_user = str(os.environ.get("MAGI_JUDICIAL_API_USER") or os.environ.get("JUDICIAL_API_USER") or "").strip()
    env_pass = str(os.environ.get("MAGI_JUDICIAL_API_PASS") or os.environ.get("JUDICIAL_API_PASS") or "").strip()
    if env_user and env_pass:
        sources.append("env")

    return {
        "present": bool(sources),
        "sources": sources,
        "config_path": str(config_path),
        "workspace_ai_config_path": str(workspace_ai_config_path),
    }


def backlog_status(cache_root: Path, process_state_path: Path, raw_root: Path) -> dict:
    proc_state = load_json(process_state_path)
    processed_map = proc_state.get("processed") if isinstance(proc_state.get("processed"), dict) else {}
    raw_files = list_files(raw_root)

    backlog_count = 0
    unreadable_count = 0
    oldest_pending_dt: Optional[datetime] = None
    newest_pending_dt: Optional[datetime] = None
    pending_files: list[str] = []

    for raw_path in raw_files:
        rel = os.path.relpath(raw_path, cache_root)
        raw_text = read_text(raw_path)
        pending = False
        if not raw_text:
            unreadable_count += 1
            pending = True
        else:
            raw_hash = hashlib.sha1(raw_text.encode("utf-8", errors="ignore")).hexdigest()
            if processed_map.get(rel) != raw_hash:
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
    }


def latest_process_summary(process_state_path: Path) -> dict:
    process_state = load_json(process_state_path)
    updated_at = parse_iso(str(process_state.get("updated_at") or ""))
    processed_map = process_state.get("processed") if isinstance(process_state.get("processed"), dict) else {}
    return {
        "exists": process_state_path.exists(),
        "path": str(process_state_path),
        "updated_at": iso_or_empty(updated_at),
        "updated_age_hours": rounded(age_hours(updated_at)),
        "processed_entries": len(processed_map),
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


def build_report() -> dict:
    cache_root = env_path("JUDICIAL_API_CACHE_ROOT", DEFAULT_CACHE_ROOT)
    pull_state_path = env_path("JUDICIAL_API_PULL_STATE_PATH", DEFAULT_PULL_STATE)
    process_state_path = env_path("JUDICIAL_API_PROCESS_STATE_PATH", DEFAULT_PROCESS_STATE)
    raw_root = env_path("JUDICIAL_API_RAW_ROOT", DEFAULT_RAW_ROOT)
    normalized_root = env_path("JUDICIAL_API_NORMALIZED_ROOT", DEFAULT_NORMALIZED_ROOT)

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

    reasons: list[str] = []
    status = "PIPELINE_HEALTHY"
    exit_code = SAFE_EXIT

    if not credentials["present"]:
        status = "MISSING_CREDENTIALS"
        exit_code = RISK_EXIT
        reasons.append("找不到司法院 API 專用帳密（judicial_api_user/judicial_api_pass）。")

    if not pull["exists"] or not pull["latest_ts"]:
        if status == "PIPELINE_HEALTHY":
            status = "PULL_NEVER_RUN"
            exit_code = RISK_EXIT
        reasons.append("尚未找到 night pull 狀態檔或成功紀錄。")
    elif (pull["latest_age_hours"] or 0.0) > pull_stale_hours:
        if status == "PIPELINE_HEALTHY":
            status = "PULL_STALE"
            exit_code = WARNING_EXIT
        reasons.append(
            f"最近一次 night pull 已超過 {pull_stale_hours:.1f} 小時。"
        )

    if int(pull.get("consecutive_failures") or 0) >= 2:
        status = "PULL_FAILING"
        exit_code = RISK_EXIT
        reasons.append("night pull 連續失敗次數過高。")

    backlog_count = int(backlog.get("backlog_count") or 0)
    oldest_backlog_age_hours = float(backlog.get("oldest_backlog_age_hours") or 0.0)
    if backlog_count > 0 and (not process["exists"] or not process["updated_at"]):
        status = "PROCESS_NEVER_RUN"
        exit_code = RISK_EXIT
        reasons.append("已有 raw backlog，但尚未找到晨間整理狀態檔。")
    elif backlog_count >= max(1, backlog_warn_count):
        if oldest_backlog_age_hours >= backlog_risk_age_hours:
            status = "BACKLOG_STALE"
            exit_code = RISK_EXIT
            reasons.append(
                f"raw backlog 共有 {backlog_count} 份，最老積壓已 {oldest_backlog_age_hours:.2f} 小時。"
            )
        elif status == "PIPELINE_HEALTHY":
            status = "BACKLOG_WARNING"
            exit_code = WARNING_EXIT
            reasons.append(f"raw backlog 尚有 {backlog_count} 份待晨間整理消化。")

    updated_age_hours = float(process.get("updated_age_hours") or 0.0)
    if backlog_count > 0 and process.get("updated_at") and updated_age_hours > process_stale_hours:
        status = "PROCESS_STALE"
        exit_code = RISK_EXIT
        reasons.append(
            f"晨間整理最後更新已超過 {process_stale_hours:.1f} 小時，且 backlog 尚未清空。"
        )

    if status == "PIPELINE_HEALTHY":
        reasons.append("night pull、day process 與 raw backlog 目前看起來健康。")

    return {
        "status": status,
        "exit_code": exit_code,
        "summary": {
            "cache_root": str(cache_root),
            "pull_stale_hours": pull_stale_hours,
            "process_stale_hours": process_stale_hours,
            "backlog_warn_count": backlog_warn_count,
            "backlog_risk_age_hours": backlog_risk_age_hours,
        },
        "credentials": credentials,
        "pull": pull,
        "process": process,
        "backlog": backlog,
        "normalized": normalized,
        "reasons": reasons,
    }


def print_human(report: dict) -> None:
    print("Judicial API Pipeline Check")
    print(f"status: {report['status']}")
    print(f"cache root: {report['summary']['cache_root']}")
    print(
        "credentials: "
        + ("present" if report["credentials"]["present"] else "missing")
        + f" | sources={','.join(report['credentials']['sources']) or '-'}"
    )

    pull = report["pull"]
    latest = pull.get("latest") or {}
    print(
        "pull: "
        f"latest_ts={pull.get('latest_ts') or '-'} | age_hours={pull.get('latest_age_hours') if pull.get('latest_age_hours') is not None else '-'}"
        f" | fetched={latest.get('fetched', '-')}"
        f" | skipped={latest.get('skipped', '-')}"
        f" | failed={latest.get('failed', '-')}"
        f" | consecutive_failures={pull.get('consecutive_failures', '-')}"
        f" | credentials_source={pull.get('credentials_source') or '-'}"
    )

    process = report["process"]
    print(
        "process: "
        f"updated_at={process.get('updated_at') or '-'} | age_hours={process.get('updated_age_hours') if process.get('updated_age_hours') is not None else '-'}"
        f" | processed_entries={process.get('processed_entries', '-')}"
    )

    backlog = report["backlog"]
    print(
        "backlog: "
        f"raw_total={backlog.get('raw_total', '-')}"
        f" | pending={backlog.get('backlog_count', '-')}"
        f" | unreadable={backlog.get('unreadable_count', '-')}"
        f" | oldest_age_hours={backlog.get('oldest_backlog_age_hours') if backlog.get('oldest_backlog_age_hours') is not None else '-'}"
    )
    if backlog.get("pending_examples"):
        print("pending examples:")
        for item in backlog["pending_examples"]:
            print(f"  - {item}")

    normalized = report["normalized"]
    print(
        "normalized: "
        f"count={normalized.get('count', '-')}"
        f" | latest_at={normalized.get('latest_at') or '-'}"
        f" | latest_age_hours={normalized.get('latest_age_hours') if normalized.get('latest_age_hours') is not None else '-'}"
    )

    print("reasons:")
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
