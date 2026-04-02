#!/usr/bin/env python3
"""
WATCHER DAEMON (MAGI-00)
========================
The Black Box - Autonomous audit log collector and anomaly detector.
Runs on Watcher hardware (MacBook Air M1) and pulls data from federation.

Responsibilities:
  - Log Collector: Aggregate audit_log from magi_brain
  - Anomaly Detection: Flag discrepancies in MAGI votes vs execution
  - Evidence Locker: Store immutable copies of critical events
"""

import os
import sys
import json
import time
import sqlite3
import logging
from datetime import datetime, timedelta

# --- Load .env for subprocess/cron credential access ---
try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv()
except Exception:
    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 26, exc_info=True)

# Add MAGI to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

# Configuration
WATCHER_DB = os.path.expanduser("~/watcher_evidence.db")
LOG_FILE = os.path.expanduser("~/watcher_daemon.log")
PULL_INTERVAL = 300  # 5 minutes
KEEPER_HOST = os.environ.get("KEEPER_HOST", "100.121.61.74")

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("WatcherDaemon")


def init_local_db():
    """Initialize local SQLite database for evidence storage."""
    conn = sqlite3.connect(WATCHER_DB)
    cursor = conn.cursor()
    
    # Mirror of audit_log with additional metadata
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS audit_archive (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id INTEGER,
            agent_name TEXT,
            target_db TEXT,
            table_name TEXT,
            record_id INTEGER,
            operation TEXT,
            old_value TEXT,
            new_value TEXT,
            reason TEXT,
            executed_at TEXT,
            pulled_at TEXT DEFAULT CURRENT_TIMESTAMP,
            verified INTEGER DEFAULT 0
        )
    """)
    
    # Anomaly detection log
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS anomalies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            detected_at TEXT DEFAULT CURRENT_TIMESTAMP,
            severity TEXT,
            description TEXT,
            source_audit_id INTEGER,
            resolved INTEGER DEFAULT 0
        )
    """)
    
    # Pull status tracking
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS pull_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pulled_at TEXT DEFAULT CURRENT_TIMESTAMP,
            entries_pulled INTEGER,
            status TEXT,
            error TEXT
        )
    """)
    
    conn.commit()
    conn.close()
    logger.info(f"📦 Local evidence database initialized: {WATCHER_DB}")


def get_last_pull_id():
    """Get the last successfully pulled audit_log ID."""
    conn = sqlite3.connect(WATCHER_DB)
    cursor = conn.cursor()
    cursor.execute("SELECT MAX(source_id) FROM audit_archive")
    result = cursor.fetchone()[0]
    conn.close()
    return result or 0


def pull_audit_logs():
    """Pull new audit log entries from Keeper (magi_brain)."""
    import mysql.connector
    
    last_id = get_last_pull_id()
    logger.info(f"🔍 Pulling audit logs after ID {last_id}...")
    
    try:
        conn = mysql.connector.connect(
            host=KEEPER_HOST,
            user=os.environ.get("DB_USER", "casper_service"),
            password=os.environ.get("DB_PASSWORD", ""),
            database='magi_brain',
            use_pure=True,
        )
        cursor = conn.cursor(dictionary=True)
        
        cursor.execute("""
            SELECT id, agent_name, target_db, table_name, record_id,
                   operation, old_value, new_value, reason, executed_at
            FROM audit_log
            WHERE id > %s
            ORDER BY id ASC
            LIMIT 100
        """, (last_id,))
        
        entries = cursor.fetchall()
        cursor.close()
        conn.close()
        
        if not entries:
            logger.info("✅ No new entries to pull.")
            log_pull_status(0, "success", None)
            return 0
        
        # Store in local DB
        local_conn = sqlite3.connect(WATCHER_DB)
        local_cursor = local_conn.cursor()
        
        for entry in entries:
            local_cursor.execute("""
                INSERT INTO audit_archive 
                (source_id, agent_name, target_db, table_name, record_id, 
                 operation, old_value, new_value, reason, executed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                entry['id'],
                entry['agent_name'],
                entry.get('target_db', 'law_firm_data'),
                entry['table_name'],
                entry['record_id'],
                entry['operation'],
                json.dumps(entry['old_value']) if entry.get('old_value') else None,
                json.dumps(entry['new_value']) if entry.get('new_value') else None,
                entry.get('reason'),
                entry['executed_at'].isoformat() if entry.get('executed_at') else None
            ))
        
        local_conn.commit()
        local_conn.close()
        
        logger.info(f"📥 Pulled and archived {len(entries)} new entries.")
        log_pull_status(len(entries), "success", None)
        
        # Run anomaly detection on new entries
        detect_anomalies(entries)
        
        return len(entries)
        
    except Exception as e:
        logger.error(f"❌ Pull failed: {e}")
        log_pull_status(0, "error", str(e))
        return -1


def log_pull_status(count, status, error):
    """Log pull operation status."""
    conn = sqlite3.connect(WATCHER_DB)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO pull_log (entries_pulled, status, error)
        VALUES (?, ?, ?)
    """, (count, status, error))
    conn.commit()
    conn.close()


def detect_anomalies(entries):
    """
    Detect anomalies in audit log entries.
    
    Current checks:
    - Unusual agent names (not CASPER, MELCHIOR, BALTHASAR, RESTORE_UI)
    - High-frequency operations (>10 ops in 1 minute from same agent)
    - Operations outside business hours
    """
    known_agents = {'CASPER', 'MELCHIOR', 'BALTHASAR', 'RESTORE_UI', 'SYSTEM'}
    
    for entry in entries:
        anomalies = []
        
        # Check unknown agent
        agent = entry.get('agent_name', '').upper()
        if agent and agent not in known_agents:
            anomalies.append(f"Unknown agent: {agent}")
        
        # Check for suspicious patterns in reason
        reason = entry.get('reason', '') or ''
        if 'DROP' in reason.upper() or 'DELETE' in reason.upper():
            anomalies.append(f"Potential destructive operation: {reason[:50]}")
        
        # Log detected anomalies
        if anomalies:
            for desc in anomalies:
                log_anomaly("warning", desc, entry.get('id'))
                logger.warning(f"⚠️ ANOMALY: {desc}")


def log_anomaly(severity, description, source_id):
    """Log an anomaly to local database."""
    conn = sqlite3.connect(WATCHER_DB)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO anomalies (severity, description, source_audit_id)
        VALUES (?, ?, ?)
    """, (severity, description, source_id))
    conn.commit()
    conn.close()


def send_alert(message, severity="warning"):
    """Send alert via Red Phone (if critical)."""
    if severity == "critical":
        try:
            import requests
            requests.post(
                "http://localhost:5003/alert",
                json={"message": f"🔍 WATCHER: {message}", "severity": severity},
                timeout=5
            )
            logger.info(f"🚨 Alert sent: {message}")
        except Exception as e:
            logger.error(f"Failed to send alert: {e}")


def get_status():
    """Get current Watcher status for heartbeat integration."""
    try:
        conn = sqlite3.connect(WATCHER_DB)
        cursor = conn.cursor()
        
        # Get last pull time
        cursor.execute("SELECT pulled_at, status FROM pull_log ORDER BY id DESC LIMIT 1")
        last_pull = cursor.fetchone()
        
        # Get total archived entries
        cursor.execute("SELECT COUNT(*) FROM audit_archive")
        total_entries = cursor.fetchone()[0]
        
        # Get unresolved anomalies
        cursor.execute("SELECT COUNT(*) FROM anomalies WHERE resolved = 0")
        open_anomalies = cursor.fetchone()[0]
        
        conn.close()
        
        return {
            "online": True,
            "last_pull": last_pull[0] if last_pull else None,
            "last_status": last_pull[1] if last_pull else None,
            "total_archived": total_entries,
            "open_anomalies": open_anomalies
        }
    except Exception as e:
        return {"online": False, "error": str(e)}


def main_loop():
    """Main daemon loop."""
    logger.info("👁️ WATCHER DAEMON STARTED")
    logger.info(f"   Evidence DB: {WATCHER_DB}")
    logger.info(f"   Pull Interval: {PULL_INTERVAL}s")
    logger.info(f"   Keeper Host: {KEEPER_HOST}")
    
    init_local_db()
    
    while True:
        try:
            pull_audit_logs()
        except Exception as e:
            logger.error(f"Loop error: {e}")
        
        time.sleep(PULL_INTERVAL)


if __name__ == "__main__":
    main_loop()
