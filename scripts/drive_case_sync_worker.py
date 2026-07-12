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

os.environ.setdefault("MAGI_MYSQL_USE_PURE", "1")
os.environ.setdefault("MYSQL_USE_PURE", "1")

try:
    from api.mysql_connector_guard import install_mysql_cext_blocker, patch_mysql_connector_for_stability

    install_mysql_cext_blocker()
    patch_mysql_connector_for_stability()
except Exception:
    pass

from api.osc.drive_case_sync import (
    DEFAULT_DRIVE_ROOT_NAME,
    DriveCaseSyncAuthRequired,
    report_has_partial_failures,
    run_inventory,
    run_priority_case_sync,
    runtime_dir,
)
from scripts.ops.background_task_locks import (
    acquire_lock,
    write_json_atomic,
)

_WORKER_LOCK_HANDLE = None
_CURRENT_RUN_CONTEXT: dict = {}
from api.domains.case_file_operation_lock import acquire_case_file_operation_lock, release_case_file_operation_lock


def state_path() -> Path:
    return runtime_dir() / "worker_state.json"


def worker_status_path(kind: str = "") -> Path:
    suffix = str(kind or "").strip().lower().replace("-", "_")
    if suffix:
        return runtime_dir() / f"drive_case_sync_worker_status_{suffix}_latest.json"
    return runtime_dir() / "drive_case_sync_worker_status_latest.json"


def worker_lock_path() -> Path:
    return runtime_dir() / "drive_case_sync_worker.pid"


def iso_now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class DriveCaseSyncTimeout(TimeoutError):
    pass


_RUN_PAYLOAD_KEYS = {
    "summary",
    "file_sync_summary",
    "execution_summary",
    "drive_folder_summary",
    "drive_imported_folder_repair",
    "priority_case_numbers",
    "all_case_numbers",
    "limits",
}
_TRANSIENT_STATUS_KEYS = {
    "active_worker_pid",
    "lock_path",
    "previous_status",
    "previous_pid",
    "previous_started_at",
    "next_step",
    "stale_lock_audit",
    "signal",
}
_TERMINAL_STATUS_KEYS = {
    "finished_at",
    "message",
}
_INTERRUPTED_STATUSES = {"timeout", "interrupted"}


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


def save_worker_status(status: dict, *, kind: str = "") -> None:
    payload = dict(status or {})
    if kind:
        payload["worker_kind"] = kind

    previous = load_worker_status()
    previous_by_kind = previous.get("status_by_kind")
    if not isinstance(previous_by_kind, dict):
        previous_by_kind = {}
    status_by_kind: dict[str, dict] = {str(k): dict(v) for k, v in previous_by_kind.items() if isinstance(v, dict)}
    if kind:
        status_by_kind[str(kind)] = payload

    status_text = str(payload.get("status") or "").strip().lower()
    if status_text in _INTERRUPTED_STATUSES or status_text.endswith("running") or bool(payload.get("ok")):
        stale_keys = _RUN_PAYLOAD_KEYS | _TRANSIENT_STATUS_KEYS | _TERMINAL_STATUS_KEYS
        previous = {k: v for k, v in previous.items() if k not in stale_keys}

    merged_payload = dict(previous)
    merged_payload.update(payload)
    if status_by_kind:
        merged_payload["status_by_kind"] = status_by_kind
    merged_payload.setdefault("worker_kind", kind or str(merged_payload.get("worker_kind") or ""))

    targets = [worker_status_path()]
    if kind:
        targets.append(worker_status_path(kind))
    for path in targets:
        write_json_atomic(path, merged_payload if path == worker_status_path() else payload)


def load_worker_status() -> dict:
    path = worker_status_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _load_worker_status_file(kind: str = "") -> dict:
    path = worker_status_path(kind)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _terminal_status_for_current_process(kind: str = "") -> dict:
    """Return a final status already written by this worker, if one exists."""
    candidates = []
    if kind:
        candidates.append(_load_worker_status_file(kind))
    candidates.append(load_worker_status())
    current_pid = os.getpid()
    for status in candidates:
        if not isinstance(status, dict) or not status:
            continue
        try:
            pid = int(status.get("pid") or 0)
        except Exception:
            pid = 0
        if pid != current_pid or not status.get("finished_at"):
            continue
        status_text = str(status.get("status") or "").strip().lower()
        if not status_text or "running" in status_text or status_text in {"interrupted", "timeout"}:
            continue
        return status
    return {}


def _terminal_status_exit_code(status: dict) -> int:
    if bool(status.get("ok")) and not bool(status.get("action_required")):
        return 0
    return 1


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
    global _WORKER_LOCK_HANDLE
    path = path or worker_lock_path()
    pid = int(pid or os.getpid())
    try:
        if _read_worker_lock_pid(path) == pid:
            path.unlink()
    except FileNotFoundError:
        pass
    except Exception:
        pass
    if _WORKER_LOCK_HANDLE is not None:
        try:
            _WORKER_LOCK_HANDLE.release()
        finally:
            _WORKER_LOCK_HANDLE = None


def _termination_status(signum: int) -> dict:
    ctx = dict(_CURRENT_RUN_CONTEXT or {})
    return {
        "ok": False,
        "status": "interrupted",
        "action_required": False,
        "pid": os.getpid(),
        "worker_kind": str(ctx.get("worker_kind") or ""),
        "started_at": str(ctx.get("started_at") or ""),
        "finished_at": iso_now(),
        "signal": int(signum),
        "matched_case_offset": int(ctx.get("matched_case_offset") or 0),
        "all_case_offset": int(ctx.get("all_case_offset") or 0),
        "all_case_total": int(ctx.get("all_case_total") or 0),
        "message": "Drive/NAS 同步被外層 watchdog 或系統訊號中止；狀態已落盤，下次排程會從保存的 offset 重試。",
    }


def _install_termination_status_handler() -> None:
    def _handle(signum, _frame):
        ctx = dict(_CURRENT_RUN_CONTEXT or {})
        kind = str(ctx.get("worker_kind") or "")
        terminal_status = _terminal_status_for_current_process(kind)
        if terminal_status:
            try:
                release_case_file_operation_lock()
            finally:
                _release_worker_lock()
            raise SystemExit(_terminal_status_exit_code(terminal_status))
        status = _termination_status(int(signum))
        kind = str(status.get("worker_kind") or "")
        try:
            save_worker_status(status, kind=kind)
        finally:
            try:
                release_case_file_operation_lock()
            finally:
                _release_worker_lock()
        raise SystemExit(128 + int(signum))

    for sig_name in ("SIGTERM", "SIGINT"):
        sig = getattr(signal, sig_name, None)
        if sig is not None:
            signal.signal(sig, _handle)


def acquire_worker_lock() -> dict:
    """Acquire a real PID lock so scheduled Drive/NAS sync cannot overlap.

    The status JSON is intentionally not used as a lock because short scheduled
    jobs can overwrite it while a longer manual/full sync is still running.
    """
    global _WORKER_LOCK_HANDLE
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
            "legacy_pid_file": True,
        }
    lock = acquire_lock(
        "drive_case_sync_worker",
        owner="drive_case_sync_worker",
        kind="singleton",
        path=path,
        blocking=False,
        write_pid_file=True,
    )
    if not lock.acquired:
        active_pid = int((lock.active_owner or {}).get("pid") or previous_pid or 0)
        if not active_pid or not _pid_is_alive(active_pid):
            return {
                "acquired": False,
                "status": "lock_held_unknown_owner",
                "active_pid": active_pid,
                "lock_path": str(path),
                "lock": lock.as_dict(),
                "stale_lock_audit": {
                    "action": "precise_fail",
                    "reason": "flock_is_held_but_owner_metadata_has_no_live_pid",
                    "metadata": lock.active_owner or {},
                },
            }
        return {
            "acquired": False,
            "status": "already_running",
            "active_pid": active_pid,
            "lock_path": str(path),
            "lock": lock.as_dict(),
        }
    if previous_pid and previous_pid != current_pid:
        stale = {
            "previous_pid": previous_pid,
            "previous_status": "stale_lock_cleared",
        }
    _WORKER_LOCK_HANDLE = lock
    atexit.register(_release_worker_lock, path, current_pid)
    return {
        "acquired": True,
        "pid": current_pid,
        "lock_path": str(path),
        "lock": lock.as_dict(),
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
    stale_kind = str(previous.get("worker_kind") or "").strip().lower()
    if stale_kind:
        previous_by_kind = previous.get("status_by_kind")
        if isinstance(previous_by_kind, dict):
            previous_by_kind = {str(k): dict(v) for k, v in previous_by_kind.items() if isinstance(v, dict)}
        else:
            previous_by_kind = {}
        previous_by_kind[stale_kind] = stale
        stale_with_map = dict(previous)
        stale_with_map.update(stale)
        stale_with_map["worker_kind"] = stale_kind
        stale_with_map["status_by_kind"] = previous_by_kind
        save_worker_status(stale_with_map, kind=stale_kind)
    else:
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


def save_state(state: dict, *, kind: str = "") -> None:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(state or {})
    if kind:
        previous = load_state()
        payload = {**previous, **payload}
        status_by_kind = previous.get("status_by_kind")
        if not isinstance(status_by_kind, dict):
            status_by_kind = {}
        status_by_kind = {str(k): dict(v) for k, v in status_by_kind.items() if isinstance(v, dict)}
        last_status = payload.get("last_status")
        if isinstance(last_status, dict):
            status_by_kind[str(kind)] = dict(last_status)
        payload["status_by_kind"] = status_by_kind
        kind_path = runtime_dir() / f"worker_state_{str(kind).strip().lower().replace('-', '_')}.json"
        write_json_atomic(kind_path, payload)
    write_json_atomic(path, payload)


def save_auth_required(exc: DriveCaseSyncAuthRequired, *, write: bool, kind: str = "") -> dict:
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
    save_worker_status({**report, "finished_at": iso_now()}, kind=kind)
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
        try:
            case_report = repair_case_folder(
                local_path,
                apply=apply,
                delete_duplicate=delete_duplicate,
                max_files=max_files_per_case,
                max_seconds=max_seconds_per_case,
            )
        except Exception as exc:
            summary["errors"] += 1
            items.append({
                "case_number": case.get("case_number") or "",
                "local_path": str(local_path),
                "alias_folders": [],
                "planned_moves": 0,
                "canonical_misfile_moves": 0,
                "duplicates": 0,
                "conflict_moves": 0,
                "conflicts": 0,
                "errors": [f"{type(exc).__name__}: {exc}"],
            })
            continue
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


def _env_truthy(name: str, default: str = "0") -> bool:
    return (os.environ.get(name, default) or "").strip().lower() in {"1", "true", "yes", "on"}


def run_imported_folder_repair(report: dict, args: argparse.Namespace) -> dict:
    if getattr(args, "no_repair_imported_folders", False):
        return {"enabled": False, "reason": "--no-repair-imported-folders"}

    apply_changes = bool(getattr(args, "repair_imported_folders_apply", False)) or _env_truthy(
        "MAGI_DRIVE_SYNC_REPAIR_APPLY",
        "0",
    )
    delete_duplicate = apply_changes and (
        bool(getattr(args, "repair_delete_duplicates", False))
        or _env_truthy("MAGI_DRIVE_SYNC_REPAIR_DELETE_DUPLICATES", "0")
    )
    result = repair_imported_drive_alias_folders(
        report,
        apply=apply_changes,
        delete_duplicate=delete_duplicate,
        max_cases=getattr(args, "repair_max_cases", 80),
        max_files_per_case=getattr(args, "repair_max_files_per_case", 300),
        max_seconds_per_case=getattr(args, "repair_max_seconds_per_case", 60),
    )
    if result.get("enabled"):
        result["safety"] = {
            "apply": apply_changes,
            "delete_duplicate": delete_duplicate,
            "default_mode": "dry_run",
            "apply_flag": "--repair-imported-folders-apply",
            "delete_flag": "--repair-delete-duplicates",
        }
    return result


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
    parser.add_argument("--repair-imported-folders-apply", action="store_true")
    parser.add_argument("--repair-delete-duplicates", action="store_true")
    parser.add_argument("--repair-max-cases", type=int, default=80)
    parser.add_argument("--repair-max-files-per-case", type=int, default=300)
    parser.add_argument("--repair-max-seconds-per-case", type=int, default=60)
    parser.add_argument("--repair-local-duplicates", action="store_true")
    parser.add_argument("--execute-local-duplicate-repair", action="store_true")
    parser.add_argument("--repair-local-duplicate-limit", type=int, default=0)
    args = parser.parse_args(argv)
    worker_kind = "all_files" if args.direct_all_cases else ("priority" if not args.no_direct_priority_sync else "inventory")

    worker_lock = acquire_worker_lock()
    if not worker_lock.get("acquired"):
        lock_status = str(worker_lock.get("status") or "already_running")
        precise_fail = lock_status != "already_running"
        status = {
            "ok": not precise_fail,
            "status": lock_status,
            "action_required": precise_fail,
            "pid": os.getpid(),
            "active_worker_pid": worker_lock.get("active_pid"),
            "lock_path": worker_lock.get("lock_path") or "",
            "started_at": iso_now(),
            "finished_at": iso_now(),
            "message": (
                "Drive/NAS worker lock 被持有，但找不到活的 owner metadata；本次精準失敗，避免誤判排程成功。"
                if precise_fail
                else "Drive/NAS 同步已在執行中，本次排程已略過，避免同時上傳/下載造成重複或錯放。"
            ),
            "worker_kind": worker_kind,
            "stale_lock_audit": worker_lock.get("stale_lock_audit") or {},
        }
        skip_path = runtime_dir() / f"drive_case_sync_worker_skip_{worker_kind}_latest.json"
        try:
            skip_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = skip_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(status, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            tmp.replace(skip_path)
        except Exception:
            pass
        save_worker_status(status, kind=worker_kind)
        print(json.dumps(status, ensure_ascii=False, indent=2))
        return 1 if precise_fail else 0
    case_file_lock = acquire_case_file_operation_lock(owner=f"drive_case_sync_worker:{worker_kind}")
    if not case_file_lock.get("acquired"):
        status = {
            "ok": True,
            "status": "case_file_operation_already_running",
            "action_required": False,
            "pid": os.getpid(),
            "active_worker_pid": case_file_lock.get("active_pid"),
            "lock_path": case_file_lock.get("lock_path") or "",
            "started_at": iso_now(),
            "finished_at": iso_now(),
            "message": "已有案件資料夾寫入任務在執行，本次 Drive/NAS 同步略過，避免與封存/清理同時改動。",
            "worker_kind": worker_kind,
        }
        save_worker_status(status, kind=worker_kind)
        print(json.dumps(status, ensure_ascii=False, indent=2))
        _release_worker_lock()
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
    _CURRENT_RUN_CONTEXT.clear()
    _CURRENT_RUN_CONTEXT.update(
        {
            "worker_kind": worker_kind,
            "started_at": started_at,
            "matched_case_offset": offset,
            "all_case_offset": all_case_offset,
            "all_case_total": all_case_total,
        }
    )
    _install_termination_status_handler()
    stale_status = clear_stale_running_status()
    save_worker_status({
        "ok": None,
        "status": direct_mode_label if direct_mode_requested else "inventory_running",
        "action_required": False,
        "pid": os.getpid(),
        "worker_kind": worker_kind,
        "started_at": started_at,
        "previous_stale_status": stale_status,
        "worker_lock": worker_lock,
        "case_file_operation_lock": case_file_lock,
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
            "repair_local_duplicates": bool(args.repair_local_duplicates or args.execute_local_duplicate_repair),
            "execute_local_duplicate_repair": bool(args.execute_local_duplicate_repair),
            "repair_local_duplicate_limit": args.repair_local_duplicate_limit,
        },
    }, kind=worker_kind)
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
                    repair_local_duplicates=args.repair_local_duplicates,
                    execute_local_duplicate_repair=args.execute_local_duplicate_repair,
                    repair_local_duplicate_limit=args.repair_local_duplicate_limit,
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
                    repair_local_duplicates=args.repair_local_duplicates,
                    execute_local_duplicate_repair=args.execute_local_duplicate_repair,
                    repair_local_duplicate_limit=args.repair_local_duplicate_limit,
                )
    except DriveCaseSyncAuthRequired as exc:
        status = save_auth_required(exc, write=needs_write_scope, kind=worker_kind)
        state["last_status"] = status
        state["last_summary"] = {"auth_required": True}
        save_state(state, kind=worker_kind)
        print(json.dumps(status, ensure_ascii=False, indent=2))
        release_case_file_operation_lock()
        _release_worker_lock()
        return 0
    except DriveCaseSyncTimeout as exc:
        status = {
            "ok": False,
            "status": "timeout",
            "action_required": False,
            "message": str(exc),
            "worker_kind": worker_kind,
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
        save_state(state, kind=worker_kind)
        save_worker_status(status, kind=worker_kind)
        print(json.dumps(status, ensure_ascii=False, indent=2))
        release_case_file_operation_lock()
        _release_worker_lock()
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
    repair_summary = run_imported_folder_repair(report, args)
    report["drive_imported_folder_repair"] = repair_summary
    state["last_drive_imported_folder_repair"] = repair_summary.get("summary") or repair_summary
    has_partial_failures = report_has_partial_failures(report)
    state["last_priority_case_numbers"] = priority_case_numbers[:30]
    state["last_all_case_numbers"] = all_case_numbers[:30]
    success_status = {
        "ok": not has_partial_failures,
        "status": "partial_failure" if has_partial_failures else "ok",
        "action_required": bool(has_partial_failures),
        "pid": os.getpid(),
        "worker_kind": worker_kind,
        "worker_lock": worker_lock,
        "case_file_operation_lock": case_file_lock,
        "started_at": started_at,
        "finished_at": iso_now(),
        "matched_case_offset_before": offset,
        "matched_case_offset_after": state["matched_case_offset"],
        "all_case_offset_before": all_case_offset,
        "all_case_offset_after": state.get("all_case_offset", all_case_offset),
        "all_case_total": all_case_total,
        "mode": report.get("mode") or "",
        "message": (
            "Drive/NAS 同步完成，但部分下載、上傳、建資料夾或整理動作失敗；請查看 summary 與 manifest。"
            if has_partial_failures else ""
        ),
    }
    state["last_status"] = success_status
    clear_auth_required()
    save_state(state, kind=worker_kind)
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
    }, kind=worker_kind)

    print(json.dumps({
        "ok": not has_partial_failures,
        "status": "partial_failure" if has_partial_failures else "ok",
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
    _CURRENT_RUN_CONTEXT.clear()
    release_case_file_operation_lock()
    _release_worker_lock()
    return 1 if has_partial_failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
