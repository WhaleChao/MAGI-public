"""
dedup_db.py — 統一去重查詢/寫入介面（DB-backed）
================================================
取代散落在各目錄的 JSON 去重檔案。
所有去重紀錄存在 law_firm_data.dedup_registry 表。

使用方式：
    from skills.ops.dedup_db import is_done, mark_done, list_done

    if is_done("download", "some_file.pdf"):
        print("已下載，跳過")
    else:
        download(file)
        mark_done("download", "some_file.pdf", metadata={"size": 12345})
"""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime
from typing import Any

logger = logging.getLogger("DedupDB")

_conn_local = threading.local()


def _get_conn():
    """取得 thread-local DB 連線。"""
    conn = getattr(_conn_local, "conn", None)
    if conn is not None:
        try:
            conn.ping(reconnect=True)
            return conn
        except Exception:
            pass

    import mysql.connector
    conn = mysql.connector.connect(
        host=os.environ.get("OSC_DB_HOST", "127.0.0.1"),
        port=int(os.environ.get("OSC_DB_PORT", 3306)),
        user=os.environ.get("OSC_DB_USER", "casper_service"),
        password=os.environ.get("OSC_DB_PASSWORD", ""),
        database="law_firm_data",
        connect_timeout=10,
        autocommit=True,
    )
    _conn_local.conn = conn
    return conn


def is_done(category: str, item_key: str) -> bool:
    """檢查某項目是否已處理過。"""
    try:
        conn = _get_conn()
        cur = conn.cursor()
        cur.execute(
            "SELECT 1 FROM dedup_registry WHERE category=%s AND item_key=%s LIMIT 1",
            (category, str(item_key)[:512]),
        )
        return cur.fetchone() is not None
    except Exception as e:
        logger.warning("dedup check failed: %s", e)
        return False


def mark_done(
    category: str,
    item_key: str,
    status: str = "done",
    metadata: dict | None = None,
    notified_at: str | None = None,
) -> bool:
    """標記某項目為已處理。"""
    try:
        conn = _get_conn()
        cur = conn.cursor()
        meta_json = json.dumps(metadata, ensure_ascii=False, default=str) if metadata else None
        ts = notified_at or datetime.now().isoformat()
        cur.execute(
            """INSERT INTO dedup_registry (category, item_key, status, metadata, notified_at)
               VALUES (%s, %s, %s, %s, %s)
               ON DUPLICATE KEY UPDATE status=%s, metadata=COALESCE(%s, metadata), updated_at=NOW()""",
            (category, str(item_key)[:512], status, meta_json, ts, status, meta_json),
        )
        return True
    except Exception as e:
        logger.warning("dedup mark_done failed: %s", e)
        return False


def list_done(category: str, limit: int = 100) -> list[dict]:
    """列出某類別的所有已處理項目。"""
    try:
        conn = _get_conn()
        cur = conn.cursor()
        cur.execute(
            "SELECT item_key, status, metadata, notified_at, created_at FROM dedup_registry "
            "WHERE category=%s ORDER BY created_at DESC LIMIT %s",
            (category, limit),
        )
        cols = ["item_key", "status", "metadata", "notified_at", "created_at"]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
    except Exception as e:
        logger.warning("dedup list failed: %s", e)
        return []


def count_done(category: str) -> int:
    """計算某類別的已處理數量。"""
    try:
        conn = _get_conn()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM dedup_registry WHERE category=%s", (category,))
        return cur.fetchone()[0]
    except Exception as e:
        logger.warning("dedup count failed: %s", e)
        return 0


def get_stats() -> dict[str, int]:
    """取得所有類別的統計。"""
    try:
        conn = _get_conn()
        cur = conn.cursor()
        cur.execute("SELECT category, COUNT(*) FROM dedup_registry GROUP BY category ORDER BY category")
        return {row[0]: row[1] for row in cur.fetchall()}
    except Exception as e:
        logger.warning("dedup stats failed: %s", e)
        return {}


def remove(category: str, item_key: str) -> bool:
    """移除某項目的去重紀錄（重新允許處理）。"""
    try:
        conn = _get_conn()
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM dedup_registry WHERE category=%s AND item_key=%s",
            (category, str(item_key)[:512]),
        )
        return True
    except Exception as e:
        logger.warning("dedup remove failed: %s", e)
        return False
