#!/usr/bin/env python3
"""
MAGI Database Migration Runner (P0-11)
======================================
Lightweight migration framework for MAGI schema evolution.

Usage:
    python migrations/migrate.py status     # Show current version and pending migrations
    python migrations/migrate.py upgrade    # Apply all pending migrations
    python migrations/migrate.py rollback   # Rollback the last applied migration

Environment:
    DB_HOST, DB_PORT, DB_USER, DB_PASSWORD, DB_NAME — or use .env
"""

from __future__ import annotations

import os
import re
import sys
import logging
from pathlib import Path
from datetime import datetime

# Ensure MAGI root is importable
_MAGI_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_MAGI_ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(_MAGI_ROOT / ".env")
except ImportError:
    pass

logger = logging.getLogger("magi.migrate")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

VERSIONS_DIR = Path(__file__).resolve().parent / "versions"

# Schema version tracking table
_SCHEMA_VERSION_TABLE = "magi_schema_versions"
_CREATE_VERSION_TABLE = f"""
CREATE TABLE IF NOT EXISTS `{_SCHEMA_VERSION_TABLE}` (
    id INT AUTO_INCREMENT PRIMARY KEY,
    version VARCHAR(10) NOT NULL,
    description VARCHAR(255) NOT NULL,
    applied_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    checksum VARCHAR(64),
    UNIQUE KEY uq_version (version)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""


def _get_db_connection():
    """Get a MySQL connection using MAGI's standard env vars."""
    import mysql.connector
    return mysql.connector.connect(
        host=os.environ.get("DB_HOST", "127.0.0.1"),
        port=int(os.environ.get("DB_PORT", "3306")),
        user=os.environ.get("DB_USER", "magi"),
        password=os.environ.get("DB_PASSWORD", ""),
        database=os.environ.get("DB_NAME", "magi_brain"),
        charset="utf8mb4",
        use_pure=True,
    )


def _ensure_version_table(cursor):
    """Create the schema version table if it doesn't exist."""
    cursor.execute(_CREATE_VERSION_TABLE)


def _parse_migration(filepath: Path) -> dict:
    """Parse a migration file into UP and DOWN sections."""
    content = filepath.read_text(encoding="utf-8")
    up_match = re.search(r"--\s*UP\s*\n(.*?)(?=--\s*DOWN|$)", content, re.DOTALL | re.IGNORECASE)
    down_match = re.search(r"--\s*DOWN\s*\n(.*?)$", content, re.DOTALL | re.IGNORECASE)

    name = filepath.stem
    parts = name.split("_", 1)
    version = parts[0]
    description = parts[1].replace("_", " ") if len(parts) > 1 else name

    return {
        "version": version,
        "description": description,
        "up": up_match.group(1).strip() if up_match else content.strip(),
        "down": down_match.group(1).strip() if down_match else "",
        "path": filepath,
    }


def _discover_migrations() -> list[dict]:
    """Find all migration files in versions/ sorted by version."""
    if not VERSIONS_DIR.exists():
        return []
    files = sorted(VERSIONS_DIR.glob("*.sql"))
    return [_parse_migration(f) for f in files]


def _applied_versions(cursor) -> set[str]:
    """Get the set of already-applied migration versions."""
    _ensure_version_table(cursor)
    cursor.execute(f"SELECT version FROM `{_SCHEMA_VERSION_TABLE}` ORDER BY version")
    return {row[0] for row in cursor.fetchall()}


def cmd_status():
    """Show current migration status."""
    conn = _get_db_connection()
    cursor = conn.cursor()
    try:
        applied = _applied_versions(cursor)
        migrations = _discover_migrations()

        print(f"\n{'Version':<10} {'Description':<40} {'Status'}")
        print("-" * 65)
        for m in migrations:
            status = "APPLIED" if m["version"] in applied else "PENDING"
            print(f"{m['version']:<10} {m['description']:<40} {status}")

        pending = [m for m in migrations if m["version"] not in applied]
        print(f"\nApplied: {len(applied)} | Pending: {len(pending)} | Total: {len(migrations)}")
    finally:
        cursor.close()
        conn.close()


def cmd_upgrade():
    """Apply all pending migrations."""
    conn = _get_db_connection()
    cursor = conn.cursor()
    try:
        applied = _applied_versions(cursor)
        migrations = _discover_migrations()
        pending = [m for m in migrations if m["version"] not in applied]

        if not pending:
            logger.info("No pending migrations.")
            return

        for m in pending:
            logger.info("Applying migration %s: %s", m["version"], m["description"])
            statements = [s.strip() for s in m["up"].split(";") if s.strip()]
            for stmt in statements:
                cursor.execute(stmt)

            import hashlib
            checksum = hashlib.sha256(m["up"].encode()).hexdigest()[:16]
            cursor.execute(
                f"INSERT INTO `{_SCHEMA_VERSION_TABLE}` (version, description, applied_at, checksum) VALUES (%s, %s, %s, %s)",
                (m["version"], m["description"][:255], datetime.now(), checksum),
            )
            conn.commit()
            logger.info("  Applied %s OK", m["version"])

        logger.info("All %d migration(s) applied.", len(pending))
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()


def cmd_rollback():
    """Rollback the last applied migration."""
    conn = _get_db_connection()
    cursor = conn.cursor()
    try:
        applied = _applied_versions(cursor)
        if not applied:
            logger.info("No migrations to rollback.")
            return

        migrations = _discover_migrations()
        applied_migrations = [m for m in migrations if m["version"] in applied]
        if not applied_migrations:
            logger.info("No matching migration files found.")
            return

        last = applied_migrations[-1]
        if not last["down"]:
            logger.error("Migration %s has no DOWN section — cannot rollback.", last["version"])
            return

        logger.info("Rolling back migration %s: %s", last["version"], last["description"])
        statements = [s.strip() for s in last["down"].split(";") if s.strip()]
        for stmt in statements:
            cursor.execute(stmt)

        cursor.execute(f"DELETE FROM `{_SCHEMA_VERSION_TABLE}` WHERE version = %s", (last["version"],))
        conn.commit()
        logger.info("  Rolled back %s OK", last["version"])
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()


if __name__ == "__main__":
    commands = {"status": cmd_status, "upgrade": cmd_upgrade, "rollback": cmd_rollback}
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd not in commands:
        print(f"Usage: python migrate.py [{' | '.join(commands)}]")
        sys.exit(1)
    commands[cmd]()
