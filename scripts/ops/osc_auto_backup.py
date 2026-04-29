#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
osc_auto_backup.py – OSC 每日自動備份腳本
由 cron_jobs.json job_osc_auto_backup 呼叫（03:00 每日）

直接重用 api.blueprints.osc_cases._osc_create_backup helper，
不走 HTTP、不需 auth token。
"""

import sys
import os
import logging

# 確保可以 import MAGI root
_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [osc_auto_backup] %(levelname)s %(message)s",
)
logger = logging.getLogger("osc_auto_backup")


def main():
    try:
        from api.blueprints.osc_cases import _osc_create_backup
    except Exception as e:
        logger.error("無法載入 _osc_create_backup: %s", e)
        sys.exit(1)

    try:
        meta = _osc_create_backup(label="auto")
        logger.info(
            "備份完成: %s (%d bytes) — %s",
            meta["filename"],
            meta["size_bytes"],
            meta["table_counts"],
        )
    except Exception as e:
        logger.error("備份失敗: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
