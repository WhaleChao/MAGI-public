"""
Global runtime guards for MAGI Python processes.

Auto-loaded by Python's site initialization when this directory is on sys.path.
"""

from __future__ import annotations

import os


# Stability-first defaults (can still be overridden by explicit env vars).
os.environ.setdefault("MAGI_MYSQL_USE_PURE", "1")
os.environ.setdefault("MYSQL_USE_PURE", "1")
os.environ.setdefault("MAGI_AVOID_DISTRIBUTED", "1")


def _patch_mysql_connector() -> None:
    try:
        from api.mysql_connector_guard import patch_mysql_connector_for_stability
    except Exception:
        return
    try:
        patch_mysql_connector_for_stability()
    except Exception:
        return


_patch_mysql_connector()
