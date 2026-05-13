#!/usr/bin/env python3
"""
Purge operational logs from magi_brain vector DB
=================================================

Moves operational log entries (autopilot steps, LAF orchestrator logs,
synced copies, test entries, etc.) out of the main knowledge vector DB.

These entries pollute RAG recall — they're system operational records,
not legal knowledge. They account for ~37% of all vectors (170K+ entries).

This script:
1. Counts entries by operational source prefix
2. Deletes from `documents` table (vectors cascade via FK)
3. Triggers FAISS index rebuild

Usage:
    # Preview what would be purged
    python scripts/ops/purge_ops_logs_from_vectors.py --dry-run

    # Execute purge
    python scripts/ops/purge_ops_logs_from_vectors.py

    # Also deduplicate exact-duplicate vectors
    python scripts/ops/purge_ops_logs_from_vectors.py --dedup
"""

import argparse
import json
import logging
import os
import sys
import time

MAGI_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if MAGI_ROOT not in sys.path:
    sys.path.insert(0, MAGI_ROOT)

logger = logging.getLogger("purge_ops_logs")

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

# These source prefixes are operational logs, not knowledge
OPS_LOG_PREFIXES = [
    "laf_orchestrator",
    "laf_automation_v2",
    "magi_autopilot_step",
    "magi_autopilot_step_synced",
    "magi_autopilot",
    "magi_autopilot_synced",
    "osc_orchestrator",
    "osc_orchestrator_synced",
    "file_review_orchestrator",
    "file_review_orchestrator_synced",
    "laf_automation_v2_synced",
    "audit_script",
    "pdf_namer",
    "smoke_synced",
    "dedup_test",
    "batch_test",
    "migration_test",
    "healthcheck",
    "healthcheck_vector",
    "healthcheck_rag_exact",
    "system_test",
    "verification_script",
    "test|",
]

# Keep these even though they look like logs — they contain useful context
KEEP_PREFIXES = [
    "magi_autopilot|",  # high-level summaries (only ~1500 entries)
]


def _connect():
    import mysql.connector
    return mysql.connector.connect(
        host=os.environ.get("DB_HOST", "127.0.0.1"),
        port=int(os.environ.get("DB_PORT", "3306")),
        user=os.environ.get("DB_USER", "casper_service"),
        password=os.environ.get("DB_PASSWORD", ""),
        database="magi_brain",
    )


def count_ops_entries(cur) -> list:
    """Count entries per ops log prefix."""
    results = []
    for prefix in OPS_LOG_PREFIXES:
        # Exclude entries that match KEEP_PREFIXES
        cur.execute(
            "SELECT COUNT(*) FROM documents WHERE source LIKE %s",
            (f"{prefix}%",)
        )
        cnt = cur.fetchone()[0]
        if cnt > 0:
            results.append((prefix, cnt))
    results.sort(key=lambda x: x[1], reverse=True)
    return results


def purge_ops(dry_run: bool = False):
    """Delete operational log entries from documents (vectors cascade)."""
    conn = _connect()
    cur = conn.cursor()

    print("📊 掃描操作日誌條目...\n")
    entries = count_ops_entries(cur)

    if not entries:
        print("✅ 沒有需要清理的操作日誌")
        cur.close()
        conn.close()
        return 0

    total = 0
    print(f"{'來源前綴':45s} {'數量':>8s}")
    print("-" * 55)
    for prefix, cnt in entries:
        print(f"  {prefix:43s} {cnt:>8,}")
        total += cnt
    print("-" * 55)
    print(f"  {'合計':43s} {total:>8,}")

    if dry_run:
        print(f"\n🔍 預覽模式 — 不會刪除")
        cur.close()
        conn.close()
        return total

    print(f"\n🗑️  開始清理 {total:,} 筆操作日誌...")
    deleted = 0
    for prefix, cnt in entries:
        cur.execute(
            "DELETE FROM documents WHERE source LIKE %s",
            (f"{prefix}%",)
        )
        deleted += cur.rowcount
        print(f"  ✅ {prefix}: {cur.rowcount:,} 筆已刪除")

    conn.commit()
    print(f"\n✅ 共刪除 {deleted:,} 筆")

    cur.close()
    conn.close()
    return deleted


def dedup_vectors(dry_run: bool = False):
    """Remove exact-duplicate documents (keep earliest ID)."""
    conn = _connect()
    cur = conn.cursor(dictionary=True)

    print("\n📊 掃描完全重複的向量條目...\n")

    cur.execute(
        "SELECT MD5(content) AS h, MIN(id) AS keep_id, COUNT(*) AS cnt, "
        "GROUP_CONCAT(id ORDER BY id SEPARATOR ',') AS all_ids "
        "FROM documents "
        "GROUP BY MD5(content) "
        "HAVING COUNT(*) > 1 "
        "ORDER BY cnt DESC"
    )
    dupes = cur.fetchall()

    if not dupes:
        print("✅ 沒有重複條目")
        cur.close()
        conn.close()
        return 0

    total_extra = sum(d["cnt"] - 1 for d in dupes)
    print(f"發現 {len(dupes)} 組重複，共 {total_extra:,} 筆多餘條目")
    print(f"前 5 組:")
    for d in dupes[:5]:
        print(f"  MD5={d['h'][:12]}... 重複 {d['cnt']} 次, 保留 id={d['keep_id']}")

    if dry_run:
        print(f"\n🔍 預覽模式 — 不會刪除")
        cur.close()
        conn.close()
        return total_extra

    print(f"\n🗑️  開始去重...")
    deleted = 0
    for d in dupes:
        keep_id = d["keep_id"]
        all_ids = [int(x) for x in d["all_ids"].split(",")]
        delete_ids = [x for x in all_ids if x != keep_id]
        if not delete_ids:
            continue
        placeholders = ",".join(["%s"] * len(delete_ids))
        cur.execute(f"DELETE FROM documents WHERE id IN ({placeholders})", delete_ids)
        deleted += cur.rowcount

    conn.commit()
    print(f"✅ 去重完成，刪除 {deleted:,} 筆多餘條目")

    cur.close()
    conn.close()
    return deleted


def rebuild_faiss():
    """Trigger FAISS index rebuild."""
    print("\n🔄 重建 FAISS 索引...")
    try:
        from skills.memory.mem_bridge import _get_faiss_index
        idx = _get_faiss_index()
        if idx and hasattr(idx, "rebuild"):
            idx.rebuild()
            print(f"✅ FAISS 索引已重建 ({idx.total:,} vectors)")
        else:
            print("⚠️  FAISS 索引不支援 rebuild，需手動重啟服務")
    except Exception as e:
        print(f"⚠️  FAISS 重建失敗: {e}")
        print("   請重啟 MAGI 服務以重建索引")


def main():
    parser = argparse.ArgumentParser(description="清理 magi_brain 中的操作日誌向量")
    parser.add_argument("--dry-run", action="store_true", help="預覽模式")
    parser.add_argument("--dedup", action="store_true", help="同時去除完全重複的向量")
    parser.add_argument("--rebuild-faiss", action="store_true", help="清理後重建 FAISS 索引")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    t0 = time.time()

    purged = purge_ops(dry_run=args.dry_run)

    if args.dedup:
        deduped = dedup_vectors(dry_run=args.dry_run)
    else:
        deduped = 0

    if not args.dry_run and (purged or deduped):
        if args.rebuild_faiss:
            rebuild_faiss()
        else:
            print("\n💡 提示：重啟 MAGI 服務後 FAISS 索引會自動重建")

    elapsed = time.time() - t0
    print(f"\n⏱️  耗時 {elapsed:.1f}s")


if __name__ == "__main__":
    main()
