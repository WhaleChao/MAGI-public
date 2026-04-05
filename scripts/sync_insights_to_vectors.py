#!/usr/bin/env python3
"""
legal_insights ↔ magi_brain 雙向同步
=====================================
將 law_firm_data.legal_insights 中有 insight_text 的記錄同步到
magi_brain 向量記憶庫，讓 MAGI recall 能搜到手動新增的見解。

用法:
    # 預覽
    python scripts/sync_insights_to_vectors.py --dry-run

    # 執行同步
    python scripts/sync_insights_to_vectors.py

    # 作為 cron / LaunchAgent 定期執行
    python scripts/sync_insights_to_vectors.py --quiet
"""
import argparse
import hashlib
import json
import logging
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

logger = logging.getLogger("insight_sync")

# ---------- DB configs ----------
REMOTE_DB = {
    "host": os.environ.get("OSC_DB_HOST", "127.0.0.1"),
    "port": int(os.environ.get("OSC_DB_PORT", "3306")),
    "user": os.environ.get("OSC_DB_USER", "python_user"),
    "password": os.environ.get("OSC_DB_PASSWORD", ""),
    "database": "law_firm_data",
}

LOCAL_DB = {
    "host": os.environ.get("DB_HOST", "127.0.0.1"),
    "port": int(os.environ.get("DB_PORT", "3306")),
    "user": os.environ.get("DB_USER", "casper_service"),
    "password": os.environ.get("DB_PASSWORD", ""),
    "database": "magi_brain",
}

OMLX_URL = os.environ.get("OMLX_EMBED_URL", "http://127.0.0.1:8081/v1/embeddings")
EMBED_MODEL = os.environ.get("OMLX_EMBED_MODEL", os.environ.get("MAGI_OMLX_EMBED_MODEL", ""))
SOURCE_PREFIX = "legal_insight"


def _get_embedding(text: str) -> list:
    """Get embedding from oMLX."""
    import requests
    try:
        resp = requests.post(
            OMLX_URL,
            json={"input": text, "model": EMBED_MODEL},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()["data"][0]["embedding"]
    except Exception as e:
        logger.warning("Embedding failed: %s", e)
        return [0.0] * 768


def _get_embeddings_batch(texts: list) -> list:
    """Batch embed via oMLX."""
    import requests
    results = []
    # oMLX may not support true batching, so do one by one
    for t in texts:
        results.append(_get_embedding(t))
    return results


def _content_hash(text: str) -> str:
    return hashlib.md5(text.encode("utf-8", errors="replace")).hexdigest()


def _build_mem_content(row: dict) -> str:
    """Build a searchable text block from a legal_insights row."""
    parts = []
    if row.get("case_number"):
        parts.append(f"案號：{row['case_number']}")
    if row.get("case_reason"):
        parts.append(f"案由：{row['case_reason']}")
    if row.get("court_reference"):
        parts.append(f"裁判字號：{row['court_reference']}")
    if row.get("insight_type"):
        parts.append(f"類型：{row['insight_type']}")
    if row.get("document_name"):
        parts.append(f"文件：{row['document_name']}")
    parts.append("")
    parts.append(row.get("insight_text") or "")
    return "\n".join(parts).strip()


def sync(dry_run: bool = False, quiet: bool = False):
    """Main sync: legal_insights → magi_brain vectors."""
    t0 = time.time()

    # 1. Read all legal_insights with content
    remote_conn = mysql.connector.connect(**REMOTE_DB)
    remote_cur = remote_conn.cursor(dictionary=True)
    remote_cur.execute(
        "SELECT id, case_number, case_reason, court_reference, court_type, "
        "insight_type, insight_text, document_name, source_file, is_degraded, "
        "extracted_date FROM legal_insights "
        "WHERE insight_text IS NOT NULL AND insight_text != '' AND is_degraded = 0"
    )
    insights = remote_cur.fetchall()
    remote_cur.close()
    remote_conn.close()

    if not insights:
        if not quiet:
            print("沒有可同步的見解")
        return

    # 2. Check which are already in magi_brain (by content hash)
    local_conn = mysql.connector.connect(**LOCAL_DB)
    local_cur = local_conn.cursor()

    # Get existing hashes from magi_brain for insight sources
    local_cur.execute(
        "SELECT MD5(content) FROM documents WHERE source LIKE %s",
        (f"{SOURCE_PREFIX}%",)
    )
    existing_hashes = {row[0] for row in local_cur.fetchall()}

    # Also check all content hashes to avoid cross-source duplicates
    new_insights = []
    for ins in insights:
        content = _build_mem_content(ins)
        h = _content_hash(content)
        if h not in existing_hashes:
            new_insights.append((ins, content))
            existing_hashes.add(h)  # prevent intra-batch dupes

    if not new_insights:
        if not quiet:
            print(f"✅ 所有 {len(insights)} 筆見解已在向量庫中，無需同步")
        local_cur.close()
        local_conn.close()
        return

    if not quiet:
        print(f"📊 legal_insights: {len(insights)} 筆有內容")
        print(f"   已向量化: {len(insights) - len(new_insights)} 筆")
        print(f"   待同步:   {len(new_insights)} 筆")

    if dry_run:
        print(f"\n🔍 預覽模式 — 不會寫入")
        for ins, content in new_insights[:5]:
            print(f"   [{ins['id']}] {ins.get('case_reason', '?')[:20]} — {content[:60]}...")
        if len(new_insights) > 5:
            print(f"   ... 還有 {len(new_insights) - 5} 筆")
        local_cur.close()
        local_conn.close()
        return

    # 3. Embed and insert
    if not quiet:
        print(f"\n⏳ 向量化 {len(new_insights)} 筆見解...")

    texts = [content for _, content in new_insights]
    embeddings = _get_embeddings_batch(texts)

    inserted = 0
    for i, ((ins, content), emb) in enumerate(zip(new_insights, embeddings)):
        source = f"{SOURCE_PREFIX}|id={ins['id']}|reason={ins.get('case_reason', '')[:30]}"[:250]
        try:
            local_cur.execute(
                "INSERT INTO documents (content, source) VALUES (%s, %s)",
                (content, source),
            )
            doc_id = local_cur.lastrowid
            local_cur.execute(
                "INSERT INTO vectors (doc_id, embedding) VALUES (%s, %s)",
                (doc_id, json.dumps(emb)),
            )
            inserted += 1
        except Exception as e:
            logger.warning("Insert failed for insight %d: %s", ins["id"], e)

    local_conn.commit()
    local_cur.close()
    local_conn.close()

    elapsed = time.time() - t0
    if not quiet:
        print(f"\n✅ 同步完成！寫入 {inserted} / {len(new_insights)} 筆")
        print(f"   耗時: {elapsed:.1f} 秒")
    else:
        if inserted > 0:
            print(f"insight_sync: {inserted} new vectors ({elapsed:.1f}s)")


def main():
    parser = argparse.ArgumentParser(description="legal_insights → magi_brain 向量同步")
    parser.add_argument("--dry-run", action="store_true", help="預覽模式")
    parser.add_argument("--quiet", action="store_true", help="安靜模式（cron 用）")
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING if args.quiet else logging.INFO)
    sync(dry_run=args.dry_run, quiet=args.quiet)


if __name__ == "__main__":
    main()
