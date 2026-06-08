#!/usr/bin/env python3
"""Bounded Google Drive/NAS bidirectional case sync worker.

The worker intentionally processes a small rotating slice of matched cases per
run. New NAS-only case folders are mirrored to Google Drive using Drive's native
folder layout; file sync remains missing-only and never overwrites or deletes.
"""

from __future__ import annotations

import argparse
import atexit
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


def worker_lock_path() -> Path:
    return runtime_dir() / "drive_case_sync_worker.pid"


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


def load_worker_status() -> dict:
    path = worker_status_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False


def _read_worker_lock_pid(path: Path | None = None) -> int:
    path = path or worker_lock_path()
    try:
        return int(path.read_text(encoding="utf-8").strip() or "0")
    except Exception:
        return 0


def _release_worker_lock(path: Path | None = None, pid: int | None = None) -> None:
    path = path or worker_lock_path()
    pid = int(pid or os.getpid())
    try:
        if _read_worker_lock_pid(path) == pid:
            path.unlink()
    except FileNotFoundError:
        pass
    except Exception:
        pass


def acquire_worker_lock() -> dict:
    """Acquire a real PID lock so scheduled Drive/NAS sync cannot overlap.

    The status JSON is intentionally not used as a lock because short scheduled
    jobs can overwrite it while a longer manual/full sync is still running.
    """
    path = worker_lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    current_pid = os.getpid()
    previous_pid = _read_worker_lock_pid(path)
    stale: dict = {}
    if previous_pid and previous_pid != current_pid and _pid_is_alive(previous_pid):
        return {
            "acquired": False,
            "status": "already_running",
            "active_pid": previous_pid,
            "lock_path": str(path),
        }
    if previous_pid and previous_pid != current_pid:
        stale = {
            "previous_pid": previous_pid,
            "previous_status": "stale_lock_cleared",
        }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(f"{current_pid}\n", encoding="utf-8")
    tmp.replace(path)
    atexit.register(_release_worker_lock, path, current_pid)
    return {
        "acquired": True,
        "pid": current_pid,
        "lock_path": str(path),
        "stale_lock": stale,
    }


def clear_stale_running_status() -> dict:
    """Return metadata about a stale running marker from a crashed worker."""
    previous = load_worker_status()
    status = str(previous.get("status") or "")
    if "running" not in status:
        return {}
    pid = int(previous.get("pid") or 0)
    if pid and _pid_is_alive(pid):
        return {}
    stale = {
        "ok": False,
        "status": "stale_running_cleared",
        "action_required": False,
        "previous_status": status,
        "previous_pid": pid,
        "previous_started_at": previous.get("started_at") or "",
        "finished_at": iso_now(),
        "message": "上一輪 Drive/NAS 同步狀態停在 running，但找不到對應程序；已清除殘留狀態。",
    }
    save_worker_status(stale)
    return stale


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


def repair_imported_drive_alias_folders(
    report: dict,
    *,
    apply: bool = True,
    delete_duplicate: bool = True,
    max_cases: int = 80,
    max_files_per_case: int = 300,
    max_seconds_per_case: int = 60,
) -> dict:
    """Repair Drive-style alias folders that already landed in local case roots."""
    if os.environ.get("MAGI_DRIVE_SYNC_REPAIR_IMPORTED_FOLDERS", "1").strip().lower() in {
        "0",
        "false",
        "no",
        "off",
    }:
        return {"enabled": False, "reason": "MAGI_DRIVE_SYNC_REPAIR_IMPORTED_FOLDERS=0"}
    try:
        from scripts.ops.repair_drive_imported_case_folders import repair_case_folder
    except Exception as exc:
        return {"enabled": False, "reason": f"import_failed:{type(exc).__name__}: {exc}"}

    cases = ((report.get("file_sync_plan") or {}).get("cases") or [])[: max(0, int(max_cases or 0))]
    summary = {
        "enabled": True,
        "mode": "apply" if apply else "dry_run",
        "cases_checked": 0,
        "cases_with_aliases": 0,
        "alias_folders": 0,
        "planned_moves": 0,
        "canonical_misfile_moves": 0,
        "duplicates": 0,
        "conflict_moves": 0,
        "conflicts": 0,
        "errors": 0,
    }
    items: list[dict] = []
    for case in cases:
        local_path = Path(str(case.get("local_path") or ""))
        if not local_path.is_dir():
            continue
        summary["cases_checked"] += 1
        case_report = repair_case_folder(
            local_path,
            apply=apply,
            delete_duplicate=delete_duplicate,
            max_files=max_files_per_case,
            max_seconds=max_seconds_per_case,
        )
        alias_count = len(case_report.get("alias_folders") or [])
        move_count = len(case_report.get("planned_moves") or [])
        canonical_misfile_count = len(case_report.get("canonical_misfile_moves") or [])
        duplicate_count = len(case_report.get("duplicates") or [])
        conflict_move_count = len(case_report.get("conflict_moves") or [])
        conflict_count = len(case_report.get("conflicts") or [])
        error_count = len(case_report.get("errors") or [])
        if alias_count or move_count or canonical_misfile_count or duplicate_count or conflict_move_count or conflict_count or error_count:
            summary["cases_with_aliases"] += 1 if alias_count else 0
            summary["alias_folders"] += alias_count
            summary["planned_moves"] += move_count
            summary["canonical_misfile_moves"] += canonical_misfile_count
            summary["duplicates"] += duplicate_count
            summary["conflict_moves"] += conflict_move_count
            summary["conflicts"] += conflict_count
            summary["errors"] += error_count
            items.append({
                "case_number": case.get("case_number") or "",
                "local_path": str(local_path),
                "alias_folders": case_report.get("alias_folders") or [],
                "planned_moves": move_count,
                "canonical_misfile_moves": canonical_misfile_count,
                "duplicates": duplicate_count,
                "conflict_moves": conflict_move_count,
                "conflicts": conflict_count,
                "errors": case_report.get("errors") or [],
            })
    return {"enabled": True, "summary": summary, "items": items[:40]}


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


def load_all_sync_case_numbers(*, limit: int = 24, offset: int = 0) -> tuple[list[str], int, int]:
    """Return a stable DB-backed slice of case numbers for all-file sync.

    This avoids a full NAS/Drive inventory scan every six hours.  The worker
    rotates over canonical DB cases and uses DB folder_path as the NAS source of
    truth while Drive keeps its own folder naming rules.
    """
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "osc-orchestrator"))
        from osc_headless.db import connect_mysql, db_config_from_env  # type: ignore
    except Exception:
        return [], 0, 0
    conn = None
    try:
        conn = connect_mysql(db_config_from_env(prefix="OSC_DB_"))
        cur = conn.cursor()
        try:
            where = """
                COALESCE(case_number, '') != ''
                AND COALESCE(folder_path, '') != ''
                AND case_number REGEXP '^[0-9]{4}-[0-9]{4}$'
                AND COALESCE(client_name, '') NOT IN ('範本', '模板', 'Template')
                AND COALESCE(case_reason, '') NOT IN ('upsert-smoke')
            """
            cur.execute(f"SELECT COUNT(*) FROM cases WHERE {where}")
            total_row = cur.fetchone()
            total = int((total_row or [0])[0] or 0)
            if total <= 0:
                return [], 0, 0
            safe_limit = max(1, min(int(limit or 24), 200))
            safe_offset = max(0, int(offset or 0)) % total
            cur.execute(
                f"""
                SELECT case_number
                FROM cases
                WHERE {where}
                ORDER BY case_number ASC
                LIMIT %s OFFSET %s
                """,
                (safe_limit, safe_offset),
            )
            numbers = [str(row[0] or "").strip() for row in (cur.fetchall() or []) if str(row[0] or "").strip()]
            next_offset = (safe_offset + max(len(numbers), 1)) % total
            return numbers, total, next_offset
        finally:
            cur.close()
    except Exception:
        return [], 0, 0
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
    parser.add_argument("--direct-all-cases", action="store_true")
    parser.add_argument("--direct-all-case-limit", type=int, default=24)
    parser.add_argument("--inventory-timeout-sec", type=int, default=1200)
    parser.add_argument("--no-downloads", action="store_true")
    parser.add_argument("--no-uploads", action="store_true")
    parser.add_argument("--no-create-drive-folders", action="store_true")
    parser.add_argument("--no-context-resolve", action="store_true")
    parser.add_argument("--no-direct-priority-sync", action="store_true")
    parser.add_argument("--no-repair-imported-folders", action="store_true")
    parser.add_argument("--repair-max-cases", type=int, default=80)
    parser.add_argument("--repair-max-files-per-case", type=int, default=300)
    parser.add_argument("--repair-max-seconds-per-case", type=int, default=60)
    args = parser.parse_args(argv)

    worker_lock = acquire_worker_lock()
    if not worker_lock.get("acquired"):
        status = {
            "ok": True,
            "status": "already_running",
            "action_required": False,
            "pid": os.getpid(),
            "active_worker_pid": worker_lock.get("active_pid"),
            "lock_path": worker_lock.get("lock_path") or "",
            "started_at": iso_now(),
            "finished_at": iso_now(),
            "message": "Drive/NAS 同步已在執行中，本次排程已略過，避免同時上傳/下載造成重複或錯放。",
        }
        save_worker_status(status)
        print(json.dumps(status, ensure_ascii=False, indent=2))
        return 0

    state = load_state()
    offset = max(0, int(state.get("matched_case_offset") or 0))
    all_case_offset = max(0, int(state.get("all_case_offset") or 0))
    priority_case_numbers = load_priority_case_numbers(
        args.priority_upcoming_days,
        limit=args.priority_case_limit,
    )
    all_case_numbers: list[str] = []
    all_case_total = 0
    all_case_next_offset = all_case_offset
    if args.direct_all_cases:
        all_case_numbers, all_case_total, all_case_next_offset = load_all_sync_case_numbers(
            limit=args.direct_all_case_limit,
            offset=all_case_offset,
        )
    direct_numbers = all_case_numbers if args.direct_all_cases else priority_case_numbers[: max(0, int(args.direct_priority_case_limit or 0))]
    direct_mode_requested = bool(direct_numbers and (args.direct_all_cases or not args.no_direct_priority_sync))
    direct_mode_label = "direct_all_case_sync_running" if args.direct_all_cases else "direct_priority_sync_running"
    needs_write_scope = not args.no_uploads or not args.no_create_drive_folders
    started_at = iso_now()
    stale_status = clear_stale_running_status()
    save_worker_status({
        "ok": None,
        "status": direct_mode_label if direct_mode_requested else "inventory_running",
        "action_required": False,
        "pid": os.getpid(),
        "started_at": started_at,
        "previous_stale_status": stale_status,
        "worker_lock": worker_lock,
        "matched_case_offset": offset,
        "all_case_offset": all_case_offset,
        "all_case_total": all_case_total,
        "all_case_numbers": all_case_numbers[:30],
        "priority_case_count": len(priority_case_numbers),
        "priority_case_numbers": priority_case_numbers[:30],
        "limits": {
            "matched_case_limit": args.matched_case_limit,
            "direct_priority_case_limit": args.direct_priority_case_limit,
            "direct_all_cases": bool(args.direct_all_cases),
            "direct_all_case_limit": args.direct_all_case_limit,
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
            "all_case_offset": all_case_offset,
            "all_case_total": all_case_total,
            "all_case_numbers": all_case_numbers[:30],
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
    if direct_mode and args.direct_all_cases:
        state["all_case_offset"] = all_case_next_offset
        state["matched_case_offset"] = offset
    elif direct_mode:
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
    repair_summary = (
        {"enabled": False, "reason": "--no-repair-imported-folders"}
        if args.no_repair_imported_folders
        else repair_imported_drive_alias_folders(
            report,
            apply=True,
            delete_duplicate=True,
            max_cases=args.repair_max_cases,
            max_files_per_case=args.repair_max_files_per_case,
            max_seconds_per_case=args.repair_max_seconds_per_case,
        )
    )
    report["drive_imported_folder_repair"] = repair_summary
    state["last_drive_imported_folder_repair"] = repair_summary.get("summary") or repair_summary
    state["last_priority_case_numbers"] = priority_case_numbers[:30]
    state["last_all_case_numbers"] = all_case_numbers[:30]
    success_status = {
        "ok": True,
        "status": "ok",
        "action_required": False,
        "pid": os.getpid(),
        "started_at": started_at,
        "finished_at": iso_now(),
        "matched_case_offset_before": offset,
        "matched_case_offset_after": state["matched_case_offset"],
        "all_case_offset_before": all_case_offset,
        "all_case_offset_after": state.get("all_case_offset", all_case_offset),
        "all_case_total": all_case_total,
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
        "drive_imported_folder_repair": state["last_drive_imported_folder_repair"],
        "priority_case_numbers": priority_case_numbers[:30],
        "all_case_numbers": all_case_numbers[:30],
        "mode": report.get("mode") or "",
    })

    print(json.dumps({
        "ok": True,
        "matched_case_offset_before": offset,
        "matched_case_offset_after": state["matched_case_offset"],
        "all_case_offset_before": all_case_offset,
        "all_case_offset_after": state.get("all_case_offset", all_case_offset),
        "all_case_total": all_case_total,
        "summary": report.get("summary") or {},
        "mode": report.get("mode") or "",
        "priority_case_numbers": priority_case_numbers[:30],
        "all_case_numbers": all_case_numbers[:30],
        "file_sync_summary": state["last_file_sync_summary"],
        "execution_summary": state["last_execution_summary"],
        "drive_folder_summary": state["last_drive_folder_summary"],
        "drive_imported_folder_repair": state["last_drive_imported_folder_repair"],
        "output_paths": report.get("output_paths") or {},
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
