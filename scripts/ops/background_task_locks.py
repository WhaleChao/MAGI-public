#!/usr/bin/env python3
"""Shared locks for MAGI background jobs.

These locks are intentionally process-level ``flock`` locks with sidecar JSON
metadata.  PID files alone are advisory and can be overwritten; ``flock`` gives
the scheduler and maintenance workers a verifiable owner while still releasing
automatically when a process dies.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import socket
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
LOCK_DIR_ENV = "MAGI_BACKGROUND_LOCK_DIR"

SCHEDULER_LOCK_NAME = "cron_scheduler_owner"
OSC_REFRESH_LOCK_NAME = "osc_calendar_todo_refresh"
CASE_FOLDER_OPS_LOCK_NAME = "case_folder_ops"


def _iso_now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def lock_dir() -> Path:
    override = os.environ.get(LOCK_DIR_ENV, "").strip()
    base = Path(override).expanduser() if override else ROOT / ".runtime" / "locks"
    base.mkdir(parents=True, exist_ok=True)
    return base


def lock_path(name: str) -> Path:
    safe = "".join(ch if ch.isalnum() or ch in {"_", "-", "."} else "_" for ch in str(name or "").strip())
    if not safe:
        raise ValueError("lock name must be non-empty")
    return lock_dir() / f"{safe}.lock"


def metadata_path(path: Path) -> Path:
    return Path(str(path) + ".json")


def read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def pid_is_alive(pid: int) -> bool:
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


def cleanup_stale_lock_metadata(lock_dirs: list[Path] | None = None, *, apply: bool = False) -> dict[str, Any]:
    """Clear stale owner metadata after confirming the flock itself is free."""
    roots = [lock_dir()] if lock_dirs is None else [Path(item) for item in lock_dirs]
    seen: set[str] = set()
    active: list[dict[str, Any]] = []
    stale: list[dict[str, Any]] = []
    cleaned: list[dict[str, Any]] = []
    malformed: list[dict[str, Any]] = []
    skipped_busy: list[dict[str, Any]] = []
    scanned = 0

    for root in roots:
        if not root.exists():
            continue
        for path in sorted(root.glob("*.lock")):
            key = str(path.resolve())
            if key in seen:
                continue
            seen.add(key)
            meta_path = metadata_path(path)
            if not meta_path.exists():
                continue
            scanned += 1
            data = read_json(meta_path)
            if not data:
                malformed.append({"path": str(path), "metadata_path": str(meta_path), "reason": "invalid_metadata"})
                continue
            try:
                pid = int(data.get("pid") or 0)
            except Exception:
                pid = 0
            item = {
                "path": str(path),
                "metadata_path": str(meta_path),
                "domain": data.get("domain") or path.stem,
                "owner": data.get("owner") or "",
                "pid": pid,
                "started_at": data.get("started_at") or "",
            }
            if pid and pid_is_alive(pid):
                active.append(item)
                continue

            try:
                fh = path.open("a+", encoding="utf-8")
            except OSError as exc:
                malformed.append({**item, "reason": f"open_failed: {exc}"})
                continue

            locked = False
            try:
                try:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    locked = True
                except BlockingIOError:
                    skipped_busy.append({**item, "reason": "flock_held"})
                    continue

                current = read_json(meta_path) or data
                try:
                    current_pid = int(current.get("pid") or 0)
                except Exception:
                    current_pid = 0
                current_item = {
                    **item,
                    "domain": current.get("domain") or item["domain"],
                    "owner": current.get("owner") or item["owner"],
                    "pid": current_pid,
                    "started_at": current.get("started_at") or item["started_at"],
                }
                if current_pid and pid_is_alive(current_pid):
                    active.append(current_item)
                    continue

                stale.append(current_item)
                if apply:
                    fh.seek(0)
                    fh.truncate()
                    fh.flush()
                    os.fsync(fh.fileno())
                    with contextlib.suppress(FileNotFoundError):
                        meta_path.unlink()
                    cleaned.append(current_item)
            finally:
                if locked:
                    with contextlib.suppress(Exception):
                        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                fh.close()

    return {
        "ok": not malformed and not skipped_busy,
        "apply": bool(apply),
        "scanned_count": scanned,
        "active_count": len(active),
        "stale_count": len(stale),
        "cleaned_count": len(cleaned),
        "malformed_count": len(malformed),
        "skipped_busy_count": len(skipped_busy),
        "active": active[:30],
        "stale": stale[:30],
        "cleaned": cleaned[:30],
        "malformed": malformed[:10],
        "skipped_busy": skipped_busy[:10],
    }


class BackgroundLock:
    def __init__(
        self,
        name: str,
        *,
        owner: str,
        kind: str = "",
        path: Path | None = None,
        blocking: bool = False,
        write_pid_file: bool = False,
    ) -> None:
        self.name = str(name)
        self.owner = str(owner)
        self.kind = str(kind or "")
        self.path = Path(path) if path is not None else lock_path(name)
        self.blocking = bool(blocking)
        self.write_pid_file = bool(write_pid_file)
        self.acquired = False
        self.metadata: dict[str, Any] = {}
        self.active_owner: dict[str, Any] = {}
        self._fh = None

    @property
    def meta_path(self) -> Path:
        return metadata_path(self.path)

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = self.path.open("a+", encoding="utf-8")
        flags = fcntl.LOCK_EX
        if not self.blocking:
            flags |= fcntl.LOCK_NB
        try:
            fcntl.flock(fh.fileno(), flags)
        except BlockingIOError:
            self.active_owner = read_json(self.meta_path)
            fh.close()
            return False
        meta = {
            "domain": self.name,
            "owner": self.owner,
            "kind": self.kind,
            "pid": os.getpid(),
            "hostname": socket.gethostname(),
            "started_at": _iso_now(),
            "lock_path": str(self.path),
            "metadata_path": str(self.meta_path),
        }
        if self.write_pid_file:
            fh.seek(0)
            fh.truncate()
            fh.write(f"{os.getpid()}\n")
            fh.flush()
            os.fsync(fh.fileno())
        else:
            fh.seek(0)
            fh.truncate()
            fh.write(json.dumps(meta, ensure_ascii=False, sort_keys=True) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        write_json_atomic(self.meta_path, meta)
        self._fh = fh
        self.metadata = meta
        self.acquired = True
        return True

    def release(self) -> None:
        if not self._fh:
            return
        try:
            try:
                current = read_json(self.meta_path)
                if int(current.get("pid") or 0) == os.getpid():
                    with contextlib.suppress(Exception):
                        self._fh.seek(0)
                        self._fh.truncate()
                        self._fh.flush()
                        os.fsync(self._fh.fileno())
                    with contextlib.suppress(FileNotFoundError):
                        self.meta_path.unlink()
            finally:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
                self._fh.close()
        finally:
            self._fh = None
            self.acquired = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "acquired": bool(self.acquired),
            "domain": self.name,
            "owner": self.owner,
            "kind": self.kind,
            "lock_path": str(self.path),
            "metadata_path": str(self.meta_path),
            "metadata": self.metadata,
            "active_owner": self.active_owner,
        }

    def __enter__(self) -> "BackgroundLock":
        self.acquire()
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.release()


def acquire_lock(
    name: str,
    *,
    owner: str,
    kind: str = "",
    blocking: bool = False,
    path: Path | None = None,
    write_pid_file: bool = False,
) -> BackgroundLock:
    lock = BackgroundLock(
        name,
        owner=owner,
        kind=kind,
        path=path,
        blocking=blocking,
        write_pid_file=write_pid_file,
    )
    lock.acquire()
    return lock


def already_running_status(lock: BackgroundLock, *, status: str = "already_running") -> dict[str, Any]:
    return {
        "ok": True,
        "status": status,
        "skipped": True,
        "reason": status,
        "pid": os.getpid(),
        "lock": lock.as_dict(),
        "active_pid": int((lock.active_owner or {}).get("pid") or 0),
        "active_owner": (lock.active_owner or {}).get("owner") or "",
        "finished_at": _iso_now(),
    }
