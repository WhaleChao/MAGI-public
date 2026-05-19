"""Compatibility shim for legacy `skills.judgment_collector_module` imports."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_REAL_PATH = Path(__file__).resolve().parent / "judgment-collector" / "action.py"
_SPEC = importlib.util.spec_from_file_location("skills.judgment_collector_module.action", _REAL_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise ImportError(f"Cannot load judgment collector module from {_REAL_PATH}")

action = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(action)
