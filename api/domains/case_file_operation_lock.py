"""Shared flock-backed PID locks for case-folder mutating jobs."""
from __future__ import annotations

import atexit
import os
from pathlib import Path
from typing import Any

from api.platforms import runtime_dir
from scripts.ops.background_task_locks import acquire_lock

_LOCK_HANDLES: dict[str, Any] = {}


def _pid_alive(pid: int) -> bool:
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


def _read_pid(path: Path) -> int:
    try:
        text = path.read_text(encoding="utf-8").strip()
        return int((text.splitlines() or ["0"])[0].strip() or "0")
    except Exception:
        return 0


def case_file_operation_lock_path(domain: str = "case_file_mutation") -> Path:
    safe_domain = "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in str(domain or "case_file_mutation"))
    return runtime_dir.root() / f"{safe_domain}.pid"


def acquire_case_file_operation_lock(
    *,
    owner: str,
    domain: str = "case_file_mutation",
    exclusive: bool = True,
) -> dict[str, Any]:
    """Acquire a shared domain lock for NAS/Drive case-folder mutations.

    This writes the legacy PID file for health checks and also holds a process
    ``flock`` with sidecar metadata.  Jobs that only read case folders should
    not use it; jobs that move, delete, upload, or repair case-folder files
    should acquire it before doing real writes.
    """

    if not exclusive:
        return {"acquired": True, "disabled": True, "owner": owner, "domain": domain}

    path = case_file_operation_lock_path(domain)
    path.parent.mkdir(parents=True, exist_ok=True)
    current_pid = os.getpid()
    previous_pid = _read_pid(path)
    if previous_pid and previous_pid != current_pid and _pid_alive(previous_pid):
        return {
            "acquired": False,
            "owner": owner,
            "domain": domain,
            "active_pid": previous_pid,
            "lock_path": str(path),
        }
    lock = acquire_lock(
        domain,
        owner=owner,
        kind="case_file_operation",
        path=path,
        blocking=False,
        write_pid_file=True,
    )
    if not lock.acquired:
        return {
            "acquired": False,
            "owner": owner,
            "domain": domain,
            "active_pid": int((lock.active_owner or {}).get("pid") or previous_pid or 0),
            "lock_path": str(path),
            "lock": lock.as_dict(),
        }
    stale = bool(previous_pid and previous_pid != current_pid)
    _LOCK_HANDLES[str(path)] = lock
    atexit.register(release_case_file_operation_lock, path=path, pid=current_pid)
    return {
        "acquired": True,
        "owner": owner,
        "domain": domain,
        "pid": current_pid,
        "stale_lock_cleared": stale,
        "lock_path": str(path),
        "lock": lock.as_dict(),
    }


def release_case_file_operation_lock(*, path: Path | None = None, pid: int | None = None, domain: str = "case_file_mutation") -> None:
    lock_path = path or case_file_operation_lock_path(domain)
    current_pid = int(pid or os.getpid())
    try:
        if _read_pid(lock_path) == current_pid:
            lock_path.unlink()
    except FileNotFoundError:
        pass
    except Exception:
        pass
    handle = _LOCK_HANDLES.pop(str(lock_path), None)
    if handle is not None:
        handle.release()
