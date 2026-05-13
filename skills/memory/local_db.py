"""
Local MariaDB Interface
=======================
Handles backup storage and synchronization for the local MariaDB instance.
Replaces sqlite_backup.py.
"""

import mysql.connector
import logging
import json
import os
import hashlib

logger = logging.getLogger("LocalDB")

# Local DB Config (Socket Auth)
LOCAL_DB_CONFIG = {
    'user': 'ai',
    'host': 'localhost',
    'database': 'magi_brain',
    'unix_socket': '/tmp/mysql.sock',
    'password': None
}

def _get_connection():
    """Get connection to local MariaDB."""
    return mysql.connector.connect(**LOCAL_DB_CONFIG)

def save_local(content: str, source: str = "unknown", is_synced: bool = False) -> int:
    """
    Save memory to local MariaDB.
    
    Args:
        content: Memory content
        source: Source identifier
        is_synced: Whether this record is already synced to Keeper
    
    Returns:
        Inserted document ID
    """
    conn = None
    cursor = None
    try:
        conn = _get_connection()
        cursor = conn.cursor()
        
        # Truncate source to fit within DB VARCHAR limits (typically 255)
        safe_source = str(source)[:250] if source else "unknown"
        content_text = str(content or "")
        content_hash = hashlib.md5(content_text.encode("utf-8", errors="replace")).hexdigest()

        cursor.execute(
            "SELECT id FROM documents WHERE MD5(content) = %s LIMIT 1",
            (content_hash,),
        )
        row = cursor.fetchone()
        if row:
            doc_id = int(row[0])
            if is_synced:
                cursor.execute("UPDATE documents SET synced = 1 WHERE id = %s", (doc_id,))
                conn.commit()
            logger.info("💾 Local DB duplicate skipped (ID: %s)", doc_id)
            return doc_id

        # Insert Document
        sql = "INSERT INTO documents (content, source, synced) VALUES (%s, %s, %s)"
        cursor.execute(sql, (content_text, safe_source, is_synced))
        doc_id = cursor.lastrowid
        
        # Note: We don't necessarily generate embeddings here if just backing up.
        # But if we want local search, we should.
        # For now, let's keep it simple: Just storage. 
        # Ideally, we should also store the vector if available, but mem_bridge handles that logic.
        
        conn.commit()
        logger.info(f"💾 Saved to Local DB (ID: {doc_id}, Synced: {is_synced})")
        return doc_id
        
    except Exception as e:
        logger.error(f"❌ Local DB Save Error: {e}")
        return -1
    finally:
        if conn and conn.is_connected():
            if cursor:
                cursor.close()
            conn.close()

def get_pending_sync(limit: int = 50) -> list:
    """
    Get records that need syncing to Keeper.
    """
    conn = None
    try:
        conn = _get_connection()
        cursor = conn.cursor(dictionary=True)
        
        sql = "SELECT id, content, source, created_at FROM documents WHERE synced = 0 LIMIT %s"
        cursor.execute(sql, (limit,))
        rows = cursor.fetchall()
        
        if rows:
            logger.info(f"📋 Found {len(rows)} pending sync records in Local DB")
            
        return rows
        
    except Exception as e:
        logger.error(f"❌ Local DB Get Pending Error: {e}")
        return []
    finally:
        if conn and conn.is_connected():
            cursor.close()
            conn.close()

def mark_synced(ids: list) -> bool:
    """
    Mark records as synced.
    """
    if not ids:
        return True
        
    conn = None
    try:
        conn = _get_connection()
        cursor = conn.cursor()
        
        placeholders = ",".join(["%s"] * len(ids))
        sql = f"UPDATE documents SET synced = 1 WHERE id IN ({placeholders})"
        cursor.execute(sql, ids)
        conn.commit()
        
        logger.info(f"✅ Marked {len(ids)} records as synced in Local DB")
        return True
        
    except Exception as e:
        logger.error(f"❌ Local DB Mark Synced Error: {e}")
        return False
    finally:
        if conn and conn.is_connected():
            cursor.close()
            conn.close()

def save_vector_local(doc_id: int, embedding: list) -> bool:
    """
    Save vector for a document in Local DB.
    Useful for local search/RAG capability (offline).
    """
    conn = None
    cursor = None
    try:
        conn = _get_connection()
        cursor = conn.cursor()

        cursor.execute("SELECT 1 FROM vectors WHERE doc_id = %s LIMIT 1", (doc_id,))
        if cursor.fetchone():
            logger.info("💾 Local DB vector duplicate skipped (doc_id: %s)", doc_id)
            return True
        
        sql = "INSERT INTO vectors (doc_id, embedding) VALUES (%s, %s)"
        cursor.execute(sql, (doc_id, json.dumps(embedding)))
        conn.commit()
        return True
        
    except Exception as e:
        logger.error(f"❌ Local DB Vector Save Error: {e}")
        return False
    finally:
        if conn and conn.is_connected():
            if cursor:
                cursor.close()
            conn.close()

def search_local(query: str, limit: int = 5, source_contains: str = "") -> list:
    """
    Search local MariaDB for matching memories (Fulltext or LIKE).
    """
    conn = None
    try:
        conn = _get_connection()
        cursor = conn.cursor(dictionary=True)

        q = (query or "").strip()
        if not q:
            return []

        src = (source_contains or "").strip()
        # Pre-parse statute-style hints (law name + article) so we can do a precise query first.
        # This dramatically improves "法規 + 第X條" lookups under local fallback (no vector index).
        import re
        law_hint_precise = ""
        article_hints_precise = []
        try:
            law_matches = re.findall(r"([\u4e00-\u9fff]{2,}(?:法|條例|通則|規則|細則|施行法|施行細則))", q)
            if law_matches:
                law_hint_precise = max(law_matches, key=len)
        except Exception:
            law_hint_precise = ""
        try:
            m0 = re.search(r"第\s*(\d{1,4})(?:\s*-\s*(\d{1,3}))?\s*條(?:\s*之\s*(\d{1,3}))?", q)
            if m0:
                art = (m0.group(1) or "").strip()
                dash = (m0.group(2) or "").strip()
                sub = (m0.group(3) or "").strip()
                base = f"{art}-{dash}" if (art and dash) else art
                if base:
                    article_hints_precise = [f"第 {base} 條", f"第{base}條"]
                    if sub:
                        article_hints_precise.extend([f"第 {base} 條之 {sub}", f"第{base}條之{sub}"])
        except Exception:
            article_hints_precise = []

        if src and ("statute|" in src) and law_hint_precise and article_hints_precise:
            try:
                ors = " OR ".join(["content LIKE %s"] * len(article_hints_precise))
                sql_precise = (
                    "SELECT id, content, source FROM documents "
                    "WHERE source LIKE %s AND source LIKE %s AND (" + ors + ") "
                    "LIMIT %s"
                )
                params = [f"%{src}%", f"%{law_hint_precise}%"]
                params.extend([f"%{h}%" for h in article_hints_precise])
                params.append(limit)
                cursor.execute(sql_precise, tuple(params))
                rows = cursor.fetchall() or []
                if rows:
                    return rows[:limit]
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 201, exc_info=True)

        # 1) exact LIKE first
        if src:
            sql = (
                "SELECT id, content, source FROM documents "
                "WHERE content LIKE %s AND source LIKE %s "
                "ORDER BY created_at DESC LIMIT %s"
            )
            cursor.execute(sql, (f"%{q}%", f"%{src}%", limit))
        else:
            sql = "SELECT id, content, source FROM documents WHERE content LIKE %s ORDER BY created_at DESC LIMIT %s"
            cursor.execute(sql, (f"%{q}%", limit))
        rows = cursor.fetchall() or []
        if rows:
            return rows

        # 2) token fallback (helps long natural-language queries)
        # Keep 2+ chars tokens to avoid noisy matches.
        import re
        # NOTE: This is a regex pattern, not a Python string-escape.
        # Use `\s` for whitespace (do NOT double-escape it), otherwise tokenization
        # fails for long queries and the fallback returns empty results.
        tokens = [t.strip() for t in re.split(r"[\s,，。;；:：\-_/()\[\]{}]+", q) if len(t.strip()) >= 2]
        article_hints = []
        law_hint = ""
        try:
            # Best-effort: pick the longest law-like fragment from the query.
            law_matches = re.findall(r"([\u4e00-\u9fff]{2,}(?:法|條例|通則|規則|細則|施行法|施行細則))", q)
            if law_matches:
                law_hint = max(law_matches, key=len)
        except Exception:
            law_hint = ""
        # Special handling: statute queries often include "第X條/第X條之Y".
        # These tokens are meaningful but may get split into 1-char pieces ("第", "條") and dropped.
        m = re.search(r"第\s*(\d{1,4})(?:\s*-\s*(\d{1,3}))?\s*條(?:\s*之\s*(\d{1,3}))?", q)
        if m:
            art = (m.group(1) or "").strip()
            dash = (m.group(2) or "").strip()
            sub = (m.group(3) or "").strip()
            base = f"{art}-{dash}" if (art and dash) else art
            if base:
                article_hints = [f"第{base}條", f"第 {base} 條"]
                tokens.extend(article_hints)
                if sub:
                    more = [f"第{base}條之{sub}", f"第 {base} 條之 {sub}"]
                    article_hints.extend(more)
                    tokens.extend(more)

        # Prefer "article-like" tokens first so statute lookups don't get drowned out by generic law-name matches.
        try:
            article_like = [t for t in tokens if re.match(r"^第\s*\d", t)]
            other_like = [t for t in tokens if t not in set(article_like)]
            tokens = article_like + other_like
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 256, exc_info=True)
        # Deduplicate while preserving order.
        seen = set()
        uniq = []
        for t in tokens:
            if t in seen:
                continue
            seen.add(t)
            uniq.append(t)
        if not uniq:
            return []

        all_rows = []
        for tok in uniq[:8]:
            if src:
                cursor.execute(sql, (f"%{tok}%", f"%{src}%", limit))
            else:
                cursor.execute(sql, (f"%{tok}%", limit))
            all_rows.extend(cursor.fetchall() or [])

        # Deduplicate by (source, content)
        uniq_rows = []
        seen_key = set()
        for r in all_rows:
            key = (r.get("source", ""), r.get("content", ""))
            if key in seen_key:
                continue
            seen_key.add(key)
            uniq_rows.append(r)

        # Ranking: if this looks like a statute lookup and we have both law + article hints,
        # prioritize rows that match the law name (source includes `law=...`) and the article.
        if src and ("statute|" in src) and (law_hint or article_hints):
            def _score(row: dict) -> int:
                s = (row.get("source") or "")
                c = (row.get("content") or "")
                sc = 0
                if law_hint and (law_hint in s or law_hint in c):
                    sc += 3
                if article_hints and any(h and (h in s or h in c) for h in article_hints):
                    sc += 2
                # small boost for direct query substring
                if q and q in c:
                    sc += 1
                return sc

            uniq_rows.sort(key=lambda r: (_score(r), r.get("id") or 0), reverse=True)

        return uniq_rows[:limit]
        
    except Exception as e:
        logger.error(f"❌ Local DB Search Error: {e}")
        return []
    finally:
        if conn and conn.is_connected():
            cursor.close()
            conn.close()
