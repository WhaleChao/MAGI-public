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
import hashlib
import json
import os
import signal
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

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
    CaseFolder,
    CaseMeta,
    DEFAULT_DRIVE_ROOT_NAME,
    DriveCaseSyncAuthRequired,
    DriveCaseSyncDeadline,
    LOCAL_HASH_RETRYABLE_FAILURE_CODES,
    drive_case_sync_exclusion_reason,
    drive_relative_path_for_local_case,
    drive_to_nas_download_skip_reason,
    drive_to_nas_relative_path,
    nas_to_drive_relative_path,
    nas_to_drive_upload_skip_reason,
    report_has_partial_failures,
    run_inventory,
    run_priority_case_sync,
    runtime_dir,
)
from scripts.ops.background_task_locks import (
    acquire_lock,
    metadata_path,
    read_json,
    write_json_atomic,
)
from magi_v3.drive_file_checkpoint import (
    DriveFileCheckpoint,
    DriveFileCheckpointError,
    case_token as drive_checkpoint_case_token,
)

_WORKER_LOCK_HANDLE = None
_CURRENT_RUN_CONTEXT: dict = {}
_ACTIVE_FILE_CHECKPOINT: DriveFileCheckpoint | None = None
_LAST_CHECKPOINT_PUBLICATION: dict[str, Any] = {}
from api.domains.case_file_operation_lock import acquire_case_file_operation_lock, release_case_file_operation_lock


def state_path() -> Path:
    return runtime_dir() / "worker_state.json"


def file_checkpoint_path(kind: str) -> Path:
    suffix = str(kind or "").strip().lower().replace("-", "_") or "inventory"
    # This directory is dedicated to PII-private durable progress and can be
    # safely forced to 0700 without changing the shared runtime directory.
    return runtime_dir() / "private" / f"drive_case_file_checkpoint_{suffix}.json"


def worker_status_path(kind: str = "") -> Path:
    suffix = str(kind or "").strip().lower().replace("-", "_")
    if suffix:
        return runtime_dir() / f"drive_case_sync_worker_status_{suffix}_latest.json"
    return runtime_dir() / "drive_case_sync_worker_status_latest.json"


def worker_lock_path() -> Path:
    return runtime_dir() / "drive_case_sync_worker.pid"


def iso_now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _adaptive_all_case_limit(requested: int, state: dict[str, Any]) -> int:
    """Raise a proven-fast one-case sweep without removing the safety cap."""
    requested = max(1, int(requested or 1))
    if requested != 1 or os.environ.get("MAGI_V3_SCHEDULE_FIXTURE_ROOT"):
        return requested
    rows = state.get("status_by_kind") if isinstance(state.get("status_by_kind"), dict) else {}
    previous = rows.get("all_files") if isinstance(rows.get("all_files"), dict) else {}
    if not previous:
        candidate = state.get("last_status") if isinstance(state.get("last_status"), dict) else {}
        previous = candidate if candidate.get("worker_kind") == "all_files" else {}
    if previous.get("ok") is not True or str(previous.get("status") or "").lower() != "ok":
        return requested
    hard_failures = 0
    for section in (
        state.get("last_file_sync_summary"),
        state.get("last_execution_summary"),
        state.get("last_drive_folder_summary"),
        state.get("last_drive_imported_folder_repair"),
    ):
        if not isinstance(section, dict):
            continue
        hard_failures += sum(
            int(section.get(key) or 0)
            for key in (
                "case_errors",
                "incomplete_case_scans",
                "download_failed",
                "upload_failed",
                "failed",
                "errors",
            )
        )
    try:
        started = datetime.fromisoformat(str(previous.get("started_at") or "").replace("Z", "+00:00"))
        finished = datetime.fromisoformat(str(previous.get("finished_at") or "").replace("Z", "+00:00"))
        elapsed = max(0.0, (finished - started).total_seconds())
    except (TypeError, ValueError):
        return requested
    # Four cases remain within the existing 30-minute watchdog when the last
    # one-case run completed cleanly within six minutes. Slow/error runs fall
    # back to the caller's one-case limit automatically.
    return 4 if hard_failures == 0 and elapsed <= 360 else requested


def _fair_all_case_chunk_limit(requested: int, configured_chunk_size: int) -> int:
    """Keep the all-case cursor fair: one terminal case chunk per occurrence.

    A complete per-case comparison can consume most of the Drive/NAS deadline.
    Letting a four-case inventory transaction run under one alarm meant that a
    slow first case could prevent *any* durable cursor progress.  The caller
    may ask for a larger inventory slice, but the write-capable all-files job
    deliberately commits one canonical case at a time.
    """
    return max(1, min(int(requested or 1), int(configured_chunk_size or 1), 1))


def _inner_inventory_budget(total_seconds: int, headroom_seconds: int) -> int:
    """Reserve time for atomic terminal receipt/state writes and lock release."""
    total = max(0, int(total_seconds or 0))
    headroom = max(0, int(headroom_seconds or 0))
    if total <= headroom:
        return 0
    return total - headroom


class DriveCaseSyncTimeout(DriveCaseSyncDeadline):
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
    checkpoint_summary = (
        _ACTIVE_FILE_CHECKPOINT.public_summary()
        if _ACTIVE_FILE_CHECKPOINT is not None
        else dict(ctx.get("file_checkpoint") or {})
    )
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
        "phase": str(checkpoint_summary.get("phase") or ctx.get("phase") or ""),
        "file_checkpoint": checkpoint_summary,
        "message": "Drive/NAS 同步被外層 watchdog 或系統訊號中止；狀態已落盤，下次排程會從保存的 offset 重試。",
    }


def _publish_file_checkpoint_progress(summary: dict[str, Any]) -> None:
    """Publish only token/count progress; private locators remain in reports."""

    ctx = _CURRENT_RUN_CONTEXT
    if not isinstance(summary, dict) or not ctx:
        return
    public = {
        "schema_version": int(summary.get("schema_version") or 0),
        "case_token": str(summary.get("case_token") or ""),
        "snapshot_hash": str(summary.get("snapshot_hash") or ""),
        "phase": str(summary.get("phase") or ""),
        "checkpoint_seq": int(summary.get("checkpoint_seq") or 0),
        "last_progress_at": str(summary.get("last_progress_at") or ""),
        "hash_cached_count": int(summary.get("hash_cached_count") or 0),
        "completed_count": int(summary.get("completed_count") or 0),
        "partial_count": int(summary.get("partial_count") or 0),
        "partial_bytes": int(summary.get("partial_bytes") or 0),
        "case_complete": summary.get("case_complete") is True,
        "case_terminal_deferred": summary.get("case_terminal_deferred") is True,
    }
    ctx["phase"] = public["phase"]
    ctx["file_checkpoint"] = public
    now_mono = time.monotonic()
    previous_phase = str(_LAST_CHECKPOINT_PUBLICATION.get("phase") or "")
    previous_at = float(_LAST_CHECKPOINT_PUBLICATION.get("monotonic") or 0.0)
    should_publish = bool(
        public["phase"] != previous_phase
        or public["case_complete"]
        or public["case_terminal_deferred"]
        or now_mono - previous_at >= 15.0
    )
    if not should_publish:
        return
    _LAST_CHECKPOINT_PUBLICATION.update(
        {"phase": public["phase"], "monotonic": now_mono}
    )
    kind = str(ctx.get("worker_kind") or "")
    save_worker_status(
        {
            "ok": None,
            "status": (
                "direct_all_case_sync_running"
                if kind == "all_files"
                else "direct_priority_sync_running"
                if kind == "priority"
                else "inventory_running"
            ),
            "action_required": False,
            "pid": os.getpid(),
            "worker_kind": kind,
            "started_at": str(ctx.get("started_at") or ""),
            "matched_case_offset": int(ctx.get("matched_case_offset") or 0),
            "all_case_offset": int(ctx.get("all_case_offset") or 0),
            "all_case_total": int(ctx.get("all_case_total") or 0),
            "phase": public["phase"],
            "file_checkpoint": public,
        },
        kind=kind,
    )


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


_WORKER_KINDS = frozenset({"all_files", "priority", "inventory"})
_WORKER_LOCK_KIND_PREFIX = "drive_case_sync:"


def _worker_kind_from_lock_metadata(metadata: object) -> str:
    """Return only a recognized aggregate worker kind from lock metadata.

    Old PID-only locks and generic singleton metadata cannot safely establish
    that a contender is the same operation.  They intentionally return an
    empty value so callers defer rather than incorrectly clear a retry.
    """
    if not isinstance(metadata, dict):
        return ""
    declared = str(metadata.get("worker_kind") or "").strip().lower()
    if declared in _WORKER_KINDS:
        return declared
    kind = str(metadata.get("kind") or "").strip().lower()
    if kind.startswith(_WORKER_LOCK_KIND_PREFIX):
        candidate = kind.removeprefix(_WORKER_LOCK_KIND_PREFIX)
        if candidate in _WORKER_KINDS:
            return candidate
    return ""


def _lock_owner_metadata(path: Path) -> dict:
    """Read lock metadata best-effort; never expose it in public status."""
    try:
        return read_json(metadata_path(path))
    except Exception:
        return {}


def acquire_worker_lock(worker_kind: str) -> dict:
    """Acquire a real PID lock so scheduled Drive/NAS sync cannot overlap.

    The status JSON is intentionally not used as a lock because short scheduled
    jobs can overwrite it while a longer manual/full sync is still running.
    """
    global _WORKER_LOCK_HANDLE
    normalized_kind = str(worker_kind or "").strip().lower()
    if normalized_kind not in _WORKER_KINDS:
        raise ValueError("unsupported drive sync worker kind")
    path = worker_lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    current_pid = os.getpid()
    previous_pid = _read_worker_lock_pid(path)
    stale: dict = {}
    if previous_pid and previous_pid != current_pid and _pid_is_alive(previous_pid):
        active_kind = _worker_kind_from_lock_metadata(_lock_owner_metadata(path))
        return {
            "acquired": False,
            "status": "already_running",
            "active_pid": previous_pid,
            "lock_path": str(path),
            "legacy_pid_file": True,
            "active_worker_kind": active_kind,
        }
    lock = acquire_lock(
        "drive_case_sync_worker",
        owner="drive_case_sync_worker",
        # The lock metadata intentionally records an enum only.  A contender
        # may treat an already-running owner as success solely when this exact
        # aggregate kind matches; otherwise it must remain retryable.
        kind=f"{_WORKER_LOCK_KIND_PREFIX}{normalized_kind}",
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
            "active_worker_kind": _worker_kind_from_lock_metadata(lock.active_owner),
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
    apply: bool = False,
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


def _schedule_fixture_manifest() -> tuple[Path, dict] | None:
    """Load a fixture-owned Drive/NAS provider for release certification only."""
    raw_path = str(os.environ.get("MAGI_DRIVE_SYNC_FIXTURE_PATH") or "").strip()
    if not raw_path:
        return None
    fixture_root_raw = str(os.environ.get("MAGI_V3_SCHEDULE_FIXTURE_ROOT") or "").strip()
    if (
        os.environ.get("MAGI_V3_SCHEDULE_ADAPTER") != "real_entrypoint_fixture_v1"
        or os.environ.get("MAGI_V3_SCHEDULE_DRY_RUN") != "1"
        or not fixture_root_raw
    ):
        raise RuntimeError("Drive/NAS certification fixture is not safely bound")
    fixture_root = Path(fixture_root_raw).expanduser().resolve()
    manifest_path = Path(raw_path).expanduser().resolve()
    if (
        not (fixture_root / ".magi-v3-schedule-fixture").is_file()
        or not manifest_path.is_file()
        or not manifest_path.is_relative_to(fixture_root)
    ):
        raise RuntimeError("Drive/NAS certification fixture escaped its owned root")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Drive/NAS certification fixture is unreadable") from exc
    cases = manifest.get("cases")
    if manifest.get("schema") != "magi.v3.drive-case-sync-fixture/v1" or not isinstance(cases, list):
        raise RuntimeError("Drive/NAS certification fixture schema is invalid")
    if not cases or len(cases) > 4 or any(not isinstance(item, dict) for item in cases):
        raise RuntimeError("Drive/NAS certification fixture cases are invalid")
    return fixture_root, manifest


def _fixture_file_map(case_root: Path) -> dict[str, dict]:
    files: dict[str, dict] = {}
    for path in sorted(case_root.rglob("*")):
        if path.is_symlink():
            raise RuntimeError("Drive/NAS certification fixture contains a symlink")
        if not path.is_file():
            continue
        relative = path.relative_to(case_root).as_posix()
        if len(files) >= 20:
            raise RuntimeError("Drive/NAS certification fixture exceeded its file bound")
        data = path.read_bytes()
        files[relative] = {
            "path": str(path),
            "relative_path": relative,
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }
    return files


def run_schedule_fixture_sync(*, worker_kind: str) -> dict | None:
    """Run real naming/mapping/dedup rules over bounded local provider trees.

    No provider result is trusted as success: every path and file is inspected,
    native NAS/Drive mappings are recomputed, and the filesystem inventory is
    hashed before/after to prove the dry-run created no folders or files.
    """
    loaded = _schedule_fixture_manifest()
    if loaded is None:
        return None
    fixture_root, manifest = loaded
    before = {
        path.relative_to(fixture_root).as_posix(): (
            "symlink" if path.is_symlink() else "dir" if path.is_dir() else hashlib.sha256(path.read_bytes()).hexdigest()
        )
        for path in sorted(fixture_root.rglob("*"))
    }
    case_reports: list[dict] = []
    summary = {
        "matched_cases": 0,
        "nas_missing_in_drive_files": 0,
        "drive_missing_in_nas_files": 0,
        "skipped_existing_files": 0,
        "blocked_invalid_drive_paths": 0,
        "created_folders": 0,
        "errors": 0,
    }
    for item in manifest["cases"]:
        case_number = str(item.get("case_number") or "").strip()
        nas_case = (fixture_root / str(item.get("nas_case") or "")).resolve()
        drive_case = (fixture_root / str(item.get("drive_case") or "")).resolve()
        if (
            not case_number
            or not nas_case.is_relative_to(fixture_root)
            or not drive_case.is_relative_to(fixture_root)
            or not nas_case.is_dir()
            or not drive_case.is_dir()
        ):
            raise RuntimeError("Drive/NAS certification case roots are invalid")
        local_case = CaseFolder(
            source="local",
            path=str(nas_case),
            local_path=str(nas_case),
            relative_path=nas_case.relative_to(fixture_root).as_posix(),
            name=nas_case.name,
            category=str(item.get("category") or ""),
            case_kind=str(item.get("case_kind") or ""),
            status=str(item.get("status") or "active"),
            meta=CaseMeta(
                case_number=case_number,
                laf_case_no=str(item.get("laf_case_no") or ""),
                client_hint=str(item.get("client_name") or ""),
                reason_hint=str(item.get("case_reason") or ""),
            ),
        )
        expected_drive_relative = drive_relative_path_for_local_case(
            local_case,
            owner_bucket=str(item.get("owner_bucket") or "Lumi"),
        )
        actual_drive_relative = str(item.get("drive_relative") or "").strip("/")
        drive_rule_ok = bool(expected_drive_relative) and actual_drive_relative == expected_drive_relative
        nas_files = _fixture_file_map(nas_case)
        drive_files = _fixture_file_map(drive_case)
        drive_by_nas_key: dict[str, dict] = {}
        invalid_drive_paths: list[dict] = []
        for relative, record in drive_files.items():
            target = drive_to_nas_relative_path(
                relative,
                case_category=local_case.category,
                case_context_name=local_case.name,
            )
            reason = drive_to_nas_download_skip_reason(
                relative,
                target,
                case_category=local_case.category,
            )
            if reason:
                invalid_drive_paths.append({"path": relative, "reason": reason})
                summary["blocked_invalid_drive_paths"] += 1
                continue
            drive_by_nas_key[target] = record
        existing: list[dict] = []
        downloads: list[dict] = []
        uploads: list[dict] = []
        for relative, record in nas_files.items():
            drive_relative = nas_to_drive_relative_path(relative)
            skip_reason = nas_to_drive_upload_skip_reason(relative, drive_relative)
            counterpart = drive_by_nas_key.get(relative)
            if counterpart and counterpart["sha256"] == record["sha256"]:
                existing.append({"nas": relative, "drive": counterpart["relative_path"]})
                summary["skipped_existing_files"] += 1
            elif not counterpart and not skip_reason:
                uploads.append({"source": relative, "target": drive_relative})
                summary["nas_missing_in_drive_files"] += 1
        for relative, record in drive_by_nas_key.items():
            if relative not in nas_files:
                downloads.append({"source": record["relative_path"], "target": relative})
                summary["drive_missing_in_nas_files"] += 1
        errors = []
        if not drive_rule_ok:
            errors.append("drive_native_folder_rule_mismatch")
        if case_number not in nas_case.name:
            errors.append("nas_case_folder_missing_osc_number")
        summary["matched_cases"] += 1
        summary["errors"] += len(errors)
        case_reports.append(
            {
                "case_number": case_number,
                "nas_case": str(nas_case),
                "drive_case": str(drive_case),
                "expected_drive_relative": expected_drive_relative,
                "actual_drive_relative": actual_drive_relative,
                "drive_rule_ok": drive_rule_ok,
                "existing": existing,
                "downloads": downloads,
                "uploads": uploads,
                "invalid_drive_paths": invalid_drive_paths,
                "errors": errors,
            }
        )
    for raw in manifest.get("invalid_drive_paths") or []:
        reason = drive_case_sync_exclusion_reason(str(raw), require_canonical_layout=True)
        if not reason:
            summary["errors"] += 1
        else:
            summary["blocked_invalid_drive_paths"] += 1
    after = {
        path.relative_to(fixture_root).as_posix(): (
            "symlink" if path.is_symlink() else "dir" if path.is_dir() else hashlib.sha256(path.read_bytes()).hexdigest()
        )
        for path in sorted(fixture_root.rglob("*"))
    }
    inventory_unchanged = before == after
    ok = summary["errors"] == 0 and inventory_unchanged
    return {
        "ok": ok,
        "status": "ok" if ok else "fixture_validation_failed",
        "mode": "fixture_bidirectional_dry_run",
        "worker_kind": worker_kind,
        "summary": summary,
        "cases": case_reports,
        "inventory_unchanged": inventory_unchanged,
        "provider_quality_certified": False,
        "provider_role": "bounded_drive_and_nas_filesystem_fixture",
    }


def resolve_write_intent(args: argparse.Namespace) -> dict[str, bool]:
    """Require explicit positive flags for every NAS/Drive mutation."""
    return {
        "execute_downloads": bool(
            getattr(args, "execute_downloads", False)
            and not getattr(args, "no_downloads", False)
        ),
        "execute_uploads": bool(
            getattr(args, "execute_uploads", False)
            and not getattr(args, "no_uploads", False)
        ),
        "ensure_drive_case_folders": bool(
            getattr(args, "ensure_drive_case_folders", False)
            and not getattr(args, "no_create_drive_folders", False)
        ),
    }


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


def load_priority_case_numbers(days: int, *, limit: int = 80) -> tuple[list[str], bool]:
    """Return upcoming case numbers plus whether DB enumeration was reliable."""
    if days <= 0:
        return [], True
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "osc-orchestrator"))
        from osc_headless.db import connect_mysql, db_config_from_env  # type: ignore
    except Exception:
        return [], False
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
            return (
                [str(row[0] or "").strip() for row in (cur.fetchall() or []) if str(row[0] or "").strip()],
                True,
            )
        finally:
            cur.close()
    except Exception:
        return [], False
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


def _all_case_offset_after_run(
    current_offset: int,
    enumerated_next_offset: int,
    *,
    has_partial_failures: bool,
) -> int:
    """Retry the same DB slice when its bounded sync did not finish safely."""
    if has_partial_failures:
        return max(0, int(current_offset or 0))
    return max(0, int(enumerated_next_offset or 0))


def _semantic_collision_only_wait(report: dict, has_partial_failures: bool) -> bool:
    """Return True only for a fail-closed path-alias ambiguity.

    This is not a completed sync, but it also is not a crashed provider or a
    failed write.  Keeping it as a deferred occurrence lets the rotating
    all-case worker advance instead of hammering the same blocked case forever.
    """

    if not has_partial_failures:
        return False
    file_plan = report.get("file_sync_plan") or {}
    file_summary = file_plan.get("summary") or {}
    cases = file_plan.get("cases") or []
    try:
        collision_files = int(file_summary.get("semantic_collision_files") or 0)
        case_errors = int(file_summary.get("case_errors") or 0)
        incomplete_scans = int(file_summary.get("incomplete_case_scans") or 0)
        pending_unverified = int(file_summary.get("pending_unverified_files") or 0)
    except (TypeError, ValueError):
        return False
    collision_cases = [
        case
        for case in cases
        if isinstance(case, dict)
        and str(case.get("error") or "") == "semantic_path_collision"
    ]
    non_collision_errors = [
        case
        for case in cases
        if isinstance(case, dict)
        and str(case.get("error") or "")
        and str(case.get("error") or "") != "semantic_path_collision"
    ]
    execution = (report.get("execution_result") or {}).get("summary") or {}
    folders = (report.get("drive_folder_result") or {}).get("summary") or {}
    repairs = [
        (report.get("duplicate_repair_result") or {}).get("summary") or {},
        (report.get("local_duplicate_repair_result") or {}).get("summary") or {},
        (report.get("drive_imported_folder_repair") or {}).get("summary") or {},
    ]
    hard_execution_keys = (
        "failed",
        "download_failed",
        "upload_failed",
        "pending_unverified",
        "download_pending_unverified",
        "upload_pending_unverified",
    )
    hard_execution = any(
        int(execution.get(key) or 0) > 0 for key in hard_execution_keys
    ) or bool(execution.get("stopped_by_limit") or execution.get("stopped_by_bytes"))
    hard_repairs = int(folders.get("failed") or 0) > 0 or any(
        int(summary.get("failed") or 0) > 0
        or int(summary.get("errors") or 0) > 0
        or int(summary.get("case_errors") or 0) > 0
        for summary in repairs
    )
    return bool(
        collision_files > 0
        and case_errors == len(collision_cases) > 0
        and not non_collision_errors
        and incomplete_scans == 0
        and pending_unverified == 0
        and not hard_execution
        and not hard_repairs
    )


def _storage_unavailable_wait(report: dict) -> bool:
    """Recognize an interrupted NAS write pass that is safe to retry later."""
    execution = (report.get("execution_result") or {}).get("summary") or {}
    try:
        storage_unavailable = int(
            execution.get("download_storage_unavailable")
            or execution.get("upload_storage_unavailable")
            or execution.get("storage_unavailable")
            or 0
        )
    except (TypeError, ValueError):
        return False
    upload_storage_wait = storage_unavailable > 0 and all(
        int(execution.get(key) or 0) == 0
        for key in ("failed", "download_failed", "upload_failed")
    )
    if upload_storage_wait:
        return True

    # A reconnecting smbfs mount can enumerate a case and then temporarily
    # reject the following read/stat.  No write was attempted in this state;
    # keep the same checkpoint and retry later instead of presenting a hard
    # failure.  Hash limits and content mismatches remain blocking.
    file_plan = report.get("file_sync_plan") or {}
    file_summary = file_plan.get("summary") or {}
    pending = [
        item
        for case in (file_plan.get("cases") or [])
        if isinstance(case, dict)
        for item in (case.get("pending") or [])
        if isinstance(item, dict)
    ]
    safe_hash_reasons = {
        f"local_hash_failed:{code}"
        for code in LOCAL_HASH_RETRYABLE_FAILURE_CODES
    }
    try:
        pending_count = int(file_summary.get("pending_unverified_files") or 0)
        case_errors = int(file_summary.get("case_errors") or 0)
        semantic_case_errors = sum(
            1
            for case in (file_plan.get("cases") or [])
            if isinstance(case, dict)
            and str(case.get("error") or "") == "semantic_path_collision"
        )
        # A semantic alias collision is a no-write data-integrity guard, not a
        # storage failure.  Only forgive the aggregate when every reported
        # case error is exactly that guard; unknown/mixed errors stay red.
        nonsemantic_case_error = case_errors > 0 and case_errors != semantic_case_errors
        hard_plan_failure = any(
            int(file_summary.get(key) or 0) > 0
            for key in (
                "incomplete_case_scans",
                "conflict_files",
                "content_mismatch_files",
            )
        ) or nonsemantic_case_error
        hard_execution = any(
            int(execution.get(key) or 0) > 0
            for key in (
                "failed",
                "download_failed",
                "upload_failed",
                "download_pending_unverified",
                "upload_pending_unverified",
            )
        )
    except (TypeError, ValueError):
        return False
    return bool(
        pending_count > 0
        and len(pending) == pending_count
        and all(str(item.get("reason") or "") in safe_hash_reasons for item in pending)
        and not hard_plan_failure
        and not hard_execution
    )


def _status_execution_summary(report: dict) -> dict:
    """Return execution evidence with the safe existing-file conflict count.

    A Drive object without an MD5 is deliberately *not* overwritten.  The
    upload manifest records that condition as ``pending_existing_conflict``;
    preserve an aggregate in the compact worker receipt so health evaluation
    does not need to expose individual case/file entries to decide whether the
    result is a review queue or an actual transfer failure.
    """

    execution_result = report.get("execution_result") or {}
    summary = dict(execution_result.get("summary") or {})
    upload_result = execution_result.get("upload_result") or {}
    manifest = upload_result.get("manifest") or []
    summary["upload_pending_existing_checksum_missing_conflict"] = sum(
        1
        for item in manifest
        if isinstance(item, dict)
        and str(item.get("status") or "") == "pending_existing_conflict"
        and str(item.get("reason") or "") == "drive_existing_checksum_missing"
    )
    return summary


def _status_file_sync_summary(report: dict) -> dict:
    """Return file-plan evidence with a PII-free existing-checksum aggregate.

    The plan can identify a Drive item that already exists locally but omits
    its checksum before any transfer is attempted.  Count only that explicit,
    non-destructive condition; hash failures, exports, mismatches and every
    other pending reason intentionally remain outside this exception.
    """

    file_plan = report.get("file_sync_plan") or {}
    summary = dict(file_plan.get("summary") or {})
    safe_reasons = {
        "drive_checksum_missing",
        "drive_existing_checksum_missing",
    }
    summary["pending_existing_checksum_missing_conflict"] = sum(
        1
        for case in (file_plan.get("cases") or [])
        if isinstance(case, dict)
        for item in (case.get("pending") or [])
        if isinstance(item, dict)
        and str(item.get("status") or "").startswith("pending_")
        and str(item.get("reason") or "") in safe_reasons
    )
    summary["smb_hash_storage_unavailable_files"] = sum(
        1
        for case in (file_plan.get("cases") or [])
        if isinstance(case, dict)
        for item in (case.get("pending") or [])
        if isinstance(item, dict)
        and str(item.get("reason") or "")
        .startswith("local_hash_failed:")
        and str(item.get("reason") or "").split(":", 1)[-1]
        in {"local_hash_smb_helper_storage_unavailable", "smb_stage_storage_unavailable"}
    )
    return summary


def _data_integrity_review_wait(report: dict, has_partial_failures: bool) -> bool:
    """Recognize the single bounded conflict that must not be retried blindly.

    The matching existing Drive object has no checksum, so an automatic retry
    cannot establish equality and must not overwrite it.  This returns false
    for every other pending item (including an SMB/local-hash problem), which
    preserves the normal retry-and-red path.
    """

    if not has_partial_failures:
        return False
    file_summary = _status_file_sync_summary(report)
    execution_summary = _status_execution_summary(report)
    try:
        pending_total = (
            int(execution_summary.get("download_pending_unverified") or 0)
            + int(execution_summary.get("upload_pending_unverified") or 0)
            + max(
                int(file_summary.get("pending_unverified_files") or 0),
                int(file_summary.get("unverified_existing_files") or 0),
            )
        )
        safe_total = (
            int(execution_summary.get("upload_pending_existing_checksum_missing_conflict") or 0)
            + int(file_summary.get("pending_existing_checksum_missing_conflict") or 0)
        )
        semantic_collisions = int(file_summary.get("semantic_collision_files") or 0)
        hard_errors = any(
            int((execution_summary.get(key) or 0)) > 0
            for key in (
                "failed", "download_failed", "upload_failed",
                "download_storage_unavailable", "upload_storage_unavailable",
            )
        ) or any(
            int((file_summary.get(key) or 0)) > 0
            for key in ("incomplete_case_scans", "storage_unavailable_case_scans")
        )
    except (TypeError, ValueError):
        return False
    folders = (report.get("drive_folder_result") or {}).get("summary") or {}
    repairs = [
        (report.get("duplicate_repair_result") or {}).get("summary") or {},
        (report.get("local_duplicate_repair_result") or {}).get("summary") or {},
        (report.get("drive_imported_folder_repair") or {}).get("summary") or {},
    ]
    folder_or_repair_error = int(folders.get("failed") or 0) > 0 or any(
        int(summary.get(key) or 0) > 0
        for summary in repairs
        for key in ("failed", "errors", "case_errors")
    )
    return bool(
        semantic_collisions > 0
        and safe_total > 0
        and pending_total == safe_total
        and not hard_errors
        and not folder_or_repair_error
    )


def main(argv: list[str] | None = None) -> int:
    global _ACTIVE_FILE_CHECKPOINT
    _LAST_CHECKPOINT_PUBLICATION.clear()
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
    parser.add_argument("--all-case-chunk-size", type=int, default=1)
    parser.add_argument("--inventory-timeout-sec", type=int, default=1200)
    parser.add_argument("--terminal-headroom-sec", type=int, default=300)
    parser.add_argument("--execute-downloads", action="store_true")
    parser.add_argument("--execute-uploads", action="store_true")
    parser.add_argument("--ensure-drive-case-folders", action="store_true")
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
    write_intent = resolve_write_intent(args)
    execute_downloads = write_intent["execute_downloads"]
    execute_uploads = write_intent["execute_uploads"]
    ensure_drive_case_folders = write_intent["ensure_drive_case_folders"]
    worker_kind = "all_files" if args.direct_all_cases else ("priority" if not args.no_direct_priority_sync else "inventory")

    worker_lock = acquire_worker_lock(worker_kind)
    if not worker_lock.get("acquired"):
        lock_status = str(worker_lock.get("status") or "already_running")
        active_worker_kind = str(worker_lock.get("active_worker_kind") or "")
        same_operation_owner = (
            lock_status == "already_running"
            and active_worker_kind == worker_kind
        )
        owner_kind_conflict = lock_status == "already_running" and not same_operation_owner
        precise_fail = lock_status != "already_running"
        deferred = bool(owner_kind_conflict)
        retryable = bool(owner_kind_conflict)
        status = {
            "ok": same_operation_owner,
            "status": "deferred_owner_kind_conflict" if owner_kind_conflict else lock_status,
            "action_required": precise_fail,
            "deferred": deferred,
            "retryable": retryable,
            "reason": (
                "different_worker_kind_owner"
                if active_worker_kind and owner_kind_conflict
                else ("owner_worker_kind_unclassified" if owner_kind_conflict else "already_running_same_worker_kind")
            ),
            "pid": os.getpid(),
            "active_worker_pid": worker_lock.get("active_pid"),
            "active_worker_kind": active_worker_kind or "unclassified",
            "lock_path": worker_lock.get("lock_path") or "",
            "started_at": iso_now(),
            "finished_at": iso_now(),
            "message": (
                "Drive/NAS worker lock 被持有，但找不到活的 owner metadata；本次精準失敗，避免誤判排程成功。"
                if precise_fail
                else (
                    "Drive/NAS 同步相同作業已在執行中，本次安全略過，不重複啟動工作者。"
                    if same_operation_owner
                    else "Drive/NAS 同步由不同或未分類作業持有；本次保留續跑，不重複啟動或清除原有狀態。"
                )
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
        # A healthy lock contender is not the owner and must not replace the
        # owner's live/latest evidence with its own short-lived PID.  Keep the
        # contention receipt in the dedicated skip file above; the owner will
        # continue updating the canonical status.  Unknown-owner contention is
        # a real fault and remains visible in the canonical health state.
        if precise_fail:
            save_worker_status(status, kind=worker_kind)
        print(json.dumps(status, ensure_ascii=False, indent=2))
        if deferred:
            return 75
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

    try:
        fixture_report = run_schedule_fixture_sync(worker_kind=worker_kind)
    except Exception as exc:
        fixture_report = {
            "ok": False,
            "status": "fixture_validation_failed",
            "worker_kind": worker_kind,
            "error": f"{type(exc).__name__}: {exc}",
            "provider_quality_certified": False,
        }
    if fixture_report is not None:
        terminal = {
            **fixture_report,
            "pid": os.getpid(),
            "started_at": iso_now(),
            "finished_at": iso_now(),
            "action_required": not bool(fixture_report.get("ok")),
        }
        save_worker_status(terminal, kind=worker_kind)
        print(json.dumps(terminal, ensure_ascii=False, indent=2))
        release_case_file_operation_lock()
        _release_worker_lock()
        return 0 if terminal.get("ok") else 1

    state = load_state()
    offset = max(0, int(state.get("matched_case_offset") or 0))
    all_case_offset = max(0, int(state.get("all_case_offset") or 0))
    priority_case_numbers, priority_enumeration_ok = load_priority_case_numbers(
        args.priority_upcoming_days,
        limit=args.priority_case_limit,
    )
    if (
        not priority_enumeration_ok
        and not args.direct_all_cases
        and any((execute_downloads, execute_uploads, ensure_drive_case_folders))
    ):
        status = {
            "ok": False,
            "status": "priority_case_enumeration_failed",
            "action_required": True,
            "pid": os.getpid(),
            "worker_kind": worker_kind,
            "started_at": iso_now(),
            "finished_at": iso_now(),
            "message": (
                "優先案件 DB 枚舉失敗；已精準停止寫入，"
                "不降級成 unrestricted inventory，避免錯建或錯放 NAS/Drive 資料夾。"
            ),
        }
        state["last_status"] = status
        state["last_summary"] = {"priority_case_enumeration_failed": True}
        save_state(state, kind=worker_kind)
        save_worker_status(status, kind=worker_kind)
        print(json.dumps(status, ensure_ascii=False, indent=2))
        release_case_file_operation_lock()
        _release_worker_lock()
        return 2
    all_case_numbers: list[str] = []
    all_case_total = 0
    all_case_next_offset = all_case_offset
    effective_all_case_limit = _adaptive_all_case_limit(args.direct_all_case_limit, state)
    all_case_chunk_limit = _fair_all_case_chunk_limit(
        effective_all_case_limit,
        args.all_case_chunk_size,
    )
    if args.direct_all_cases:
        all_case_numbers, all_case_total, all_case_next_offset = load_all_sync_case_numbers(
            limit=all_case_chunk_limit,
            offset=all_case_offset,
        )
        if not all_case_numbers:
            status = {
                "ok": False,
                "status": "all_case_enumeration_empty",
                "action_required": True,
                "pid": os.getpid(),
                "worker_kind": worker_kind,
                "started_at": iso_now(),
                "finished_at": iso_now(),
                "all_case_offset": all_case_offset,
                "all_case_total": all_case_total,
                "message": (
                    "全案件同步未取得 DB canonical case 清單；已精準停止，"
                    "不降級成可寫入的全樹 inventory，避免錯建或錯放 NAS/Drive 資料夾。"
                ),
            }
            state["last_status"] = status
            state["last_summary"] = {"all_case_enumeration_empty": True}
            save_state(state, kind=worker_kind)
            save_worker_status(status, kind=worker_kind)
            print(json.dumps(status, ensure_ascii=False, indent=2))
            release_case_file_operation_lock()
            _release_worker_lock()
            return 2
    direct_numbers = all_case_numbers if args.direct_all_cases else priority_case_numbers[: max(0, int(args.direct_priority_case_limit or 0))]
    direct_mode_requested = bool(direct_numbers and (args.direct_all_cases or not args.no_direct_priority_sync))
    direct_mode_label = "direct_all_case_sync_running" if args.direct_all_cases else "direct_priority_sync_running"
    needs_write_scope = execute_uploads or ensure_drive_case_folders
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
    _ACTIVE_FILE_CHECKPOINT = None
    if args.direct_all_cases and all_case_numbers and (execute_downloads or execute_uploads):
        try:
            _ACTIVE_FILE_CHECKPOINT = DriveFileCheckpoint(
                file_checkpoint_path(worker_kind),
                case_key=drive_checkpoint_case_token(worker_kind, all_case_numbers[0]),
                on_progress=_publish_file_checkpoint_progress,
            )
            checkpoint_public = _ACTIVE_FILE_CHECKPOINT.public_summary()
            _CURRENT_RUN_CONTEXT["phase"] = checkpoint_public.get("phase") or "starting"
            _CURRENT_RUN_CONTEXT["file_checkpoint"] = checkpoint_public
        except DriveFileCheckpointError as exc:
            status = {
                "ok": False,
                "success": False,
                "status": "file_checkpoint_invalid",
                "reason": str(exc),
                "action_required": True,
                "pid": os.getpid(),
                "worker_kind": worker_kind,
                "started_at": started_at,
                "finished_at": iso_now(),
                "all_case_offset_before": all_case_offset,
                "all_case_offset_after": all_case_offset,
                "cycle_completed": False,
                "message": "案件內檔案 checkpoint 無法安全驗證；已停止且未前進案件 cursor。",
            }
            state["last_status"] = status
            save_state(state, kind=worker_kind)
            save_worker_status(status, kind=worker_kind)
            print(json.dumps(status, ensure_ascii=False, indent=2))
            release_case_file_operation_lock()
            _release_worker_lock()
            return 2
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
            "direct_all_case_limit": effective_all_case_limit,
            "direct_all_case_limit_requested": args.direct_all_case_limit,
            "all_case_chunk_limit": all_case_chunk_limit,
            "download_limit": args.download_limit if execute_downloads else 0,
            "upload_limit": args.upload_limit if execute_uploads else 0,
            "max_case_depth": args.max_case_depth,
            "max_case_items": args.max_case_items,
            "inventory_timeout_sec": args.inventory_timeout_sec,
            "terminal_headroom_sec": args.terminal_headroom_sec,
            "repair_local_duplicates": bool(args.repair_local_duplicates or args.execute_local_duplicate_repair),
            "execute_local_duplicate_repair": bool(args.execute_local_duplicate_repair),
            "repair_local_duplicate_limit": args.repair_local_duplicate_limit,
        },
        "phase": str(_CURRENT_RUN_CONTEXT.get("phase") or ""),
        "file_checkpoint": dict(_CURRENT_RUN_CONTEXT.get("file_checkpoint") or {}),
    }, kind=worker_kind)
    inner_budget = _inner_inventory_budget(
        args.inventory_timeout_sec,
        args.terminal_headroom_sec,
    )
    if not inner_budget:
        status = {
            "ok": False,
            "success": False,
            "status": "invalid_time_budget",
            "action_required": True,
            "worker_kind": worker_kind,
            "started_at": started_at,
            "finished_at": iso_now(),
            "all_case_offset_before": all_case_offset,
            "all_case_offset_after": all_case_offset,
            "cycle_completed": False,
            "message": "同步時間預算未保留 terminal receipt 與釋鎖 headroom；已停止而未變更 cursor。",
        }
        state["last_status"] = status
        save_state(state, kind=worker_kind)
        save_worker_status(status, kind=worker_kind)
        print(json.dumps(status, ensure_ascii=False, indent=2))
        release_case_file_operation_lock()
        _release_worker_lock()
        return 2
    try:
        with inventory_time_limit(inner_budget):
            root_name = args.root_name or os.environ.get("MAGI_DRIVE_SYNC_ROOT_FOLDER_NAME", DEFAULT_DRIVE_ROOT_NAME)
            if direct_mode_requested:
                report = run_priority_case_sync(
                    case_numbers=direct_numbers,
                    root_id=args.root_id,
                    root_name=root_name,
                    file_diff=execute_downloads or execute_uploads,
                    execute_downloads=execute_downloads,
                    execute_uploads=execute_uploads,
                    download_limit=args.download_limit,
                    max_download_bytes=args.max_download_bytes,
                    upload_limit=args.upload_limit,
                    max_upload_bytes=args.max_upload_bytes,
                    max_case_depth=args.max_case_depth,
                    max_case_items=args.max_case_items,
                    ensure_drive_case_folders=ensure_drive_case_folders,
                    drive_owner_bucket_name=args.drive_owner_bucket,
                    repair_local_duplicates=args.repair_local_duplicates,
                    execute_local_duplicate_repair=args.execute_local_duplicate_repair,
                    repair_local_duplicate_limit=args.repair_local_duplicate_limit,
                    checkpoint=_ACTIVE_FILE_CHECKPOINT,
                )
            else:
                report = run_inventory(
                    root_id=args.root_id,
                    root_name=root_name,
                    max_depth=args.max_depth,
                    max_items=args.max_items,
                    resolve_context=not args.no_context_resolve,
                    file_diff=execute_downloads or execute_uploads,
                    execute_downloads=execute_downloads,
                    execute_uploads=execute_uploads,
                    download_limit=args.download_limit,
                    max_download_bytes=args.max_download_bytes,
                    upload_limit=args.upload_limit,
                    max_upload_bytes=args.max_upload_bytes,
                    max_case_depth=args.max_case_depth,
                    max_case_items=args.max_case_items,
                    matched_case_limit=args.matched_case_limit,
                    matched_case_offset=offset,
                    priority_case_numbers=priority_case_numbers,
                    ensure_drive_case_folders=ensure_drive_case_folders,
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
        if _ACTIVE_FILE_CHECKPOINT is not None:
            try:
                _ACTIVE_FILE_CHECKPOINT.set_phase("timeout")
            except DriveFileCheckpointError:
                pass
        checkpoint_public = (
            _ACTIVE_FILE_CHECKPOINT.public_summary()
            if _ACTIVE_FILE_CHECKPOINT is not None
            else {}
        )
        status = {
            "ok": False,
            "success": False,
            "status": "chunk_deadline_deferred",
            "deferred": True,
            "partial": False,
            "reason": "chunk_deadline",
            "action_required": False,
            "message": str(exc),
            "worker_kind": worker_kind,
            "started_at": started_at,
            "finished_at": iso_now(),
            "matched_case_offset_before": offset,
            "matched_case_offset_after": offset,
            "all_case_offset_before": all_case_offset,
            "all_case_offset_after": all_case_offset,
            "all_case_total": all_case_total,
            "chunk_completed": False,
            "cycle_completed": False,
            "inner_budget_sec": inner_budget,
            "terminal_headroom_sec": args.terminal_headroom_sec,
            "phase": str(checkpoint_public.get("phase") or "timeout"),
            "file_checkpoint": checkpoint_public,
            "all_case_count": len(all_case_numbers),
            "priority_case_count": len(priority_case_numbers),
            "next_step": "已在保留 headroom 內落盤 terminal receipt 並釋鎖；下次排程從同一個未完成 case chunk 續跑。",
        }
        state["last_status"] = status
        state["last_summary"] = {"timeout": True}
        state["last_priority_case_numbers"] = priority_case_numbers[:30]
        save_state(state, kind=worker_kind)
        save_worker_status(status, kind=worker_kind)
        print(json.dumps(status, ensure_ascii=False, indent=2))
        release_case_file_operation_lock()
        _release_worker_lock()
        return 75
    except DriveFileCheckpointError as exc:
        checkpoint_public = (
            _ACTIVE_FILE_CHECKPOINT.public_summary()
            if _ACTIVE_FILE_CHECKPOINT is not None
            else {}
        )
        status = {
            "ok": False,
            "success": False,
            "status": "file_checkpoint_invalid",
            "reason": str(exc),
            "action_required": True,
            "worker_kind": worker_kind,
            "started_at": started_at,
            "finished_at": iso_now(),
            "matched_case_offset_before": offset,
            "matched_case_offset_after": offset,
            "all_case_offset_before": all_case_offset,
            "all_case_offset_after": all_case_offset,
            "cycle_completed": False,
            "phase": str(checkpoint_public.get("phase") or ""),
            "file_checkpoint": checkpoint_public,
            "message": "案件內檔案 checkpoint 證據失效；已停止且未前進案件 cursor。",
        }
        state["last_status"] = status
        save_state(state, kind=worker_kind)
        save_worker_status(status, kind=worker_kind)
        print(json.dumps(status, ensure_ascii=False, indent=2))
        release_case_file_operation_lock()
        _release_worker_lock()
        return 2

    storage_unavailable_wait = _storage_unavailable_wait(report)
    direct_mode = report.get("mode") == "direct_db_case_sync"
    matched_total = int((report.get("summary") or {}).get("matched_case_folders") or 0)
    scanned = int(((report.get("file_sync_plan") or {}).get("summary") or {}).get("matched_cases_scanned") or 0)
    if storage_unavailable_wait:
        # Preserve the same checkpoint: a later schedule run can retry once
        # the NAS mount is back, without treating unscanned paths as missing.
        state["all_case_offset"] = all_case_offset
        state["matched_case_offset"] = offset
    elif direct_mode and args.direct_all_cases:
        # The case cursor is the final commit.  It remains pinned until every
        # file proof and the case-terminal checkpoint are durable below.
        state["all_case_offset"] = all_case_offset
        state["matched_case_offset"] = offset
    elif direct_mode:
        state["matched_case_offset"] = offset
    elif matched_total > 0:
        state["matched_case_offset"] = (offset + max(scanned, 1)) % matched_total
    else:
        state["matched_case_offset"] = 0
    state["last_output_paths"] = report.get("output_paths") or {}
    state["last_summary"] = report.get("summary") or {}
    state["last_file_sync_summary"] = _status_file_sync_summary(report)
    state["last_execution_summary"] = _status_execution_summary(report)
    state["last_drive_folder_summary"] = (report.get("drive_folder_result") or {}).get("summary") or {}
    repair_summary = run_imported_folder_repair(report, args)
    report["drive_imported_folder_repair"] = repair_summary
    state["last_drive_imported_folder_repair"] = repair_summary.get("summary") or repair_summary
    has_partial_failures = report_has_partial_failures(report)
    semantic_collision_wait = _semantic_collision_only_wait(
        report,
        has_partial_failures,
    )
    data_integrity_review_wait = _data_integrity_review_wait(
        report,
        has_partial_failures,
    )
    if direct_mode and args.direct_all_cases:
        hard_incomplete = bool(
            (has_partial_failures and not semantic_collision_wait and not data_integrity_review_wait)
            or storage_unavailable_wait
        )
        if not hard_incomplete:
            try:
                if _ACTIVE_FILE_CHECKPOINT is not None:
                    if data_integrity_review_wait:
                        _ACTIVE_FILE_CHECKPOINT.mark_case_deferred("data_integrity_review")
                    elif semantic_collision_wait:
                        _ACTIVE_FILE_CHECKPOINT.mark_case_deferred(
                            "semantic_path_collision_requires_human_review"
                        )
                    else:
                        _ACTIVE_FILE_CHECKPOINT.mark_case_complete()
            except DriveFileCheckpointError as exc:
                state["all_case_offset"] = all_case_offset
                status = {
                    "ok": False,
                    "success": False,
                    "status": "file_checkpoint_unverified",
                    "reason": str(exc),
                    "action_required": False,
                    "deferred": True,
                    "partial": False,
                    "worker_kind": worker_kind,
                    "started_at": started_at,
                    "finished_at": iso_now(),
                    "all_case_offset_before": all_case_offset,
                    "all_case_offset_after": all_case_offset,
                    "cycle_completed": False,
                    "phase": str(
                        (_ACTIVE_FILE_CHECKPOINT.public_summary().get("phase") if _ACTIVE_FILE_CHECKPOINT else "")
                        or ""
                    ),
                    "file_checkpoint": (
                        _ACTIVE_FILE_CHECKPOINT.public_summary()
                        if _ACTIVE_FILE_CHECKPOINT is not None
                        else {}
                    ),
                    "message": "案件內檔案尚未全部取得精確完成證據；保留同一案件 cursor 自動續跑。",
                }
                state["last_status"] = status
                save_state(state, kind=worker_kind)
                save_worker_status(status, kind=worker_kind)
                print(json.dumps(status, ensure_ascii=False, indent=2))
                release_case_file_operation_lock()
                _release_worker_lock()
                return 75
            state["all_case_offset"] = all_case_next_offset
        else:
            state["all_case_offset"] = all_case_offset
    state["last_priority_case_numbers"] = priority_case_numbers[:30]
    state["last_all_case_numbers"] = all_case_numbers[:30]
    cycle_completed = bool(
        direct_mode
        and args.direct_all_cases
        and not has_partial_failures
        and all_case_total > 0
        and state.get("all_case_offset") == 0
    )
    chunk_completed = bool(
        direct_mode and args.direct_all_cases and not has_partial_failures and not cycle_completed
    )
    terminal_status = (
        "deferred"
        if data_integrity_review_wait or semantic_collision_wait or storage_unavailable_wait
        else "partial_failure"
        if has_partial_failures
        else "cycle_completed"
        if cycle_completed
        else "chunk_completed"
        if chunk_completed
        else "ok"
    )
    success_status = {
        "ok": not has_partial_failures,
        "success": not has_partial_failures,
        "status": terminal_status,
        "deferred": data_integrity_review_wait or semantic_collision_wait or storage_unavailable_wait,
        "partial": bool(
            has_partial_failures
            and not data_integrity_review_wait
            and not semantic_collision_wait
            and not storage_unavailable_wait
        ),
        "reason": (
            "data_integrity_review"
            if data_integrity_review_wait
            else "semantic_path_collision_requires_human_review"
            if semantic_collision_wait
            else "storage_unavailable"
            if storage_unavailable_wait
            else ""
        ),
        "action_required": bool(
            has_partial_failures
            and not semantic_collision_wait
            and not storage_unavailable_wait
        ),
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
        "chunk_completed": chunk_completed,
        "cycle_completed": cycle_completed,
        "inner_budget_sec": inner_budget,
        "terminal_headroom_sec": args.terminal_headroom_sec,
        "mode": report.get("mode") or "",
        "message": (
            "既有 Drive 檔案缺少校驗碼，已安全封鎖覆寫並保留資料完整性確認。"
            if data_integrity_review_wait
            else "Drive/NAS 同步遇到語意路徑衝突，已安全封鎖寫入並留待確認。"
            if semantic_collision_wait
            else "NAS 儲存裝置中途失聯，已安全停止寫入並保留 checkpoint，將於下次排程重試。"
            if storage_unavailable_wait
            else
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
        "phase": str(
            (_ACTIVE_FILE_CHECKPOINT.public_summary().get("phase") if _ACTIVE_FILE_CHECKPOINT else "")
            or ""
        ),
        "file_checkpoint": (
            _ACTIVE_FILE_CHECKPOINT.public_summary()
            if _ACTIVE_FILE_CHECKPOINT is not None
            else {}
        ),
    }, kind=worker_kind)

    if (
        _ACTIVE_FILE_CHECKPOINT is not None
        and direct_mode
        and args.direct_all_cases
        and state.get("all_case_offset") == all_case_next_offset
    ):
        try:
            _ACTIVE_FILE_CHECKPOINT.discard_after_cursor_commit()
        except (DriveFileCheckpointError, OSError):
            # Cursor is already durable and the completed checkpoint is safe
            # to reconcile on the next run; cleanup must not strand the locks.
            pass

    print(json.dumps({
        "ok": not has_partial_failures,
        "success": not has_partial_failures,
        "status": terminal_status,
        "deferred": data_integrity_review_wait or semantic_collision_wait or storage_unavailable_wait,
        "partial": bool(
            has_partial_failures
            and not data_integrity_review_wait
            and not semantic_collision_wait
            and not storage_unavailable_wait
        ),
        "reason": (
            "data_integrity_review"
            if data_integrity_review_wait
            else "semantic_path_collision_requires_human_review"
            if semantic_collision_wait
            else "storage_unavailable"
            if storage_unavailable_wait
            else ""
        ),
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
    # Exit 75 means a bounded, retryable partial result.  It keeps duplicate
    # semantic-path collisions fail-closed without presenting a completed
    # safety scan as a crashed MAGI service.
    return 75 if (
        has_partial_failures
        and not data_integrity_review_wait
        and not semantic_collision_wait
        and not storage_unavailable_wait
    ) else 0


if __name__ == "__main__":
    raise SystemExit(main())
