#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Compatibility entrypoint for LAF deep extract backfill.

The source of truth lives in
``casper_ecosystem.law_firm_orchestrators.laf_deep_extract_backfill``.  This
script remains as the scheduled/CLI path only.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from casper_ecosystem.law_firm_orchestrators import laf_deep_extract_backfill as _canonical  # noqa: E402

run = _canonical.run
main = _canonical.main

__all__ = ["run", "main"]


if __name__ == "__main__":
    raise SystemExit(main())
