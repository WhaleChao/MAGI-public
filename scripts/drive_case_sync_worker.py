#!/usr/bin/env python3
"""Bounded Google Drive/NAS bidirectional case sync worker.

The worker intentionally processes a small rotating slice of matched cases per
run. New NAS-only case folders are mirrored to Google Drive using Drive's native
folder layout; file sync remains missing-only and never overwrites or deletes.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import signal
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api.osc.drive_case_sync import (
    DEFAULT_DRIVE_ROOT_NAME,
    DriveCaseSyncAuthRequired,
    run_inventory,
    run_priority_case_sync,
    runtime_dir,
)


def state_path() -> Path:
    return runtime_dir() / "worker_state.json"


def worker_status_path() -> Path:
    return runtime_dir() / "drive_case_sync_worker_status_latest.json"


def iso_now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class DriveCaseSyncTimeout(TimeoutError):
    pass


@contextlib.contextmanager
def inventory_time_limit(seconds: int):
    if seconds <= 0 or not hasattr(signal, "SIGALRM"):
        yield
        return

    def _handle_timeout(_signum, _frame):
        raise DriveCaseSyncTimeout(f"drive_case_sync_timeout:{seconds}s")

    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_alarm = signal.alarm(0)
    signal.signal(signal.SIGALRM, _handle_timeout)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_alarm:
            signal.alarm(previous_alarm)


def save_worker_status(status: dict) -> None:
    path = worker_status_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(status, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def load_state() -> dict:
    path = state_path()
    if not path.exists():
        return {"matched_case_offset": 0}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"matched_case_offset": 0}
    if not isinstance(data, dict):
        return {"matched_case_offset": 0}
    return data


def save_state(state: dict) -> None:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def save_auth_required(exc: DriveCaseSyncAuthRequired, *, write: bool) -> dict:
    report = {
        "ok": False,
        "status": "auth_required",
        "action_required": True,
        "message": str(exc),
        "token_path": str(exc.token_path or ""),
        "write_scope": bool(write),
        "next_step": (
            "請在本機執行：python3 -m api.osc.drive_case_sync --auth "
            + ("--execute-uploads 或 --ensure-drive-case-folders 重新建立寫入授權" if write else "重新建立唯讀授權")
        ),
    }
    path = runtime_dir() / "drive_case_sync_auth_required_latest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    save_worker_status({**report, "finished_at": iso_now()})
    return report


def clear_auth_required() -> None:
    """Remove a stale auth-required marker after a successful worker run."""
    try:
        (runtime_dir() / "drive_case_sync_auth_required_latest.json").unlink()
    except FileNotFoundError:
        pass


def load_priority_case_numbers(days: int, *, limit: int = 80) -> list[str]:
    """Return upcoming case numbers so Drive/NAS sync reaches urgent files first."""
    if days <= 0:
        return []
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "osc-orchestrator"))
        from osc_headless.db import connect_mysql, db_config_from_env  # type: ignore
    except Exception:
        return []
    start = datetime.now().date().isoformat()
    end = (datetime.now().date() + timedelta(days=days)).isoformat()
    conn = None
    try:
        conn = connect_mysql(db_config_from_env(prefix="OSC_DB_"))
        cur = conn.cursor()
        try:
            cur.execute(
                """
                SELECT DISTINCT case_number
                FROM case_todos
                WHERE COALESCE(case_number, '') != ''
                  AND todo_date BETWEEN %s AND %s
                  AND (
                    status IS NULL OR status=''
                    OR status NOT IN ('deleted', 'calendar_deduped', 'completed', 'done', '已完成', '完成', 'cancelled', 'canceled', '取消')
                  )
                ORDER BY todo_date ASC, case_number DESC
                LIMIT %s
                """,
                (start, end, max(1, int(limit or 80))),
            )
            return [str(row[0] or "").strip() for row in (cur.fetchall() or []) if str(row[0] or "").strip()]
        finally:
            cur.close()
    except Exception:
        return []
    finally:
        try:
            if conn:
                conn.close()
        except Exception:
            pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MAGI bounded Drive/NAS bidirectional sync worker")
    parser.add_argument("--root-id", default="")
    parser.add_argument("--root-name", default="")
    parser.add_argument("--max-depth", type=int, default=7)
    parser.add_argument("--max-items", type=int, default=5000)
    parser.add_argument("--matched-case-limit", type=int, default=4)
    parser.add_argument("--download-limit", type=int, default=20)
    parser.add_argument("--upload-limit", type=int, default=20)
    parser.add_argument("--max-download-bytes", type=int, default=300_000_000)
    parser.add_argument("--max-upload-bytes", type=int, default=300_000_000)
    parser.add_argument("--max-case-depth", type=int, default=4)
    parser.add_argument("--max-case-items", type=int, default=150)
    parser.add_argument("--create-drive-folder-limit", type=int, default=10)
    parser.add_argument("--create-drive-folder-max-age-hours", type=int, default=168)
    parser.add_argument("--drive-owner-bucket", default="")
    parser.add_argument("--priority-upcoming-days", type=int, default=21)
    parser.add_argument("--priority-case-limit", type=int, default=80)
    parser.add_argument("--direct-priority-case-limit", type=int, default=24)
    parser.add_argument("--inventory-timeout-sec", type=int, default=1200)
    parser.add_argument("--no-downloads", action="store_true")
    parser.add_argument("--no-uploads", action="store_true")
    parser.add_argument("--no-create-drive-folders", action="store_true")
    parser.add_argument("--no-context-resolve", action="store_true")
    parser.add_argument("--no-direct-priority-sync", action="store_true")
    args = parser.parse_args(argv)

    state = load_state()
    offset = max(0, int(state.get("matched_case_offset") or 0))
    priority_case_numbers = load_priority_case_numbers(
        args.priority_upcoming_days,
        limit=args.priority_case_limit,
    )
    direct_numbers = priority_case_numbers[: max(0, int(args.direct_priority_case_limit or 0))]
    direct_mode_requested = bool(direct_numbers and not args.no_direct_priority_sync)
    needs_write_scope = not args.no_uploads or not args.no_create_drive_folders
    started_at = iso_now()
    save_worker_status({
        "ok": None,
        "status": "direct_priority_sync_running" if direct_mode_requested else "inventory_running",
        "action_required": False,
        "started_at": started_at,
        "matched_case_offset": offset,
        "priority_case_count": len(priority_case_numbers),
        "priority_case_numbers": priority_case_numbers[:30],
        "limits": {
            "matched_case_limit": args.matched_case_limit,
            "direct_priority_case_limit": args.direct_priority_case_limit,
            "download_limit": 0 if args.no_downloads else args.download_limit,
            "upload_limit": 0 if args.no_uploads else args.upload_limit,
            "max_case_depth": args.max_case_depth,
            "max_case_items": args.max_case_items,
            "inventory_timeout_sec": args.inventory_timeout_sec,
        },
    })
    try:
        with inventory_time_limit(max(0, int(args.inventory_timeout_sec or 0))):
            root_name = args.root_name or os.environ.get("MAGI_DRIVE_SYNC_ROOT_FOLDER_NAME", DEFAULT_DRIVE_ROOT_NAME)
            if direct_mode_requested:
                report = run_priority_case_sync(
                    case_numbers=direct_numbers,
                    root_id=args.root_id,
                    root_name=root_name,
                    file_diff=not (args.no_downloads and args.no_uploads),
                    execute_downloads=not args.no_downloads,
                    execute_uploads=not args.no_uploads,
                    download_limit=args.download_limit,
                    max_download_bytes=args.max_download_bytes,
                    upload_limit=args.upload_limit,
                    max_upload_bytes=args.max_upload_bytes,
                    max_case_depth=args.max_case_depth,
                    max_case_items=args.max_case_items,
                    ensure_drive_case_folders=not args.no_create_drive_folders,
                    drive_owner_bucket_name=args.drive_owner_bucket,
                )
            else:
                report = run_inventory(
                    root_id=args.root_id,
                    root_name=root_name,
                    max_depth=args.max_depth,
                    max_items=args.max_items,
                    resolve_context=not args.no_context_resolve,
                    file_diff=not (args.no_downloads and args.no_uploads),
                    execute_downloads=not args.no_downloads,
                    execute_uploads=not args.no_uploads,
                    download_limit=args.download_limit,
                    max_download_bytes=args.max_download_bytes,
                    upload_limit=args.upload_limit,
                    max_upload_bytes=args.max_upload_bytes,
                    max_case_depth=args.max_case_depth,
                    max_case_items=args.max_case_items,
                    matched_case_limit=args.matched_case_limit,
                    matched_case_offset=offset,
                    priority_case_numbers=priority_case_numbers,
                    ensure_drive_case_folders=not args.no_create_drive_folders,
                    create_drive_folder_limit=args.create_drive_folder_limit,
                    create_drive_folder_max_age_hours=args.create_drive_folder_max_age_hours,
                    drive_owner_bucket_name=args.drive_owner_bucket,
                )
    except DriveCaseSyncAuthRequired as exc:
        status = save_auth_required(exc, write=needs_write_scope)
        state["last_status"] = status
        state["last_summary"] = {"auth_required": True}
        save_state(state)
        print(json.dumps(status, ensure_ascii=False, indent=2))
        return 0
    except DriveCaseSyncTimeout as exc:
        status = {
            "ok": False,
            "status": "timeout",
            "action_required": False,
            "message": str(exc),
            "started_at": started_at,
            "finished_at": iso_now(),
            "matched_case_offset": offset,
            "priority_case_count": len(priority_case_numbers),
            "priority_case_numbers": priority_case_numbers[:30],
            "next_step": "下次排程會從同一批近期待辦案件重試；若連續逾時，請降低 matched-case-limit 或檢查 Google Drive/NAS 連線。",
        }
        state["last_status"] = status
        state["last_summary"] = {"timeout": True}
        state["last_priority_case_numbers"] = priority_case_numbers[:30]
        save_state(state)
        save_worker_status(status)
        print(json.dumps(status, ensure_ascii=False, indent=2))
        return 2

    direct_mode = report.get("mode") == "direct_db_case_sync"
    matched_total = int((report.get("summary") or {}).get("matched_case_folders") or 0)
    scanned = int(((report.get("file_sync_plan") or {}).get("summary") or {}).get("matched_cases_scanned") or 0)
    if direct_mode:
        state["matched_case_offset"] = offset
    elif matched_total > 0:
        state["matched_case_offset"] = (offset + max(scanned, 1)) % matched_total
    else:
        state["matched_case_offset"] = 0
    state["last_output_paths"] = report.get("output_paths") or {}
    state["last_summary"] = report.get("summary") or {}
    state["last_file_sync_summary"] = (report.get("file_sync_plan") or {}).get("summary") or {}
    state["last_execution_summary"] = (report.get("execution_result") or {}).get("summary") or {}
    state["last_drive_folder_summary"] = (report.get("drive_folder_result") or {}).get("summary") or {}
    state["last_priority_case_numbers"] = priority_case_numbers[:30]
    success_status = {
        "ok": True,
        "status": "ok",
        "action_required": False,
        "started_at": started_at,
        "finished_at": iso_now(),
        "matched_case_offset_before": offset,
        "matched_case_offset_after": state["matched_case_offset"],
        "mode": report.get("mode") or "",
    }
    state["last_status"] = success_status
    clear_auth_required()
    save_state(state)
    save_worker_status({
        **success_status,
        "summary": state["last_summary"],
        "file_sync_summary": state["last_file_sync_summary"],
        "execution_summary": state["last_execution_summary"],
        "drive_folder_summary": state["last_drive_folder_summary"],
        "priority_case_numbers": priority_case_numbers[:30],
        "mode": report.get("mode") or "",
    })

    print(json.dumps({
        "ok": True,
        "matched_case_offset_before": offset,
        "matched_case_offset_after": state["matched_case_offset"],
        "summary": report.get("summary") or {},
        "mode": report.get("mode") or "",
        "priority_case_numbers": priority_case_numbers[:30],
        "file_sync_summary": state["last_file_sync_summary"],
        "execution_summary": state["last_execution_summary"],
        "drive_folder_summary": state["last_drive_folder_summary"],
        "output_paths": report.get("output_paths") or {},
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
