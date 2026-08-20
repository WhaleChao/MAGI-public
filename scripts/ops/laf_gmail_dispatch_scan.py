#!/usr/bin/env python3
"""One-shot LAF Gmail scan for supervised scheduled execution.

The API server deliberately keeps browser-capable Gmail work out of process.
Cron runs this bounded entrypoint every few minutes, using the same
LAFOrchestrator callback path with durable deduplication and success-only Gmail
marking.  Formal scheduled execution must pass ``--apply`` explicitly.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
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

from api.runtime_paths import get_config_path, get_laf_processed_emails_path  # noqa: E402
from casper_ecosystem.law_firm_orchestrators.laf_orchestrator import LAFOrchestrator  # noqa: E402
from laf import LAFGmailMonitor  # noqa: E402


_MUTABLE_STATIC_DIR = Path(
    os.environ.get("MAGI_MUTABLE_STATIC_DIR", "").strip() or REPO_ROOT / "static"
).expanduser()
_RUNTIME_DIR = Path(
    os.environ.get("MAGI_RUNTIME_DIR", "").strip() or REPO_ROOT / ".runtime"
).expanduser()
DEFAULT_STATE_PATH = _MUTABLE_STATIC_DIR / "laf_gmail_monitor_state.json"
DEFAULT_PENDING_PATH = _RUNTIME_DIR / "laf_gmail_dispatch_pending.json"


def _output_path(env_name: str, cli_value: Any, default: Path) -> Path:
    """Let a V3 deployment bind output even when a legacy cron command has an explicit path."""
    configured = os.environ.get(env_name, "").strip()
    return Path(configured or cli_value or default).expanduser()


def _json_default(value: Any) -> str:
    if isinstance(value, (datetime, Path)):
        return str(value)
    return repr(value)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
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


def _callback_succeeded(result: Any, *, route: str = "") -> bool:
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
        if str(route or "") == "dispatch" and result.get("created_case") is not True:
            return False
    return True


def _env_truthy(name: str) -> bool:
    return str(os.environ.get(name) or "").strip().lower() in {"1", "true", "yes", "on"}


def _load_gmail_provider_fixture() -> tuple[Path, dict[str, Any]] | None:
    raw = os.environ.get("MAGI_LAF_GMAIL_PROVIDER_FIXTURE", "").strip()
    if not raw:
        return None
    if os.environ.get("MAGI_V3_REALISM_SANDBOX") != "1":
        raise RuntimeError("LAF Gmail fixture requires the V3 realism sandbox")
    root_raw = os.environ.get("MAGI_V3_SCHEDULE_FIXTURE_ROOT", "").strip()
    if not root_raw:
        raise RuntimeError("LAF Gmail fixture root is missing")
    root = Path(root_raw).expanduser().resolve(strict=True)
    path = Path(raw).expanduser()
    if path.is_symlink():
        raise RuntimeError("LAF Gmail fixture may not be a symlink")
    path = path.resolve(strict=True)
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise RuntimeError("LAF Gmail fixture escapes the schedule sandbox") from exc
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("LAF Gmail fixture is unreadable") from exc
    messages = payload.get("messages") if isinstance(payload, dict) else None
    required = {"message_id", "subject", "notification_type"}
    if (
        not isinstance(messages, list)
        or not messages
        or any(
            not isinstance(row, dict)
            or any(not str(row.get(key) or "").strip() for key in required)
            for row in messages
        )
    ):
        raise RuntimeError("LAF Gmail fixture messages are malformed")
    return root, payload


class _FixtureGmailMonitor:
    """External Gmail boundary used only inside the schedule Seatbelt sandbox."""

    def __init__(self, root: Path, payload: dict[str, Any]):
        self.root = root
        self.payload = payload
        self.processed_exists_func = None
        self.marked: set[str] = set()
        self.transcript: list[dict[str, Any]] = []

    def _record(self, action: str, **values: Any) -> None:
        self.transcript.append({"action": action, **values})
        target = self.root / "gmail_provider_transcript.json"
        temporary = target.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(self.transcript, ensure_ascii=False, sort_keys=True, indent=2)
            + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, target)

    def authenticate(self) -> bool:
        self._record("authenticate", ok=True)
        return True

    def _laf_message_already_processed(self, message_id: str, check_exists_func: Any) -> bool:
        return message_id in self.marked or bool(
            callable(check_exists_func) and check_exists_func(message_id)
        )

    def check_emails(
        self,
        *,
        max_results: int,
        check_exists_func: Any,
        mark_processed: bool,
    ) -> list[SimpleNamespace]:
        rows: list[SimpleNamespace] = []
        for raw in self.payload["messages"][: max(0, int(max_results))]:
            message_id = str(raw.get("message_id") or "").strip()
            if self._laf_message_already_processed(message_id, check_exists_func):
                continue
            rows.append(SimpleNamespace(**dict(raw)))
            if mark_processed:
                self.mark_laf_processed(message_id)
        self._record(
            "check_emails",
            returned=len(rows),
            mark_processed=bool(mark_processed),
        )
        return rows

    def mark_laf_processed(self, message_id: str) -> None:
        self.marked.add(str(message_id))
        self._record("mark_laf_processed", message_id=str(message_id))

    def _close_service(self) -> None:
        self._record("close", ok=True)


def _make_monitor(*, credentials_path: str, token_path: str):
    provider_fixture = _load_gmail_provider_fixture()
    if provider_fixture is not None:
        return _FixtureGmailMonitor(*provider_fixture)
    return LAFGmailMonitor(
        credentials_path=credentials_path,
        token_path=token_path,
        log_callback=lambda msg: print(msg, flush=True),
        processed_ids_file=str(get_laf_processed_emails_path()),
    )


def _cleanup_run(orchestrator: Any, monitor: Any) -> list[str]:
    """Close browser and Gmail transports before interpreter teardown."""
    errors: list[str] = []
    close_orchestrator = getattr(orchestrator, "close", None)
    if callable(close_orchestrator):
        try:
            close_orchestrator()
        except Exception as exc:
            errors.append(f"orchestrator_cleanup_failed:{type(exc).__name__}:{exc}"[:500])
    close_monitor = getattr(monitor, "_close_service", None)
    if callable(close_monitor):
        try:
            close_monitor()
        except Exception as exc:
            errors.append(f"gmail_cleanup_failed:{type(exc).__name__}:{exc}"[:500])
    return errors


def _persist_durable_success(
    orchestrator: Any,
    case_info: Any,
    *,
    route: str = "",
    callback_result: Any = None,
) -> tuple[bool, str]:
    """Write the success marker to the MariaDB inbox before Gmail is marked."""
    message_id = str(getattr(case_info, "message_id", "") or "").strip()
    if not message_id:
        return False, "missing_message_id_for_durable_record"
    result = callback_result if isinstance(callback_result, dict) else {}
    created_case_id = str(
        result.get("created_case_id") or result.get("case_number") or ""
    ).strip()
    if str(route or "") == "dispatch":
        if result.get("created_case") is not True:
            return False, "dispatch_case_not_created"
        if not created_case_id:
            return False, "dispatch_created_case_id_missing"
    durable_status = "completed"
    if str(route or "") == "result_download" and bool(result.get("retry_queued")):
        laf_case_number = str(
            result.get("laf_case_number")
            or getattr(case_info, "laf_case_number", "")
            or ""
        ).strip()
        loader = getattr(orchestrator, "_load_pending_portal_downloads", None)
        if not laf_case_number or not callable(loader):
            return False, "durable_portal_retry_store_unavailable"
        try:
            queued = loader().get(laf_case_number) or {}
        except Exception as exc:
            return False, f"durable_portal_retry_read_failed:{type(exc).__name__}"[:500]
        expected_token = str(result.get("retry_queue_token") or "").strip()
        observed_token = str(queued.get("queue_token") or "").strip()
        if not getattr(orchestrator, "_portal_retry_item_is_pending", lambda _item: False)(queued):
            return False, "durable_portal_retry_not_observable_after_queue"
        if not expected_token:
            return False, "durable_portal_retry_receipt_missing"
        if observed_token != expected_token:
            return False, "durable_portal_retry_receipt_mismatch"
        durable_status = "pending_download"
    db = getattr(orchestrator, "db", None)
    check = getattr(db, "check_laf_email_exists", None)
    add = getattr(db, "add_laf_email_record", None)
    if db is None or not callable(add):
        return False, "durable_laf_email_store_unavailable"
    try:
        if callable(check) and bool(check(message_id)):
            return True, ""
        add(
            {
                "gmail_message_id": message_id,
                "subject": str(getattr(case_info, "subject", "") or "")[:500],
                "sender": str(getattr(case_info, "sender", "") or "")[:320],
                "received_at": getattr(case_info, "received_at", None),
                "status": durable_status,
                "case_number": str(getattr(case_info, "laf_case_number", "") or "")[:100],
                "created_case_id": created_case_id or None,
            }
        )
        if callable(check) and not bool(check(message_id)):
            return False, "durable_laf_email_record_not_observable_after_write"
        return True, ""
    except Exception as exc:
        return False, f"durable_laf_email_record_failed:{type(exc).__name__}:{exc}"[:500]


def _reconcile_pending_report(
    path: Path, monitor: Any, check_exists_func: Any
) -> dict[str, Any]:
    """Remove stale dry-run backlog entries already covered by durable dedup."""
    report = _load_json(path)
    cases = report.get("cases") if isinstance(report.get("cases"), dict) else {}
    clear: list[str] = []
    already_processed = getattr(monitor, "_laf_message_already_processed", None)
    if callable(already_processed):
        for key, row in dict(cases or {}).items():
            item = row if isinstance(row, dict) else {}
            message_id = str(item.get("message_id") or key or "").strip()
            if not message_id:
                continue
            try:
                if bool(already_processed(message_id, check_exists_func)):
                    clear.append(str(key))
            except Exception:
                continue
    return _update_pending_report(path, clear_keys=clear)


def run_once(args: argparse.Namespace) -> dict[str, Any]:
    started = time.time()
    apply_enabled = bool(getattr(args, "apply", False)) or _env_truthy("MAGI_LAF_GMAIL_APPLY")
    dry_run = True if bool(getattr(args, "dry_run", False)) else not apply_enabled
    orchestrator = LAFOrchestrator(dry_run=dry_run)
    credentials_path, token_path = _resolve_gmail_paths(orchestrator)

    state_path = _output_path("MAGI_LAF_GMAIL_STATE_PATH", args.json_out, DEFAULT_STATE_PATH)
    pending_path = _output_path("MAGI_LAF_GMAIL_PENDING_PATH", args.pending_out, DEFAULT_PENDING_PATH)

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
        "pending_report_path": str(pending_path),
        "seen": 0,
        "handled": 0,
        "marked_processed": 0,
        "skipped_dry_run": 0,
        "pending_count": 0,
        "failure_count": 0,
        "errors": [],
        "cases": [],
    }

    _write_json(state_path, summary)

    monitor = _make_monitor(
        credentials_path=credentials_path,
        token_path=token_path,
    )
    monitor.processed_exists_func = _db_processed_checker(orchestrator)
    orchestrator._gmail_monitor = monitor

    if not monitor.authenticate():
        cleanup_errors = _cleanup_run(orchestrator, monitor)
        summary["ok"] = False
        summary["status"] = "auth_failed"
        summary["updated_at"] = datetime.now().isoformat(timespec="seconds")
        summary["duration_sec"] = round(time.time() - started, 3)
        summary["errors"].append("gmail_auth_failed")
        summary["errors"].extend(cleanup_errors)
        summary["cleanup_ok"] = not cleanup_errors
        _write_json(state_path, summary)
        return summary

    pending = _reconcile_pending_report(
        pending_path, monitor, monitor.processed_exists_func
    )
    summary["pending_count"] = int(pending.get("pending_count") or 0)
    summary["failure_count"] = int(pending.get("failure_count") or 0)
    try:
        cases = monitor.check_emails(
            max_results=int(args.max_results),
            check_exists_func=monitor.processed_exists_func,
            mark_processed=False,
        )
    except Exception as exc:
        err = f"gmail_scan_failed:{type(exc).__name__}:{exc}"[:500]
        cleanup_errors = _cleanup_run(orchestrator, monitor)
        summary["errors"].append(err)
        summary["errors"].extend(cleanup_errors)
        summary["cleanup_ok"] = not cleanup_errors
        summary["ok"] = False
        summary["status"] = "error"
        summary["updated_at"] = datetime.now().isoformat(timespec="seconds")
        summary["duration_sec"] = round(time.time() - started, 3)
        _write_json(state_path, summary)
        return summary
    # Gmail normally returns newest first.  When an original assignment and a
    # later reply are both pending, process the original first so quoted reply
    # text cannot become the initial case metadata.
    cases = sorted(
        cases or [],
        key=lambda item: (
            str(getattr(item, "received_at", "") or ""),
            str(getattr(item, "message_id", "") or ""),
        ),
    )
    summary["seen"] = len(cases)

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
            if _callback_succeeded(callback_result, route=route):
                message_id = str(getattr(case_info, "message_id", "") or "").strip()
                durable_ok, durable_error = _persist_durable_success(
                    orchestrator,
                    case_info,
                    route=route,
                    callback_result=callback_result,
                )
                if message_id and durable_ok:
                    case_row["handled"] = True
                    summary["handled"] += 1
                    monitor.mark_laf_processed(message_id)
                    summary["marked_processed"] += 1
                    case_row["marked_processed"] = True
                    pending = _update_pending_report(pending_path, clear_keys=[pending_key])
                else:
                    err = durable_error or "missing_message_id_after_callback"
                    case_row["error"] = err
                    summary["errors"].append(err)
                    pending = _update_pending_report(
                        pending_path,
                        pending_updates={pending_key: _pending_case_row(case_row, status="failed_durable_record", error=err)},
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

    cleanup_errors = _cleanup_run(orchestrator, monitor)
    summary["errors"].extend(cleanup_errors)
    summary["cleanup_ok"] = not cleanup_errors
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
