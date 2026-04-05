import logging
import mysql.connector
import os
import sys

# --- Load .env for subprocess/cron credential access ---
try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv()
except Exception:
    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 9, exc_info=True)


# DATABASE CONFIG (Keeper - Dell)
DB_CONFIG = {
    'user': os.environ.get("DB_USER", "casper_service"),
    'password': os.environ.get("DB_PASSWORD", ""),
    'host': os.environ.get("DB_HOST", "127.0.0.1"),
    'database': 'magi_brain',
}

def migrate_synced_column(cursor):
    """Add the synced column to documents if it doesn't exist yet."""
    try:
        cursor.execute("SHOW COLUMNS FROM documents LIKE 'synced'")
        if not cursor.fetchone():
            cursor.execute("ALTER TABLE documents ADD COLUMN synced TINYINT(1) NOT NULL DEFAULT 0")
            print("✅ Added documents.synced column")
    except Exception as e:
        print(f"⚠️  Could not add synced column: {e}")


def migrate_source_column(cursor):
    """Widen the source column to TEXT if it is currently narrower than TEXT."""
    try:
        cursor.execute("SHOW COLUMNS FROM documents LIKE 'source'")
        row = cursor.fetchone()
        if row:
            col_type = str(row[1]).upper()  # e.g. "VARCHAR(100)", "TEXT"
            if "TEXT" not in col_type:
                cursor.execute("ALTER TABLE documents MODIFY COLUMN source TEXT")
                print("✅ Migrated documents.source -> TEXT")
    except Exception as e:
        print(f"⚠️  Could not migrate source column: {e}")


def setup_rag_db():
    try:
        conn = mysql.connector.connect(**DB_CONFIG)
        cursor = conn.cursor()

        # 1. Documents Table
        print("Creating table: documents...")
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS documents (
                id INT AUTO_INCREMENT PRIMARY KEY,
                content TEXT,
                source TEXT,
                synced TINYINT(1) NOT NULL DEFAULT 0,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        migrate_source_column(cursor)
        migrate_synced_column(cursor)
        
        # 2. Vectors Table (Using JSON for simplicity initially, or BLOB)
        # We will store the embedding as a JSON string for now to be safe across versions
        print("Creating table: vectors...")
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS vectors (
                doc_id INT,
                embedding JSON, 
                FOREIGN KEY (doc_id) REFERENCES documents(id) ON DELETE CASCADE
            )
        """)
        
        conn.commit()
        print("✅ RAG Database Setup Complete on Keeper!")
        
    except mysql.connector.Error as err:
        print(f"❌ Error: {err}")
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()

if __name__ == "__main__":
    setup_rag_db()
