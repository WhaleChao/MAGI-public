# -*- coding: utf-8 -*-
"""Compatibility entrypoint for open-case date extraction.

The source of truth lives in
``casper_ecosystem.law_firm_orchestrators.open_case_vision``.  Keep this file
as a thin wrapper because the legacy LAF portal skill imports
``open_case_vision`` from its own directory.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from casper_ecosystem.law_firm_orchestrators.open_case_vision import (  # noqa: E402
    build_go_live_remark,
    extract_open_case_date,
)

__all__ = ["build_go_live_remark", "extract_open_case_date"]
