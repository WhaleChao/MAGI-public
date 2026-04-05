#!/usr/bin/env python3
"""
clean_degraded_memories.py
==========================
清除 magi_brain.documents 中因模型降級產生的破碎記憶，
並強制重建本地 FAISS 向量索引。

步驟：
  1. 找出 source 含 "is_degraded=True" 的記錄
  2. 找出 content 含降級標記文字的記錄
  3. （dry-run 模式）僅顯示數量，不刪除
  4. 刪除 documents + cascade 清除 vectors
  5. 強制重建 FAISS（hours_threshold=0）

Usage:
    python clean_degraded_memories.py --dry-run       # 預覽，不刪
    python clean_degraded_memories.py                  # 實際執行
    python clean_degraded_memories.py --skip-faiss     # 只清 DB，不重建 FAISS
"""

from __future__ import annotations

import argparse
import logging
import os
_MAGI_ROOT = os.path.abspath(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
import sys

# --- Load .env for subprocess/cron credential access ---
try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv()
except Exception:
    pass

sys.path.insert(0, os.environ.get("MAGI_ROOT", _MAGI_ROOT))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("clean-degraded-memories")

# ---------------------------------------------------------------------------
# DB config（與 mem_bridge.py 相同來源）
# ---------------------------------------------------------------------------
DB_CONFIG = {
    "user": os.environ.get("DB_USER", "casper_service"),
    "password": os.environ.get("DB_PASSWORD", ""),
    "host": os.environ.get("DB_HOST", "127.0.0.1"),
    "database": os.environ.get("DB_NAME", "magi_brain"),
}

# 降級標記：符合任一條件即視為污染記錄
DEGRADED_CONTENT_FLAGS = [
    "（系統降級回覆）",
    "(系統降級回覆)",
    "（降級摘要）",
    "(降級摘要)",
    "摘要失敗，前 20 行預覽",
    "(系統提示：最終整合失敗",
    "模型忙碌",
    "server busy",
    "maximum pending requests",
]

DEGRADED_SOURCE_FLAGS = [
    "is_degraded=True",
]


def _get_conn():
    try:
        import mysql.connector
        return mysql.connector.connect(**DB_CONFIG)
    except Exception as e:
        logger.error("無法連線 Keeper DB：%s", e)
        return None


def find_degraded_ids(conn) -> list[int]:
    """回傳所有污染記錄的 document id。"""
    ids = set()
    cur = conn.cursor()

    # 1. source 含降級旗標
    for flag in DEGRADED_SOURCE_FLAGS:
        cur.execute("SELECT id FROM documents WHERE source LIKE %s", (f"%{flag}%",))
        for (row_id,) in cur.fetchall():
            ids.add(row_id)

    # 2. content 含降級文字
    for flag in DEGRADED_CONTENT_FLAGS:
        cur.execute("SELECT id FROM documents WHERE content LIKE %s", (f"%{flag}%",))
        for (row_id,) in cur.fetchall():
            ids.add(row_id)

    cur.close()
    return sorted(ids)


def delete_by_ids(conn, ids: list[int]) -> int:
    """刪除 documents（vectors 透過 ON DELETE CASCADE 自動清除）。"""
    if not ids:
        return 0
    cur = conn.cursor()
    # 分批刪除，避免超大 IN 條件
    batch_size = 200
    deleted = 0
    for i in range(0, len(ids), batch_size):
        batch = ids[i:i + batch_size]
        placeholders = ",".join(["%s"] * len(batch))
        cur.execute(f"DELETE FROM documents WHERE id IN ({placeholders})", batch)
        deleted += cur.rowcount
    conn.commit()
    cur.close()
    return deleted


def rebuild_faiss():
    """強制重建本地 FAISS 索引（hours_threshold=0 = 無條件重建）。"""
    try:
        from skills.memory.faiss_index import FAISSMemoryIndex
        idx = FAISSMemoryIndex.get_instance(dim=768)
        rebuilt = idx.rebuild_if_needed(DB_CONFIG, hours_threshold=0)
        if rebuilt:
            logger.info("FAISS 索引重建完成，total=%d", idx.total)
        else:
            logger.warning("rebuild_if_needed 回傳 False（不應發生於 threshold=0）")
    except Exception as e:
        logger.error("FAISS 重建失敗：%s", e)


def main():
    parser = argparse.ArgumentParser(description="清除降級污染記憶並重建 FAISS")
    parser.add_argument("--dry-run", action="store_true", help="只顯示數量，不刪除")
    parser.add_argument("--skip-faiss", action="store_true", help="跳過 FAISS 重建")
    args = parser.parse_args()

    conn = _get_conn()
    if conn is None:
        sys.exit(1)

    logger.info("掃描污染記錄中...")
    ids = find_degraded_ids(conn)
    logger.info("找到 %d 筆污染記錄（id: %s...）", len(ids), ids[:10])

    if args.dry_run:
        logger.info("[dry-run] 不執行刪除。")
        conn.close()
        return

    if not ids:
        logger.info("無污染記錄，跳過刪除。")
    else:
        deleted = delete_by_ids(conn, ids)
        logger.info("已刪除 %d 筆 documents（vectors 已 cascade 清除）", deleted)

    conn.close()

    if not args.skip_faiss:
        logger.info("強制重建 FAISS 向量索引...")
        rebuild_faiss()

    logger.info("清污完成。")


if __name__ == "__main__":
    main()
