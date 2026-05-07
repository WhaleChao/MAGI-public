"""Compatibility shim for legacy `skills.osc_orchestrator.action` imports."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_REAL_PATH = Path(__file__).resolve().parents[1] / "osc-orchestrator" / "action.py"
_SPEC = importlib.util.spec_from_file_location("skills.osc_orchestrator._action", _REAL_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise ImportError(f"Cannot load OSC orchestrator action from {_REAL_PATH}")

_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)

for _name in dir(_MODULE):
    if _name.startswith("__"):
        continue
    globals()[_name] = getattr(_MODULE, _name)

