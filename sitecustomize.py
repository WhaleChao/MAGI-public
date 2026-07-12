"""
Global runtime guards for MAGI Python processes.

Auto-loaded by Python's site initialization when this directory is on sys.path.
"""

from __future__ import annotations

import importlib.abc
import importlib.util
import os
import sys
from pathlib import Path


# Stability-first defaults (can still be overridden by explicit env vars).
os.environ.setdefault("MAGI_MYSQL_USE_PURE", "1")
os.environ.setdefault("MYSQL_USE_PURE", "1")
os.environ.setdefault("MAGI_AVOID_DISTRIBUTED", "1")

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _env_on(name: str, default: bool = False) -> bool:
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


def _install_mysql_cext_blocker() -> None:
    if _env_on("MAGI_MYSQL_ALLOW_CEXT", False):
        return
    if any(bool(getattr(finder, "__magi_mysql_cext_blocker__", False)) for finder in sys.meta_path):
        return
    sys.meta_path.insert(0, _BlockMySQLCExtFinder())


def _patch_mysql_connector() -> None:
    try:
        from api.mysql_connector_guard import patch_mysql_connector_for_stability
    except Exception:
        return
    try:
        patch_mysql_connector_for_stability()
    except Exception:
        return


_install_mysql_cext_blocker()
_patch_mysql_connector()
