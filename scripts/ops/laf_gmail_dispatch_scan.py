#!/usr/bin/env python3
"""One-shot LAF Gmail scan for cron fallback.

The API server also starts a long-running Gmail monitor thread.  This script is
the independent safety net: cron can run it every few minutes, and each run
uses the same LAFOrchestrator callback path as the daemon.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

ORCH_DIR = REPO_ROOT / "casper_ecosystem" / "law_firm_orchestrators"
LEGAL_SKILL_DIR = REPO_ROOT / "skills" / "legal"
for _p in (ORCH_DIR, LEGAL_SKILL_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

try:
    from dotenv import load_dotenv

    load_dotenv(REPO_ROOT / ".env")
except Exception:
    pass

from api.runtime_paths import get_config_path  # noqa: E402
from casper_ecosystem.law_firm_orchestrators.laf_orchestrator import LAFOrchestrator  # noqa: E402
from laf import LAFGmailMonitor  # noqa: E402


DEFAULT_STATE_PATH = REPO_ROOT / "static" / "laf_gmail_monitor_state.json"
DEFAULT_PENDING_PATH = REPO_ROOT / ".runtime" / "laf_gmail_dispatch_pending.json"


def _json_default(value: Any) -> str:
    if isinstance(value, (datetime, Path)):
        return str(value)
    return repr(value)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    tmp.replace(path)


def _load_json(path: Path) -> dict[str, Any]:
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}
    return {}


def _case_pending_key(case_info: Any) -> str:
    message_id = str(getattr(case_info, "message_id", "") or "").strip()
    if message_id:
        return message_id
    laf_no = str(getattr(case_info, "laf_case_number", "") or "").strip()
    subject = str(getattr(case_info, "subject", "") or "").strip()
    return f"{laf_no}|{subject}"[:260] or f"unknown-{int(time.time())}"


def _pending_case_row(case_row: dict[str, Any], *, status: str, error: str = "") -> dict[str, Any]:
    now = datetime.now().isoformat(timespec="seconds")
    row = dict(case_row)
    row.update({
        "status": status,
        "error": str(error or "")[:500],
        "updated_at": now,
    })
    row.setdefault("first_seen_at", now)
    return row


def _update_pending_report(
    path: Path,
    *,
    pending_updates: dict[str, dict[str, Any]] | None = None,
    clear_keys: list[str] | None = None,
) -> dict[str, Any]:
    report = _load_json(path)
    cases = report.get("cases") if isinstance(report.get("cases"), dict) else {}
    cases = dict(cases or {})
    for key in clear_keys or []:
        cases.pop(str(key), None)
    for key, row in (pending_updates or {}).items():
        old = cases.get(key) if isinstance(cases.get(key), dict) else {}
        merged = {**old, **row}
        if old.get("first_seen_at"):
            merged["first_seen_at"] = old.get("first_seen_at")
        cases[str(key)] = merged
    failures = [row for row in cases.values() if str(row.get("status") or "").startswith("failed")]
    out = {
        "ok": len(failures) == 0,
        "source": "laf_gmail_dispatch_scan",
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "pending_count": len(cases),
        "failure_count": len(failures),
        "cases": cases,
    }
    _write_json(path, out)
    return out


def _resolve_gmail_paths(orchestrator: LAFOrchestrator) -> tuple[str, str]:
    config = orchestrator.config or {}
    gmail_cfg = config.get("gmail") if isinstance(config.get("gmail"), dict) else {}

    credentials_path = (
        os.environ.get("MAGI_GMAIL_CREDENTIALS_PATH", "").strip()
        or str(gmail_cfg.get("credentials_path") or "").strip()
        or str(config.get("google_credentials_path") or "").strip()
        or str(get_config_path("credentials.json"))
    )
    token_path = (
        os.environ.get("MAGI_LAF_GMAIL_TOKEN_PATH", "").strip()
        or str(gmail_cfg.get("token_path") or "").strip()
        or str(config.get("google_token_path") or "").strip()
        or str(get_config_path("laf_gmail_token.pickle"))
    )
    return credentials_path, token_path


def _db_processed_checker(orchestrator: LAFOrchestrator):
    def check(message_id: str) -> bool:
        try:
            db = getattr(orchestrator, "db", None)
            if db is not None and hasattr(db, "check_laf_email_exists"):
                return bool(db.check_laf_email_exists(message_id))
        except Exception:
            return False
        return False

    return check


def _callback_succeeded(result: Any) -> bool:
    if result is False or result is None:
        return False
    if isinstance(result, dict):
        if result.get("success") is False or result.get("ok") is False:
            return False
        for key in ("handled", "processed", "consumed"):
            if key in result and result.get(key) is False:
                return False
        if result.get("error"):
            return False
    return True


def _env_truthy(name: str) -> bool:
    return str(os.environ.get(name) or "").strip().lower() in {"1", "true", "yes", "on"}


def run_once(args: argparse.Namespace) -> dict[str, Any]:
    started = time.time()
    apply_enabled = bool(getattr(args, "apply", False)) or _env_truthy("MAGI_LAF_GMAIL_APPLY")
    dry_run = True if bool(getattr(args, "dry_run", False)) else not apply_enabled
    orchestrator = LAFOrchestrator(dry_run=dry_run)
    credentials_path, token_path = _resolve_gmail_paths(orchestrator)

    summary: dict[str, Any] = {
        "ok": False,
        "status": "running",
        "source": "laf_gmail_dispatch_scan",
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "dry_run": dry_run,
        "apply": apply_enabled and not dry_run,
        "max_results": int(args.max_results),
        "credentials_path": credentials_path,
        "token_path": token_path,
        "pending_report_path": str(args.pending_out or DEFAULT_PENDING_PATH),
        "seen": 0,
        "handled": 0,
        "marked_processed": 0,
        "skipped_dry_run": 0,
        "pending_count": 0,
        "failure_count": 0,
        "errors": [],
        "cases": [],
    }

    state_path = Path(args.json_out or DEFAULT_STATE_PATH)
    pending_path = Path(args.pending_out or DEFAULT_PENDING_PATH)
    _write_json(state_path, summary)

    monitor = LAFGmailMonitor(
        credentials_path=credentials_path,
        token_path=token_path,
        log_callback=lambda msg: print(msg, flush=True),
    )
    monitor.processed_exists_func = _db_processed_checker(orchestrator)
    orchestrator._gmail_monitor = monitor

    if not monitor.authenticate():
        summary["ok"] = False
        summary["status"] = "auth_failed"
        summary["updated_at"] = datetime.now().isoformat(timespec="seconds")
        summary["duration_sec"] = round(time.time() - started, 3)
        summary["errors"].append("gmail_auth_failed")
        _write_json(state_path, summary)
        return summary

    cases = monitor.check_emails(
        max_results=int(args.max_results),
        check_exists_func=monitor.processed_exists_func,
        mark_processed=False,
    )
    summary["seen"] = len(cases or [])

    for case_info in cases:
        notification_type = str(getattr(case_info, "notification_type", "") or "")
        route = orchestrator._resolve_email_route(case_info, notification_type)
        case_row = {
            "message_id": str(getattr(case_info, "message_id", "") or ""),
            "subject": str(getattr(case_info, "subject", "") or "")[:300],
            "notification_type": notification_type,
            "route": route,
            "laf_case_number": str(getattr(case_info, "laf_case_number", "") or ""),
            "client_name": str(getattr(case_info, "client_name", "") or ""),
            "handled": False,
            "marked_processed": False,
        }

        pending_key = _case_pending_key(case_info)

        if dry_run:
            summary["skipped_dry_run"] += 1
            case_row["pending_key"] = pending_key
            pending = _update_pending_report(
                pending_path,
                pending_updates={pending_key: _pending_case_row(case_row, status="pending_dry_run")},
            )
            summary["pending_count"] = int(pending.get("pending_count") or 0)
            summary["failure_count"] = int(pending.get("failure_count") or 0)
            summary["cases"].append(case_row)
            continue

        try:
            callback_result = orchestrator.on_new_email(case_info)
            if _callback_succeeded(callback_result):
                case_row["handled"] = True
                message_id = str(getattr(case_info, "message_id", "") or "").strip()
                if message_id:
                    summary["handled"] += 1
                    monitor.mark_laf_processed(message_id)
                    summary["marked_processed"] += 1
                    case_row["marked_processed"] = True
                    pending = _update_pending_report(pending_path, clear_keys=[pending_key])
                else:
                    err = "missing_message_id_after_callback"
                    case_row["error"] = err
                    summary["errors"].append(err)
                    pending = _update_pending_report(
                        pending_path,
                        pending_updates={pending_key: _pending_case_row(case_row, status="failed_missing_message_id", error=err)},
                    )
                summary["pending_count"] = int(pending.get("pending_count") or 0)
                summary["failure_count"] = int(pending.get("failure_count") or 0)
            else:
                case_row["error"] = "callback_failed"
                if isinstance(callback_result, dict):
                    case_row["callback_result"] = callback_result
                    err = str(callback_result.get("error") or callback_result.get("message") or "callback_failed")
                    summary["errors"].append(err[:500])
                pending = _update_pending_report(
                    pending_path,
                    pending_updates={pending_key: _pending_case_row(case_row, status="failed_callback", error="callback_failed")},
                )
                summary["pending_count"] = int(pending.get("pending_count") or 0)
                summary["failure_count"] = int(pending.get("failure_count") or 0)
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"
            summary["errors"].append(err[:500])
            case_row["error"] = err[:500]
            pending = _update_pending_report(
                pending_path,
                pending_updates={pending_key: _pending_case_row(case_row, status="failed_exception", error=err)},
            )
            summary["pending_count"] = int(pending.get("pending_count") or 0)
            summary["failure_count"] = int(pending.get("failure_count") or 0)
        finally:
            summary["cases"].append(case_row)

    summary["ok"] = not bool(summary["errors"])
    summary["status"] = "ok" if summary["ok"] else "error"
    summary["updated_at"] = datetime.now().isoformat(timespec="seconds")
    summary["duration_sec"] = round(time.time() - started, 3)
    _write_json(state_path, summary)
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one bounded LAF Gmail scan.")
    parser.add_argument("--max-results", type=int, default=80)
    parser.add_argument("--apply", action="store_true", help="正式 callback 並在成功後標記 Gmail 已處理；預設為 dry-run")
    parser.add_argument("--dry-run", action="store_true", help="強制只掃描/報告，不 callback、不 mark processed")
    parser.add_argument("--json-out", default=str(DEFAULT_STATE_PATH))
    parser.add_argument("--pending-out", default=str(DEFAULT_PENDING_PATH))
    args = parser.parse_args(argv)

    result = run_once(args)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=_json_default))
    return 0 if result.get("status") in {"ok", "running"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
