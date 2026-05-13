"""
Keeper Sync Daemon
==================
Periodically syncs local SQLite backup to Keeper (MySQL) when it comes back online.
Runs as a background thread or standalone process.
"""

import time
import json
import logging
import threading
import hashlib
import mysql.connector
import requests

# Import from sibling modules
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from skills.memory.local_db import get_pending_sync, mark_synced

# --- Load .env for subprocess/cron credential access ---
try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv()
except Exception:
    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 26, exc_info=True)

# Database config
DB_CONFIG = {
    'user': os.environ.get("DB_USER", "casper_service"),
    'password': os.environ.get("DB_PASSWORD", ""),
    'host': os.environ.get("DB_HOST", "127.0.0.1"),
    'database': 'magi_brain',
}

OLLAMA_URL = os.environ.get("OLLAMA_EMBED_URL", "http://127.0.0.1:8081/v1/embeddings")
EMBED_MODEL = os.environ.get("MAGI_OMLX_EMBED_MODEL", "modernbert-embed-4bit")
SYNC_INTERVAL = 300  # 5 minutes

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("KeeperSync")


def get_embedding(text):
    """Get embedding for text. Prefers oMLX/ModernBERT, falls back to Ollama."""
    # Try oMLX first
    try:
        from skills.bridge.melchior_client import embed_omlx, _omlx_embed_available
        if _omlx_embed_available():
            emb = embed_omlx(text)
            if isinstance(emb, list) and len(emb) > 0:
                return emb
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 54, exc_info=True)
    # Fallback to Ollama (OpenAI-compatible /v1/embeddings format)
    try:
        response = requests.post(OLLAMA_URL, json={
            "model": EMBED_MODEL,
            "input": text
        }, timeout=10)
        if response.status_code == 200:
            data = response.json().get("data", [])
            if data and isinstance(data, list):
                return data[0].get("embedding", [0.0] * 768)
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 64, exc_info=True)
    return [0.0] * 768


def check_keeper_online():
    """Check if Keeper is reachable."""
    try:
        conn = mysql.connector.connect(**DB_CONFIG)
        conn.close()
        return True
    except Exception:
        return False


def _ensure_schema(conn):
    """Self-heal documents schema:
    1. Widen source to TEXT if still narrow VARCHAR.
    2. Add `synced` column if missing — mem_bridge / keeper_sync INSERT 都會寫此欄位，
       新部署或重建 magi_brain DB 時若沒先跑 setup_rag_db.py 會炸。
    """
    try:
        cur = conn.cursor()
        cur.execute("SHOW COLUMNS FROM documents LIKE 'source'")
        row = cur.fetchone()
        if row and "TEXT" not in str(row[1]).upper():
            cur.execute("ALTER TABLE documents MODIFY COLUMN source TEXT")
            conn.commit()
            logger.info("✅ Migrated documents.source -> TEXT")
        cur.execute("SHOW COLUMNS FROM documents LIKE 'synced'")
        if not cur.fetchone():
            cur.execute("ALTER TABLE documents ADD COLUMN synced TINYINT(1) NOT NULL DEFAULT 0")
            conn.commit()
            logger.info("✅ Added documents.synced column")
        cur.close()
    except Exception as e:
        logger.warning(f"Schema migration skipped: {e}")


def _row_md5(content: str) -> str:
    return hashlib.md5((content or "").encode("utf-8", errors="replace")).hexdigest()


def _already_synced_in_target(cursor, record: dict) -> bool:
    """
    Idempotency guard for sync daemon.

    1) If same id/content/source already exists in target, treat as already synced.
       (This happens when local backup and keeper share the same DB.)
    2) Else if same content hash + same source already exists (different id), skip reinsert.
    """
    rid = int(record.get("id") or 0)
    content = str(record.get("content") or "")
    source = str(record.get("source") or "")
    if rid > 0:
        cursor.execute(
            "SELECT 1 FROM documents WHERE id = %s AND source = %s AND MD5(content) = %s LIMIT 1",
            (rid, source, _row_md5(content)),
        )
        if cursor.fetchone():
            return True
    cursor.execute(
        "SELECT 1 FROM documents WHERE source = %s AND MD5(content) = %s LIMIT 1",
        (source, _row_md5(content)),
    )
    return cursor.fetchone() is not None


def sync_to_keeper():
    """
    Sync pending records from SQLite backup to Keeper.

    Returns:
        Number of records synced
    """
    pending = get_pending_sync()

    if not pending:
        return 0

    logger.info(f"🔄 Found {len(pending)} records to sync...")

    conn = None
    try:
        conn = mysql.connector.connect(**DB_CONFIG)
        _ensure_schema(conn)
        cursor = conn.cursor()

        synced_ids = []

        for record in pending:
            try:
                content = record['content']
                source = str(record['source'] or "")

                # Idempotency first: avoid duplicate reinsertion.
                if _already_synced_in_target(cursor, record):
                    synced_ids.append(record['id'])
                    continue

                # Generate embedding
                embedding = get_embedding(content)

                # Insert document
                cursor.execute(
                    "INSERT INTO documents (content, source, synced) VALUES (%s, %s, %s)",
                    (content, source, 1)
                )
                doc_id = cursor.lastrowid

                # Insert vector
                cursor.execute(
                    "INSERT INTO vectors (doc_id, embedding) VALUES (%s, %s)",
                    (doc_id, json.dumps(embedding))
                )

                synced_ids.append(record['id'])
                logger.debug(f"Synced record {record['id']} → doc_id {doc_id}")

            except Exception as e:
                logger.error(f"❌ Failed to sync record {record['id']}: {e}")

        conn.commit()
        cursor.close()

        # Mark as synced in SQLite
        if synced_ids:
            mark_synced(synced_ids)
            logger.info(f"Synced {len(synced_ids)} records to Keeper")

        return len(synced_ids)

    except mysql.connector.Error as e:
        logger.error(f"❌ Keeper connection error: {e}")
        return 0
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def sync_loop():
    """Main sync loop - runs every SYNC_INTERVAL seconds."""
    logger.info(f"🔄 Keeper Sync Daemon started (interval: {SYNC_INTERVAL}s)")
    
    while True:
        try:
            if check_keeper_online():
                count = sync_to_keeper()
                if count > 0:
                    logger.info(f"✅ Synced {count} records to Keeper")
            else:
                logger.debug("⚠️ Keeper offline, waiting...")
        except Exception as e:
            logger.error(f"❌ Sync error: {e}")
        
        time.sleep(SYNC_INTERVAL)


def start_sync_daemon():
    """Start sync as background thread."""
    thread = threading.Thread(target=sync_loop, daemon=True)
    thread.start()
    logger.info("🚀 Keeper Sync Daemon thread started")
    return thread


if __name__ == "__main__":
    print("🔄 Starting Keeper Sync Daemon (standalone)...")
    sync_loop()
