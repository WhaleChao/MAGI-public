"""
SQLite Memory Backup Module
============================
Provides local backup storage when Keeper (MySQL) is offline.
Syncs pending records when Keeper comes back online.
"""

import sqlite3
import os
import logging
from datetime import datetime

logger = logging.getLogger("SQLiteBackup")

# SQLite database path
BACKUP_DB_PATH = os.path.join(os.path.dirname(__file__), "memory_backup.db")


def _get_connection():
    """Get SQLite connection with auto-create table."""
    conn = sqlite3.connect(BACKUP_DB_PATH)
    conn.row_factory = sqlite3.Row
    
    # Create table if not exists
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memory_backup (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content TEXT NOT NULL,
            source TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            synced INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    return conn


def save_to_backup(content: str, source: str = "unknown") -> int:
    """
    Save memory to local SQLite backup.
    
    Args:
        content: Memory content
        source: Source identifier
    
    Returns:
        Inserted row ID
    """
    try:
        conn = _get_connection()
        cursor = conn.execute(
            "INSERT INTO memory_backup (content, source) VALUES (?, ?)",
            (content, source)
        )
        conn.commit()
        row_id = cursor.lastrowid
        conn.close()
        
        logger.info(f"💾 Saved to backup (ID: {row_id}): {content[:50]}...")
        return row_id
        
    except Exception as e:
        logger.error(f"❌ Backup save error: {e}")
        return -1


def get_pending_sync() -> list:
    """
    Get all records that haven't been synced to Keeper.
    
    Returns:
        List of dicts with id, content, source, created_at
    """
    try:
        conn = _get_connection()
        cursor = conn.execute(
            "SELECT id, content, source, created_at FROM memory_backup WHERE synced = 0"
        )
        rows = [dict(row) for row in cursor.fetchall()]
        conn.close()
        
        logger.info(f"📋 Found {len(rows)} pending sync records")
        return rows
        
    except Exception as e:
        logger.error(f"❌ Get pending error: {e}")
        return []


def mark_synced(ids: list) -> bool:
    """
    Mark records as synced after successfully writing to Keeper.
    
    Args:
        ids: List of record IDs to mark
    
    Returns:
        True if successful
    """
    if not ids:
        return True
        
    try:
        conn = _get_connection()
        placeholders = ",".join("?" * len(ids))
        conn.execute(
            f"UPDATE memory_backup SET synced = 1 WHERE id IN ({placeholders})",
            ids
        )
        conn.commit()
        conn.close()
        
        logger.info(f"✅ Marked {len(ids)} records as synced")
        return True
        
    except Exception as e:
        logger.error(f"❌ Mark synced error: {e}")
        return False


def search_backup(query: str, limit: int = 5) -> list:
    """
    Search local backup for matching memories.
    Simple LIKE search for fallback purposes.
    
    Args:
        query: Search query
        limit: Max results
    
    Returns:
        List of matching memories
    """
    try:
        conn = _get_connection()
        cursor = conn.execute(
            "SELECT content, source FROM memory_backup WHERE content LIKE ? ORDER BY created_at DESC LIMIT ?",
            (f"%{query}%", limit)
        )
        rows = [{"content": row["content"], "source": row["source"]} for row in cursor.fetchall()]
        conn.close()
        
        return rows
        
    except Exception as e:
        logger.error(f"❌ Backup search error: {e}")
        return []


def get_backup_count() -> dict:
    """Get statistics about the backup database."""
    try:
        conn = _get_connection()
        total = conn.execute("SELECT COUNT(*) FROM memory_backup").fetchone()[0]
        pending = conn.execute("SELECT COUNT(*) FROM memory_backup WHERE synced = 0").fetchone()[0]
        conn.close()
        
        return {"total": total, "pending": pending}
        
    except Exception as e:
        return {"error": str(e)}


if __name__ == "__main__":
    # Test
    print("🧪 Testing SQLite Backup...")
    
    # Save test record
    save_to_backup("Test memory content", "test_source")
    
    # Get pending
    pending = get_pending_sync()
    print(f"Pending: {pending}")
    
    # Search
    results = search_backup("Test")
    print(f"Search results: {results}")
    
    # Stats
    stats = get_backup_count()
    print(f"Stats: {stats}")
