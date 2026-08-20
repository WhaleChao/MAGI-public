"""Advisory single-active guard for one V3 runtime state directory."""

from __future__ import annotations

from . import fcntl_compat as fcntl
import json
import os
from pathlib import Path

from .errors import CoreError


class SingleActiveError(CoreError):
    """Another V3 runtime already owns the active lock."""


class SingleActiveGuard:
    """Hold an exclusive non-blocking ``flock`` until explicitly released."""

    def __init__(self, path: Path, *, instance_id: str) -> None:
        self.path = path
        self.instance_id = instance_id
        self._fd: int | None = None

    @property
    def acquired(self) -> bool:
        return self._fd is not None

    def acquire(self) -> None:
        if self._fd is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(fd)
            raise SingleActiveError(f"V3 runtime lock is already held: {self.path}") from exc
        payload = json.dumps(
            {"instance_id": self.instance_id, "pid": os.getpid()},
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        os.ftruncate(fd, 0)
        os.write(fd, payload)
        os.fsync(fd)
        self._fd = fd

    def release(self) -> None:
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def __enter__(self) -> "SingleActiveGuard":
        self.acquire()
        return self

    def __exit__(self, *_: object) -> None:
        self.release()
