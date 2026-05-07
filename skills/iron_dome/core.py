#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Compatibility loader for `skills/iron-dome/core.py`."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "iron-dome" / "core.py"
_SPEC = importlib.util.spec_from_file_location("skills.iron_dome._core_impl", str(_SRC))
if _SPEC is None or _SPEC.loader is None:
    raise ImportError(f"Unable to load Iron Dome core from {_SRC}")
_MOD = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MOD)

for _k in dir(_MOD):
    if _k.startswith("__"):
        continue
    globals()[_k] = getattr(_MOD, _k)

