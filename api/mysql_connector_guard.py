"""
mysql_connector_guard.py
========================
Stability guard for mysql-connector on Python 3.14/macOS.

Why:
- The C extension path (`_mysql_connector`) can segfault under threaded load
  in some environments.
- For service processes, favor stability over raw connect throughput.

Behavior:
- Monkeypatch `mysql.connector.connect` once per process.
- Default to `use_pure=True` unless explicitly disabled.
- Can be controlled with env:
    MAGI_MYSQL_USE_PURE=1|0
"""

from __future__ import annotations

import os
from typing import Any


def _env_on(name: str, default: bool = True) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return bool(default)
    return str(raw).strip().lower() in {"1", "true", "yes", "on", "y"}


def patch_mysql_connector_for_stability() -> bool:
    """
    Patch mysql.connector.connect in-process.
    Returns True when patch is active (or already active), False otherwise.
    """
    try:
        import mysql.connector  # type: ignore
    except Exception:
        return False

    cur_connect = getattr(mysql.connector, "connect", None)
    if not callable(cur_connect):
        return False
    if bool(getattr(cur_connect, "__magi_mysql_guard__", False)):
        return True

    original_connect = cur_connect

    def _guarded_connect(*args: Any, **kwargs: Any):
        # Prefer pure-python path in long-running threaded services.
        if "use_pure" not in kwargs and _env_on("MAGI_MYSQL_USE_PURE", True):
            kwargs["use_pure"] = True
        return original_connect(*args, **kwargs)

    setattr(_guarded_connect, "__magi_mysql_guard__", True)
    setattr(_guarded_connect, "__magi_mysql_original__", original_connect)
    mysql.connector.connect = _guarded_connect  # type: ignore[attr-defined]
    return True

