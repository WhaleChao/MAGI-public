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

import importlib.abc
import importlib.util
import os
import sys
from typing import Any


def _env_on(name: str, default: bool = True) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return bool(default)
    return str(raw).strip().lower() in {"1", "true", "yes", "on", "y"}


class _BlockedMySQLCExtLoader(importlib.abc.Loader):
    def create_module(self, spec):  # type: ignore[override]
        return None

    def exec_module(self, module) -> None:  # type: ignore[override]
        raise ImportError("MAGI blocks mysql-connector C extension in service processes")


class _BlockMySQLCExtFinder(importlib.abc.MetaPathFinder):
    __magi_mysql_cext_blocker__ = True

    def find_spec(self, fullname, path=None, target=None):  # type: ignore[override]
        if fullname == "_mysql_connector" and not _env_on("MAGI_MYSQL_ALLOW_CEXT", False):
            return importlib.util.spec_from_loader(fullname, _BlockedMySQLCExtLoader())
        return None


def install_mysql_cext_blocker() -> bool:
    """
    Prevent mysql-connector from importing the `_mysql_connector` C extension.

    The pure-python connector is slower but stable.  The C extension can load a
    second OpenSSL build into long-running MAGI processes and has caused
    libmalloc memory-corruption crashes under threaded SSL reads.
    """
    if _env_on("MAGI_MYSQL_ALLOW_CEXT", False):
        return False
    if any(bool(getattr(finder, "__magi_mysql_cext_blocker__", False)) for finder in sys.meta_path):
        return True
    sys.meta_path.insert(0, _BlockMySQLCExtFinder())
    return True


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
        # Prefer the pure-python path in long-running threaded services.
        # The C extension can load a second OpenSSL build and crash under
        # threaded SSL reads on Python 3.14/macOS.  Only allow it by explicit
        # opt-in for short-lived diagnostics.
        if _env_on("MAGI_MYSQL_USE_PURE", True) and not _env_on("MAGI_MYSQL_ALLOW_CEXT", False):
            kwargs["use_pure"] = True
        return original_connect(*args, **kwargs)

    setattr(_guarded_connect, "__magi_mysql_guard__", True)
    setattr(_guarded_connect, "__magi_mysql_original__", original_connect)
    mysql.connector.connect = _guarded_connect  # type: ignore[attr-defined]
    return True
