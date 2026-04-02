#!/usr/bin/env python3
"""
清除向量記憶庫中被 LLM 角色幻覺污染的 chatlog 記憶
===================================================
LLM 有時不回答問題，反而自行生成 CASPER 角色描述/流程說明，
這些內容被 _auto_remember 存入 magi_brain，造成正回饋循環。

用法:
    # 預覽（不刪除）
    python scripts/purge_persona_memories.py --dry-run

    # 實際清理
    python scripts/purge_persona_memories.py
"""

import argparse
import os
import sys

import mysql.connector

try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv()
except Exception:
    pass

DB_CONFIG = {
    "user": os.environ.get("DB_USER", "python_user"),
    "password": os.environ.get("DB_PASSWORD", ""),
    "host": os.environ.get("DB_HOST", "127.0.0.1"),
    "port": int(os.environ.get("DB_PORT", "3306")),
    "database": "magi_brain",
}

# 角色幻覺關鍵字 — 命中 >= THRESHOLD 個即判定為污染記憶
PERSONA_KEYWORDS = [
    "你扮演",
    "扮演 CASPER",
    "CASPER 角色",
    "法律事務說明",
    "核心工作項目",
    "夜間巡邏",
    "確保事務所",
    "運作核心",
    "CASPER 法律事務",
    "流程總覽",
    "法律事務流程",
    "調處紀錄事件",
    "判決獲取",
    "夜間思考、討論與記錄",
]
THRESHOLD = 3


def get_conn():
    return mysql.connector.connect(**DB_CONFIG)


def find_polluted(cursor):
    """找出所有 chatlog 來源中疑似角色幻覺的記憶。"""
    cursor.execute(
        "SELECT id, content, source FROM documents WHERE source LIKE %s",
        ("%chatlog%",),
    )
    polluted = []
    for doc_id, content, source in cursor.fetchall():
        text = content or ""
        hits = sum(1 for kw in PERSONA_KEYWORDS if kw in text)
        if hits >= THRESHOLD:
            polluted.append((doc_id, hits, source, text[:120]))
    return polluted


def delete_ids(cursor, ids):
    if not ids:
        return
    placeholders = ",".join(["%s"] * len(ids))
    cursor.execute(f"DELETE FROM vectors WHERE doc_id IN ({placeholders})", ids)
    cursor.execute(f"DELETE FROM documents WHERE id IN ({placeholders})", ids)


def main():
    parser = argparse.ArgumentParser(description="清除角色幻覺污染記憶")
    parser.add_argument("--dry-run", action="store_true", help="僅預覽，不刪除")
    args = parser.parse_args()

    conn = get_conn()
    cursor = conn.cursor()

    polluted = find_polluted(cursor)
    if not polluted:
        print("✅ 未發現角色幻覺污染記憶。")
        cursor.close()
        conn.close()
        return

    print(f"🔍 發現 {len(polluted)} 筆疑似角色幻覺記憶：\n")
    for doc_id, hits, source, preview in polluted:
        print(f"  id={doc_id}  命中={hits}  來源={source}")
        print(f"    預覽: {preview}...")
        print()

    if args.dry_run:
        print("⏸️  dry-run 模式，不執行刪除。")
    else:
        ids = [p[0] for p in polluted]
        delete_ids(cursor, ids)
        conn.commit()
        print(f"🗑️  已刪除 {len(ids)} 筆污染記憶。")

    cursor.close()
    conn.close()


if __name__ == "__main__":
    main()
