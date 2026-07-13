"""Compatibility entrypoint for LAF workflow helpers.

The source of truth lives in ``api.domains.laf_flow``.  This module exists for
legacy imports from the orchestrator directory and must not grow separate
business rules.
"""
from __future__ import annotations

from api.domains import laf_flow as _canonical

for _name in dir(_canonical):
    if not (_name.startswith("__") and _name.endswith("__")):
        globals()[_name] = getattr(_canonical, _name)

__all__ = [
    _name
    for _name in globals()
    if not (_name.startswith("__") and _name.endswith("__"))
]
