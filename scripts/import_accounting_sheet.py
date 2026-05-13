#!/usr/bin/env python3
"""CLI wrapper for colleague Google Sheet accounting import."""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env", override=False)
except Exception:
    pass

from api.osc.accounting_sheet_import import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
