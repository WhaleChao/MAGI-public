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
    """取得 thread-local DB 連線，支援 failover（遠端→本地）。"""
    conn = getattr(_conn_local, "conn", None)
    if conn is not None:
        try:
            conn.ping(reconnect=True)
            return conn
        except Exception:
            _conn_local.conn = None

    # --- Ensure .env is loaded ---
    from pathlib import Path
    _proj_root = Path(__file__).resolve().parent.parent.parent
    _env_path = _proj_root / ".env"
    if _env_path.exists():
        try:
            from dotenv import load_dotenv
            load_dotenv(str(_env_path))
        except ImportError:
            pass

    import mysql.connector

    # Priority 1: OSC_* (Orchestrator sync), Priority 2: DB_* (Generic)
    primary_host = os.environ.get("OSC_DB_HOST") or os.environ.get("DB_HOST") or "127.0.0.1"
    port = int(os.environ.get("OSC_DB_PORT") or os.environ.get("DB_PORT") or 3306)
    user = os.environ.get("OSC_DB_USER") or os.environ.get("DB_USER") or "casper_service"
    password = os.environ.get("OSC_DB_PASSWORD") or os.environ.get("DB_PASSWORD") or ""

    hosts = [primary_host]
    if primary_host not in {"127.0.0.1", "localhost"}:
        hosts.append("127.0.0.1")

    last_err = None
    for host in hosts:
        try:
            conn = mysql.connector.connect(
                host=host,
                port=port,
                user=user,
                password=password,
                database="law_firm_data",
                connect_timeout=5,
                autocommit=True,
            )
            if host != primary_host:
                logger.info("dedup DB failover: using local DB (127.0.0.1)")
            _conn_local.conn = conn
            return conn
        except Exception as e:
            last_err = e
            continue

    raise ConnectionError(f"dedup DB: all hosts unreachable. last_err: {last_err}")


def normalize_case_id(text: str) -> str:
    """
    規格化案號：移除點、橫槓、年度、字第、號等字符，並移除所有前導零。
    範例：114年度原上訴字第000154號 -> 114原上訴154
          114.原上訴.000154 -> 114原上訴154
    """
    import re
    if not text:
        return ""
    # 移除點、橫槓、空白、底線
    s = re.sub(r"[\.\-_\s]+", "", str(text))
    # 移除中文標記
    s = s.replace("年度", "").replace("字第", "").replace("號", "").replace("字", "")
    # 移除所有數字區塊的前導零 (e.g. 000154 -> 154)
    s = re.sub(r"\b0+(\d+)", r"\1", s)
    # 處理字串中間的零：有些格式是 114原上訴000154，我們希望能對應到 114原上訴154
    # 更暴力一點：移除所有非必要的 0，但年度不能移除。
    # 為了保險，我們只處理「數字區塊」的前端 0
    s = re.sub(r"(?<=[^\d])0+(\d+)", r"\1", s)
    return s


def is_done(category: str, item_key: str) -> bool:
    """
    檢查某項目是否已處理過。
    針對 download 類別，支援模糊匹配（規一化案號）。
    """
    item_key_str = str(item_key).strip()
    if not item_key_str:
        return False

    try:
        conn = _get_conn()
        cur = conn.cursor()
        
        # 1. 精確匹配
        cur.execute(
            "SELECT 1 FROM dedup_registry WHERE category=%s AND item_key=%s LIMIT 1",
            (category, item_key_str[:512]),
        )
        if cur.fetchone():
            return True
        
        # 2. 針對案件下載/申請類別，進行魯棒性規一化匹配
        if category in ("download", "apply", "payment_slip", "filereview_payment"):
            norm_key = normalize_case_id(item_key_str)
            if norm_key:
                # 為了應對 000154 vs 154，我們在 SQL 中同時清理搜尋目標與資料庫欄位
                # 我們移除所有 0 並比較（這在案號場景通常是安全的）
                cur.execute(
                    """SELECT 1 FROM dedup_registry 
                       WHERE category=%s 
                       AND (REPLACE(REPLACE(REPLACE(REPLACE(item_key, '.', ''), '-', ''), ' ', ''), '0', '') 
                            LIKE %s)
                       LIMIT 1""",
                    (category, f"%{norm_key.replace('0', '')}%"),
                )
                if cur.fetchone():
                    return True

        return False
    except Exception as e:
        logger.warning("dedup check failed: %s", e)
        return False


def mark_done(
    category: str,
    item_key: str,
    status: str = "done",
    metadata: Optional[dict] = None,
    notified_at: Optional[str] = None,
) -> bool:
    """標記某項目為已處理。"""
    try:
        conn = _get_conn()
        cur = conn.cursor()
        meta_json = json.dumps(metadata, ensure_ascii=False, default=str) if metadata else None
        ts = notified_at or datetime.now().isoformat()
        
        # 插入原始項
        cur.execute(
            """INSERT INTO dedup_registry (category, item_key, status, metadata, notified_at)
               VALUES (%s, %s, %s, %s, %s)
               ON DUPLICATE KEY UPDATE status=%s, metadata=COALESCE(%s, metadata), updated_at=NOW()""",
            (category, str(item_key)[:512], status, meta_json, ts, status, meta_json),
        )
        
        # 如果是案件，額外「雙重標記」規一化版本，確保下次精確匹配也能中
        if category in ("download", "apply"):
            norm_key = normalize_case_id(item_key)
            if norm_key and norm_key != item_key:
                cur.execute(
                    """INSERT IGNORE INTO dedup_registry (category, item_key, status, metadata, notified_at)
                       VALUES (%s, %s, %s, %s, %s)""",
                    (category, norm_key[:512], "done", '{"source":"auto_normalize"}', ts),
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
