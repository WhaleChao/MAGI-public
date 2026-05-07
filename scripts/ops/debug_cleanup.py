#!/usr/bin/env python3
"""Clean old debug capture files for the MAGI cron scheduler."""

from __future__ import annotations

import sys
from pathlib import Path

# Cron 環境執行時 cwd 與 sys.path 不一定包含專案根目錄
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api.debug_capture import cleanup_old


def main() -> int:
    cleaned = cleanup_old(48)
    print(f"cleaned {cleaned} files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
