"""
MAGI DB Helper — context manager wrappers for safe connection & cursor handling.
"""

from __future__ import annotations
import logging

import os
from contextlib import contextmanager
from typing import Any, Dict, Generator, Optional

import mysql.connector


def _default_config() -> dict[str, Any]:
    return {
        "host": os.environ.get("DB_HOST", "127.0.0.1"),
        "port": int(os.environ.get("DB_PORT", "3306")),
        "user": os.environ.get("DB_USER", "casper_service"),
        "password": os.environ.get("DB_PASSWORD") or os.environ.get("MAGI_REMOTE_DB_PASSWORD", ""),
        "database": os.environ.get("DB_NAME", "magi_brain"),
        "use_pure": True,
        "connection_timeout": 5,
        "charset": "utf8mb4",
        "collation": "utf8mb4_unicode_ci",
    }


@contextmanager
def get_connection(config: Optional[dict] = None) -> Generator:
    """Context manager that opens a mysql connection and guarantees close."""
    conn = mysql.connector.connect(**(config or _default_config()))
    try:
        yield conn
    finally:
        try:
            conn.close()
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 35, exc_info=True)


@contextmanager
def get_cursor(config: Optional[dict] = None, dictionary: bool = False, buffered: bool = True) -> Generator:
    """Context manager that yields (conn, cursor) and guarantees cleanup."""
    with get_connection(config) as conn:
        cursor = conn.cursor(dictionary=dictionary, buffered=buffered)
        try:
            yield conn, cursor
        finally:
            try:
                cursor.close()
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 49, exc_info=True)
