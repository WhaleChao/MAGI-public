import sqlite3
import os
_MAGI_ROOT = os.path.abspath(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

_AGENT_DIR = (os.environ.get("MAGI_AGENT_DIR") or "").strip()
DB_PATH = os.environ.get(
    "MAGI_BRAIN_SQLITE_PATH",
    os.path.join(_AGENT_DIR, "magi_brain.db") if _AGENT_DIR else f"{_MAGI_ROOT}/magi_brain.db",
)

def init_db():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Users Table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            line_user_id VARCHAR(100) UNIQUE,
            role TEXT DEFAULT 'guest',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Pending Registrations
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS pending_registrations (
            token VARCHAR(20) PRIMARY KEY,
            role TEXT DEFAULT 'admin',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    # Audit Log (Phase 3 Prep)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            action TEXT,
            details TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.commit()
    conn.close()
    print(f"✅ MAGI Brain initialized at {DB_PATH}")

if __name__ == "__main__":
    init_db()
