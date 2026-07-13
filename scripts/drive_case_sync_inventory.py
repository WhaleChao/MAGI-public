#!/usr/bin/env python3
"""Run MAGI Google Drive/NAS case inventory and conservative sync commands."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api.osc.drive_case_sync import main


if __name__ == "__main__":
    raise SystemExit(main())
