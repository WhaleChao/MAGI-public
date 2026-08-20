"""Small ``fcntl`` compatibility surface for Windows self-host installs.

MAGI only needs advisory whole-file locks.  POSIX keeps its native semantics;
Windows maps shared/exclusive requests to a one-byte mandatory lock because
``msvcrt`` does not expose shared locks.
"""

from __future__ import annotations

import os

try:  # pragma: no branch - exactly one backend exists on a host
    import fcntl as _native
except ImportError:  # Windows
    import msvcrt as _msvcrt

    LOCK_SH = 1
    LOCK_EX = 2
    LOCK_NB = 4
    LOCK_UN = 8

    def _descriptor(target: int | object) -> int:
        return int(target if isinstance(target, int) else target.fileno())  # type: ignore[attr-defined]

    def flock(target: int | object, operation: int) -> None:
        descriptor = _descriptor(target)
        os.lseek(descriptor, 0, os.SEEK_SET)
        if operation & LOCK_UN:
            try:
                _msvcrt.locking(descriptor, _msvcrt.LK_UNLCK, 1)
            except OSError:
                # Unlock is idempotent for MAGI cleanup paths.
                pass
            return
        try:
            mode = _msvcrt.LK_NBLCK if operation & LOCK_NB else _msvcrt.LK_LOCK
            _msvcrt.locking(descriptor, mode, 1)
        except OSError as exc:
            raise BlockingIOError(str(exc)) from exc

    def lockf(target: int | object, operation: int, *_: object, **__: object) -> None:
        flock(target, operation)

else:  # POSIX
    LOCK_SH = _native.LOCK_SH
    LOCK_EX = _native.LOCK_EX
    LOCK_NB = _native.LOCK_NB
    LOCK_UN = _native.LOCK_UN
    flock = _native.flock
    lockf = _native.lockf


__all__ = ("LOCK_SH", "LOCK_EX", "LOCK_NB", "LOCK_UN", "flock", "lockf")
