"""Table-aware bidirectional DB sync with conflict resolution."""

import logging
import os
import json
import time
from datetime import datetime
from typing import Optional

import mysql.connector

logger = logging.getLogger(__name__)

# Table sync strategies
# - "append_only": INSERT IGNORE from source to target (logs, audit trails)
# - "last_writer_wins": Compare updated_at, newer row wins (mutable data)
# - "remote_authoritative": Remote always wins (settings, config)
# - "skip": Don't sync this table

_TABLE_STRATEGIES = {
    # Append-only tables (safe to INSERT IGNORE)
    "audit_log": "append_only",
    "notification_log": "append_only",
    "dedup_registry": "append_only",
    "magi_schema_versions": "skip",

    # Default for unknown tables
    "_default": "last_writer_wins",
}

_BATCH_SIZE = 100


def get_strategy(table_name: str) -> str:
    return _TABLE_STRATEGIES.get(table_name, _TABLE_STRATEGIES["_default"])


def _get_primary_keys(cursor, database: str, table: str) -> list[str]:
    """Get primary key column names for a table."""
    cursor.execute(
        "SELECT COLUMN_NAME FROM information_schema.KEY_COLUMN_USAGE "
        "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s AND CONSTRAINT_NAME = 'PRIMARY' "
        "ORDER BY ORDINAL_POSITION",
        (database, table),
    )
    return [r["COLUMN_NAME"] for r in cursor.fetchall()]


def _has_column(cursor, database: str, table: str, column: str) -> bool:
    """Check if a table has a specific column."""
    cursor.execute(
        "SELECT COUNT(*) AS cnt FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s AND COLUMN_NAME = %s",
        (database, table, column),
    )
    return cursor.fetchone()["cnt"] > 0


def _get_all_columns(cursor, database: str, table: str) -> list[str]:
    """Get all column names for a table."""
    cursor.execute(
        "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s ORDER BY ORDINAL_POSITION",
        (database, table),
    )
    return [r["COLUMN_NAME"] for r in cursor.fetchall()]


def _quote(name: str) -> str:
    """Quote a SQL identifier with backticks."""
    return f"`{name.replace(chr(96), chr(96)+chr(96))}`"


def _pk_tuple(row: dict, pk_cols: list[str]) -> tuple:
    """Extract a hashable PK tuple from a row dict."""
    return tuple(row[c] for c in pk_cols)


def _build_insert_ignore(table: str, columns: list[str]) -> str:
    """Build an INSERT IGNORE statement."""
    cols = ", ".join(_quote(c) for c in columns)
    placeholders = ", ".join(["%s"] * len(columns))
    return f"INSERT IGNORE INTO {_quote(table)} ({cols}) VALUES ({placeholders})"


def _build_replace_into(table: str, columns: list[str]) -> str:
    """Build a REPLACE INTO statement."""
    cols = ", ".join(_quote(c) for c in columns)
    placeholders = ", ".join(["%s"] * len(columns))
    return f"REPLACE INTO {_quote(table)} ({cols}) VALUES ({placeholders})"


def _row_values(row: dict, columns: list[str]) -> tuple:
    """Extract values from a row dict in column order."""
    return tuple(row[c] for c in columns)


def _fetch_rows_by_pks(cursor, database: str, table: str, pk_cols: list[str],
                        pk_values: list[tuple], columns: list[str]) -> list[dict]:
    """Fetch full rows by primary key values in batches."""
    if not pk_values:
        return []

    results = []
    for i in range(0, len(pk_values), _BATCH_SIZE):
        batch = pk_values[i:i + _BATCH_SIZE]
        if len(pk_cols) == 1:
            placeholders = ", ".join(["%s"] * len(batch))
            sql = (f"SELECT * FROM {_quote(database)}.{_quote(table)} "
                   f"WHERE {_quote(pk_cols[0])} IN ({placeholders})")
            params = [v[0] for v in batch]
        else:
            # Composite PK: use OR of ANDs
            conditions = []
            params = []
            for pk_val in batch:
                cond = " AND ".join(f"{_quote(pk_cols[j])} = %s" for j in range(len(pk_cols)))
                conditions.append(f"({cond})")
                params.extend(pk_val)
            sql = (f"SELECT * FROM {_quote(database)}.{_quote(table)} "
                   f"WHERE {' OR '.join(conditions)}")

        cursor.execute(sql, params)
        results.extend(cursor.fetchall())
    return results


def _sync_append_only(local_cur, remote_cur, local_conn, remote_conn,
                      database: str, table: str) -> dict:
    """Sync using INSERT IGNORE in both directions."""
    pk_cols = _get_primary_keys(local_cur, database, table)
    if not pk_cols:
        return {"status": "skipped", "reason": "no_primary_key"}

    columns = _get_all_columns(local_cur, database, table)

    # Fetch PKs from both sides
    pk_select = ", ".join(_quote(c) for c in pk_cols)
    local_cur.execute(f"SELECT {pk_select} FROM {_quote(database)}.{_quote(table)}")
    local_pks = {_pk_tuple(r, pk_cols) for r in local_cur.fetchall()}

    remote_cur.execute(f"SELECT {pk_select} FROM {_quote(database)}.{_quote(table)}")
    remote_pks = {_pk_tuple(r, pk_cols) for r in remote_cur.fetchall()}

    only_local = local_pks - remote_pks
    only_remote = remote_pks - local_pks

    pushed = 0
    pulled = 0

    # Push local-only rows to remote
    if only_local:
        rows = _fetch_rows_by_pks(local_cur, database, table, pk_cols,
                                   list(only_local), columns)
        insert_sql = _build_insert_ignore(table, columns)
        for i in range(0, len(rows), _BATCH_SIZE):
            batch = rows[i:i + _BATCH_SIZE]
            data = [_row_values(r, columns) for r in batch]
            remote_cur.executemany(insert_sql, data)
            pushed += len(batch)
        remote_conn.commit()

    # Pull remote-only rows to local
    if only_remote:
        rows = _fetch_rows_by_pks(remote_cur, database, table, pk_cols,
                                   list(only_remote), columns)
        insert_sql = _build_insert_ignore(table, columns)
        for i in range(0, len(rows), _BATCH_SIZE):
            batch = rows[i:i + _BATCH_SIZE]
            data = [_row_values(r, columns) for r in batch]
            local_cur.executemany(insert_sql, data)
            pulled += len(batch)
        local_conn.commit()

    logger.info("Table %s (append_only): pushed %d, pulled %d", table, pushed, pulled)
    return {"status": "ok", "pushed": pushed, "pulled": pulled}


def _sync_last_writer_wins(local_cur, remote_cur, local_conn, remote_conn,
                           database: str, table: str) -> dict:
    """Sync using updated_at comparison; newer row wins."""
    # Check for updated_at column
    if not _has_column(local_cur, database, table, "updated_at"):
        logger.warning("Table %s has no updated_at column, falling back to append_only", table)
        return _sync_append_only(local_cur, remote_cur, local_conn, remote_conn,
                                  database, table)

    pk_cols = _get_primary_keys(local_cur, database, table)
    if not pk_cols:
        return {"status": "skipped", "reason": "no_primary_key"}

    columns = _get_all_columns(local_cur, database, table)
    pk_select = ", ".join(_quote(c) for c in pk_cols)

    # Fetch PKs + updated_at from both sides
    ts_query = f"SELECT {pk_select}, `updated_at` FROM {_quote(database)}.{_quote(table)}"

    local_cur.execute(ts_query)
    local_rows = {_pk_tuple(r, pk_cols): r["updated_at"] for r in local_cur.fetchall()}

    remote_cur.execute(ts_query)
    remote_rows = {_pk_tuple(r, pk_cols): r["updated_at"] for r in remote_cur.fetchall()}

    local_keys = set(local_rows.keys())
    remote_keys = set(remote_rows.keys())

    only_local = local_keys - remote_keys
    only_remote = remote_keys - local_keys
    common = local_keys & remote_keys

    # Determine which common rows need updating
    push_to_remote = []  # local is newer
    pull_to_local = []   # remote is newer
    for pk in common:
        l_ts = local_rows[pk]
        r_ts = remote_rows[pk]
        if l_ts is None and r_ts is None:
            continue
        if r_ts is None or (l_ts is not None and l_ts > r_ts):
            push_to_remote.append(pk)
        elif l_ts is None or (r_ts is not None and r_ts > l_ts):
            pull_to_local.append(pk)

    pushed = 0
    pulled = 0
    updated_remote = 0
    updated_local = 0

    replace_sql = _build_replace_into(table, columns)

    # Push local-only rows to remote
    if only_local:
        rows = _fetch_rows_by_pks(local_cur, database, table, pk_cols,
                                   list(only_local), columns)
        insert_sql = _build_insert_ignore(table, columns)
        for i in range(0, len(rows), _BATCH_SIZE):
            batch = rows[i:i + _BATCH_SIZE]
            remote_cur.executemany(insert_sql, [_row_values(r, columns) for r in batch])
            pushed += len(batch)
        remote_conn.commit()

    # Pull remote-only rows to local
    if only_remote:
        rows = _fetch_rows_by_pks(remote_cur, database, table, pk_cols,
                                   list(only_remote), columns)
        insert_sql = _build_insert_ignore(table, columns)
        for i in range(0, len(rows), _BATCH_SIZE):
            batch = rows[i:i + _BATCH_SIZE]
            local_cur.executemany(insert_sql, [_row_values(r, columns) for r in batch])
            pulled += len(batch)
        local_conn.commit()

    # Update common rows where local is newer → push to remote
    if push_to_remote:
        rows = _fetch_rows_by_pks(local_cur, database, table, pk_cols,
                                   push_to_remote, columns)
        for i in range(0, len(rows), _BATCH_SIZE):
            batch = rows[i:i + _BATCH_SIZE]
            remote_cur.executemany(replace_sql, [_row_values(r, columns) for r in batch])
            updated_remote += len(batch)
        remote_conn.commit()

    # Update common rows where remote is newer → pull to local
    if pull_to_local:
        rows = _fetch_rows_by_pks(remote_cur, database, table, pk_cols,
                                   pull_to_local, columns)
        for i in range(0, len(rows), _BATCH_SIZE):
            batch = rows[i:i + _BATCH_SIZE]
            local_cur.executemany(replace_sql, [_row_values(r, columns) for r in batch])
            updated_local += len(batch)
        local_conn.commit()

    logger.info("Table %s (last_writer_wins): pushed %d, pulled %d, "
                "updated_remote %d, updated_local %d",
                table, pushed, pulled, updated_remote, updated_local)
    return {
        "status": "ok",
        "pushed": pushed,
        "pulled": pulled,
        "updated_remote": updated_remote,
        "updated_local": updated_local,
    }


def _sync_remote_authoritative(local_cur, remote_cur, local_conn, remote_conn,
                               database: str, table: str) -> dict:
    """Remote is authoritative: REPLACE INTO local from remote. Keep local-only rows."""
    pk_cols = _get_primary_keys(local_cur, database, table)
    if not pk_cols:
        return {"status": "skipped", "reason": "no_primary_key"}

    columns = _get_all_columns(remote_cur, database, table)

    # Fetch all rows from remote
    remote_cur.execute(f"SELECT * FROM {_quote(database)}.{_quote(table)}")
    remote_rows = remote_cur.fetchall()

    replaced = 0
    replace_sql = _build_replace_into(table, columns)

    for i in range(0, len(remote_rows), _BATCH_SIZE):
        batch = remote_rows[i:i + _BATCH_SIZE]
        local_cur.executemany(replace_sql, [_row_values(r, columns) for r in batch])
        replaced += len(batch)
    local_conn.commit()

    # Check for local-only rows and log a warning
    pk_select = ", ".join(_quote(c) for c in pk_cols)
    local_cur.execute(f"SELECT {pk_select} FROM {_quote(database)}.{_quote(table)}")
    local_pks = {_pk_tuple(r, pk_cols) for r in local_cur.fetchall()}

    remote_pks = {_pk_tuple(r, pk_cols) for r in remote_rows}
    local_only = local_pks - remote_pks

    if local_only:
        logger.warning("Table %s (remote_authoritative): %d local-only rows kept "
                       "(not deleted): %s",
                       table, len(local_only),
                       list(local_only)[:5])

    logger.info("Table %s (remote_authoritative): replaced %d rows from remote, "
                "%d local-only rows kept",
                table, replaced, len(local_only))
    return {
        "status": "ok",
        "replaced_from_remote": replaced,
        "local_only_kept": len(local_only),
    }


def sync_bidirectional(local_conn, remote_conn, database: str = "law_firm_data") -> dict:
    """
    Sync all tables between local and remote DBs.
    Returns a report dict with counts per table.
    """
    report = {"tables": {}, "started_at": datetime.now().isoformat(), "errors": []}

    try:
        local_cur = local_conn.cursor(dictionary=True)
        remote_cur = remote_conn.cursor(dictionary=True)

        # Get table list
        local_cur.execute(
            "SELECT TABLE_NAME FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA = %s AND TABLE_TYPE = 'BASE TABLE'",
            (database,),
        )
        tables = [r["TABLE_NAME"] for r in local_cur.fetchall()]

        for table in tables:
            strategy = get_strategy(table)
            if strategy == "skip":
                report["tables"][table] = {"strategy": "skip", "status": "skipped"}
                continue

            try:
                if strategy == "append_only":
                    result = _sync_append_only(
                        local_cur, remote_cur, local_conn, remote_conn, database, table)
                elif strategy == "last_writer_wins":
                    result = _sync_last_writer_wins(
                        local_cur, remote_cur, local_conn, remote_conn, database, table)
                elif strategy == "remote_authoritative":
                    result = _sync_remote_authoritative(
                        local_cur, remote_cur, local_conn, remote_conn, database, table)
                else:
                    result = {"status": "unknown_strategy"}

                report["tables"][table] = {"strategy": strategy, **result}
            except Exception as e:
                logger.warning("Sync failed for table %s: %s", table, e)
                report["tables"][table] = {
                    "strategy": strategy, "status": "error", "error": str(e),
                }
                report["errors"].append(f"{table}: {e}")

    finally:
        report["finished_at"] = datetime.now().isoformat()

    return report
