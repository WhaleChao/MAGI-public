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


def _json_default(value: Any) -> str:
    if isinstance(value, (datetime, Path)):
        return str(value)
    return repr(value)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    tmp.replace(path)


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


def run_once(args: argparse.Namespace) -> dict[str, Any]:
    started = time.time()
    orchestrator = LAFOrchestrator(dry_run=bool(args.dry_run))
    credentials_path, token_path = _resolve_gmail_paths(orchestrator)

    summary: dict[str, Any] = {
        "status": "running",
        "source": "laf_gmail_dispatch_scan",
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "dry_run": bool(args.dry_run),
        "max_results": int(args.max_results),
        "credentials_path": credentials_path,
        "token_path": token_path,
        "seen": 0,
        "handled": 0,
        "marked_processed": 0,
        "skipped_dry_run": 0,
        "errors": [],
        "cases": [],
    }

    state_path = Path(args.json_out or DEFAULT_STATE_PATH)
    _write_json(state_path, summary)

    monitor = LAFGmailMonitor(
        credentials_path=credentials_path,
        token_path=token_path,
        log_callback=lambda msg: print(msg, flush=True),
    )
    monitor.processed_exists_func = _db_processed_checker(orchestrator)
    orchestrator._gmail_monitor = monitor

    if not monitor.authenticate():
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

        if args.dry_run:
            summary["skipped_dry_run"] += 1
            summary["cases"].append(case_row)
            continue

        try:
            callback_result = orchestrator.on_new_email(case_info)
            if callback_result is not False:
                summary["handled"] += 1
                case_row["handled"] = True
                monitor.mark_laf_processed(case_info.message_id)
                summary["marked_processed"] += 1
                case_row["marked_processed"] = True
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"
            summary["errors"].append(err[:500])
            case_row["error"] = err[:500]
        finally:
            summary["cases"].append(case_row)

    summary["status"] = "ok" if not summary["errors"] else "error"
    summary["updated_at"] = datetime.now().isoformat(timespec="seconds")
    summary["duration_sec"] = round(time.time() - started, 3)
    _write_json(state_path, summary)
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one bounded LAF Gmail scan.")
    parser.add_argument("--max-results", type=int, default=80)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json-out", default=str(DEFAULT_STATE_PATH))
    args = parser.parse_args(argv)

    result = run_once(args)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=_json_default))
    return 0 if result.get("status") in {"ok", "running"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
