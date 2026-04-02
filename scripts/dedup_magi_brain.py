#!/usr/bin/env python3
"""
magi_brain 去重腳本
==================
清除 documents + vectors 表中的重複內容，每組保留最早（id 最小）的一筆。

用法:
    # 預覽模式（不刪除，僅報告）
    python scripts/dedup_magi_brain.py --dry-run

    # 實際清理
    python scripts/dedup_magi_brain.py

    # 同時清除 smoke test chatlog
    python scripts/dedup_magi_brain.py --purge-smoke
"""

import argparse
import os
import sys
import time

import mysql.connector

# --- Load .env for subprocess/cron credential access ---
try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv()
except Exception:
    pass

DB_CONFIG = {
    "user": os.environ.get("DB_USER", "python_user"),
    "password": os.environ.get("DB_PASSWORD", ""),
    "host": os.environ.get("DB_HOST", "100.121.61.74"),
    "port": int(os.environ.get("DB_PORT", "3306")),
    "database": "magi_brain",
}

BATCH_SIZE = 500  # 每批刪除筆數，避免長時間鎖表


def get_conn():
    return mysql.connector.connect(**DB_CONFIG)


def count_duplicates(cursor):
    """統計重複內容筆數"""
    cursor.execute("""
        SELECT COUNT(*) FROM documents d
        WHERE d.id NOT IN (
            SELECT MIN(id) FROM documents GROUP BY content
        )
    """)
    return cursor.fetchone()[0]


def count_smoke_test(cursor):
    """統計 smoke test chatlog 筆數"""
    cursor.execute(
        "SELECT COUNT(*) FROM documents"
        " WHERE source LIKE %s"
        "   AND (source LIKE %s OR source LIKE %s)",
        ("%chatlog%", "%smoke%", "%bulk_smoke%"),
    )
    return cursor.fetchone()[0]


def get_duplicate_ids_batch(cursor, offset, limit):
    """取得一批要刪除的重複 id（保留每組 MIN(id)）"""
    cursor.execute("""
        SELECT d.id FROM documents d
        WHERE d.id NOT IN (
            SELECT MIN(id) FROM documents GROUP BY content
        )
        ORDER BY d.id
        LIMIT %s OFFSET %s
    """, (limit, offset))
    return [row[0] for row in cursor.fetchall()]


def get_smoke_test_ids_batch(cursor, offset, limit):
    """取得一批 smoke test chatlog 的 id"""
    cursor.execute(
        "SELECT id FROM documents"
        " WHERE source LIKE %s"
        "   AND (source LIKE %s OR source LIKE %s)"
        " ORDER BY id LIMIT %s OFFSET %s",
        ("%chatlog%", "%smoke%", "%bulk_smoke%", limit, offset),
    )
    return [row[0] for row in cursor.fetchall()]


def delete_batch(cursor, ids):
    """刪除一批 documents + 對應的 vectors"""
    if not ids:
        return 0
    placeholders = ",".join(["%s"] * len(ids))
    # 先刪 vectors（外鍵依賴）
    cursor.execute(f"DELETE FROM vectors WHERE doc_id IN ({placeholders})", ids)
    # 再刪 documents
    cursor.execute(f"DELETE FROM documents WHERE id IN ({placeholders})", ids)
    return len(ids)


def dedup_report(cursor):
    """產生去重前的詳細報告"""
    # 依來源分類統計重複
    cursor.execute("""
        SELECT
            CASE
                WHEN source LIKE '%%transcript%%' THEN 'transcript'
                WHEN source LIKE '%%chatlog%%' THEN 'chatlog'
                WHEN source LIKE 'doc=%%' THEN 'doc_upload'
                WHEN source LIKE '%%synced' THEN 'synced_copy'
                WHEN source LIKE '%%autopilot%%' THEN 'autopilot'
                ELSE 'other'
            END AS category,
            COUNT(*) AS total,
            COUNT(DISTINCT content) AS uniq
        FROM documents
        GROUP BY category
        ORDER BY total DESC
    """)
    rows = cursor.fetchall()
    print("\n📊 各來源重複統計:")
    print(f"  {'類別':<16} {'總筆數':>8} {'唯一':>8} {'重複':>8} {'重複率':>8}")
    print("  " + "-" * 56)
    for cat, total, uniq in rows:
        dupes = total - uniq
        rate = f"{dupes/total*100:.1f}%" if total > 0 else "0%"
        print(f"  {cat:<16} {total:>8,} {uniq:>8,} {dupes:>8,} {rate:>8}")


def main():
    parser = argparse.ArgumentParser(description="magi_brain 去重清理")
    parser.add_argument("--dry-run", action="store_true", help="預覽模式，不實際刪除")
    parser.add_argument("--purge-smoke", action="store_true", help="同時清除 smoke test chatlog")
    args = parser.parse_args()

    conn = get_conn()
    cursor = conn.cursor()

    # 先統計
    cursor.execute("SELECT COUNT(*) FROM documents")
    total_before = cursor.fetchone()[0]
    dup_count = count_duplicates(cursor)
    smoke_count = count_smoke_test(cursor) if args.purge_smoke else 0

    print(f"\n🗄️  magi_brain 去重報告")
    print(f"  總筆數:     {total_before:,}")
    print(f"  重複筆數:   {dup_count:,}")
    if args.purge_smoke:
        print(f"  Smoke test: {smoke_count:,}")

    dedup_report(cursor)

    if args.dry_run:
        print(f"\n🔍 預覽模式 — 不會刪除任何資料")
        print(f"   預計刪除: {dup_count:,} 筆重複")
        if args.purge_smoke:
            print(f"   預計刪除: {smoke_count:,} 筆 smoke test（部分與重複重疊）")
        cursor.close()
        conn.close()
        return

    # 確認
    print(f"\n⚠️  即將刪除 {dup_count:,} 筆重複記錄（保留每組最早一筆）")
    confirm = input("確認執行？(yes/no): ").strip().lower()
    if confirm != "yes":
        print("已取消")
        cursor.close()
        conn.close()
        return

    # 批次刪除重複
    deleted_total = 0
    t0 = time.time()

    print(f"\n🗑️  開始批次刪除...")
    while True:
        # 每次重新查詢，因為刪除後 offset 會變
        ids = get_duplicate_ids_batch(cursor, 0, BATCH_SIZE)
        if not ids:
            break
        n = delete_batch(cursor, ids)
        conn.commit()
        deleted_total += n
        elapsed = time.time() - t0
        print(f"  已刪除 {deleted_total:,} / {dup_count:,}  ({elapsed:.0f}s)")

    # Smoke test 清理
    if args.purge_smoke:
        print(f"\n🧹 清除 smoke test chatlog...")
        while True:
            ids = get_smoke_test_ids_batch(cursor, 0, BATCH_SIZE)
            if not ids:
                break
            n = delete_batch(cursor, ids)
            conn.commit()
            deleted_total += n

    # 最終統計
    cursor.execute("SELECT COUNT(*) FROM documents")
    total_after = cursor.fetchone()[0]
    elapsed = time.time() - t0

    print(f"\n✅ 清理完成！")
    print(f"  清理前: {total_before:,}")
    print(f"  清理後: {total_after:,}")
    print(f"  刪除:   {deleted_total:,} 筆")
    print(f"  耗時:   {elapsed:.1f} 秒")

    # 建議 OPTIMIZE
    print(f"\n💡 建議執行 OPTIMIZE TABLE 回收磁碟空間:")
    print(f"   OPTIMIZE TABLE documents;")
    print(f"   OPTIMIZE TABLE vectors;")

    cursor.close()
    conn.close()


if __name__ == "__main__":
    main()
