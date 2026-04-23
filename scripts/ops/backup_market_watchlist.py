#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Daily backup of .agent/market_watchlist.json to .runtime/backups/market_watchlist/.

Also raises a Telegram alert when the watchlist shrinks by >= 50% (protects
against silent truncation like the 2026-04-08→04-11 incident where 9 symbols
collapsed to 1 without anyone noticing).

Retention: 90 days.

Exit codes:
    0 — success (backup written or no-op because nothing changed)
    1 — backup failed (watchlist file missing or write error)
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

logger = logging.getLogger("backup_market_watchlist")

WATCHLIST_PATH = _ROOT / ".agent" / "market_watchlist.json"
BACKUP_DIR = _ROOT / ".runtime" / "backups" / "market_watchlist"
RETENTION_DAYS = int(os.environ.get("MAGI_WATCHLIST_BACKUP_RETENTION_DAYS", "90"))
SHRINK_ALERT_THRESHOLD = float(os.environ.get("MAGI_WATCHLIST_SHRINK_ALERT_RATIO", "0.5"))


def _load_watchlist_count(path: Path) -> Optional[int]:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        wl = data.get("watchlist")
        if isinstance(wl, list):
            return len(wl)
    except Exception:
        logger.exception("failed to read %s", path)
    return None


def _latest_backup(backup_dir: Path) -> Optional[Path]:
    if not backup_dir.exists():
        return None
    candidates = sorted(backup_dir.glob("*.json"))
    return candidates[-1] if candidates else None


def _purge_old_backups(backup_dir: Path, retention_days: int) -> int:
    if not backup_dir.exists():
        return 0
    cutoff = time.time() - retention_days * 86400
    removed = 0
    for f in backup_dir.glob("*.json"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
                removed += 1
        except OSError:
            pass
    return removed


def _alert_tg(message: str) -> None:
    try:
        from skills.ops.red_phone import send_telegram_push_with_status
        send_telegram_push_with_status(
            message=message,
            severity="warning",
            source="backup_market_watchlist",
            topic_key="self_repair",
        )
    except Exception as e:
        logger.error("TG alert failed: %s", e)


def main() -> int:
    if not WATCHLIST_PATH.exists():
        logger.warning("watchlist file missing: %s (skip)", WATCHLIST_PATH)
        return 0  # empty-state is not a failure

    # Compare to latest backup for shrink detection
    latest = _latest_backup(BACKUP_DIR)
    old_count = _load_watchlist_count(latest) if latest else None
    new_count = _load_watchlist_count(WATCHLIST_PATH)

    if new_count is None:
        logger.error("cannot parse watchlist json: %s", WATCHLIST_PATH)
        return 1

    # Shrink alert (fires before we overwrite — preserves evidence)
    if old_count is not None and old_count > 0:
        shrink_ratio = (old_count - new_count) / old_count
        if shrink_ratio >= SHRINK_ALERT_THRESHOLD and new_count < old_count:
            _alert_tg(
                f"⚠️ Watchlist 縮減告警\n"
                f"舊: {old_count} 支 → 新: {new_count} 支（縮減 {shrink_ratio:.0%}）\n"
                f"上次備份: {latest.name if latest else '(無)'}\n"
                f"如非預期請立即檢查 .agent/market_watchlist.json"
            )
            logger.warning("shrink alert sent: %d -> %d", old_count, new_count)

    # Write new backup (idempotent per day — same-day retries overwrite)
    today = datetime.now().strftime("%Y-%m-%d")
    backup_path = BACKUP_DIR / f"{today}.json"
    try:
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy2(WATCHLIST_PATH, backup_path)
    except OSError as e:
        logger.error("backup write failed: %s", e)
        return 1

    removed = _purge_old_backups(BACKUP_DIR, RETENTION_DAYS)
    logger.info("backup ok: %s (count=%d, purged=%d)", backup_path.name, new_count, removed)
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    sys.exit(main())
