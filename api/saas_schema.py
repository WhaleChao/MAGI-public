from __future__ import annotations

import os
import re
from typing import Any


TENANT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{2,63}$")

AUTH_TENANT_COLUMNS: dict[str, str] = {
    "users": "default_tenant_id",
    "documents": "tenant_id",
    "vectors": "tenant_id",
    "messages": "tenant_id",
    "tasks": "tenant_id",
    "audit_log": "tenant_id",
    "pending_registrations": "tenant_id",
}

OSC_TENANT_COLUMNS: dict[str, str] = {
    "cases": "tenant_id",
    "clients": "tenant_id",
    "opponents": "tenant_id",
    "case_todos": "tenant_id",
    "calendar_events": "tenant_id",
    "case_calendar_events": "tenant_id",
    "case_checklists": "tenant_id",
    "case_documents": "tenant_id",
    "document_index": "tenant_id",
    "documents": "tenant_id",
    "legal_aid_checklists": "tenant_id",
    "legal_insights": "tenant_id",
    "court_judgments": "tenant_id",
    "case_transactions": "tenant_id",
    "quotations": "tenant_id",
    "activity_logs": "tenant_id",
    "settings": "tenant_id",
    "user_settings": "tenant_id",
    "learning_history": "tenant_id",
}

SUPPORT_TABLES = ("tenants", "tenant_memberships", "tenant_api_keys", "tenant_storage_roots")


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def tenant_id_from_env(default: str = "default") -> str:
    raw = str(os.environ.get("MAGI_TENANT_ID") or default or "default").strip()
    return raw if TENANT_ID_RE.fullmatch(raw) else "default"


def auth_db_config(base: dict[str, Any] | None = None) -> dict[str, Any]:
    base = dict(base or {})
    return {
        "host": base.get("host") or os.environ.get("DB_HOST") or "127.0.0.1",
        "port": int(base.get("port") or os.environ.get("DB_PORT") or "3306"),
        "user": base.get("user") or os.environ.get("DB_USER") or "",
        "password": base.get("password") or os.environ.get("DB_PASSWORD") or os.environ.get("MAGI_REMOTE_DB_PASSWORD") or "",
        "database": base.get("database") or os.environ.get("DB_NAME") or "magi_brain",
        "use_pure": True,
        "connection_timeout": 5,
        "charset": "utf8mb4",
        "collation": "utf8mb4_unicode_ci",
    }


def osc_db_config() -> dict[str, Any]:
    return {
        "host": os.environ.get("OSC_DB_HOST") or os.environ.get("MAGI_REMOTE_DB_HOST") or os.environ.get("DB_HOST") or "127.0.0.1",
        "port": int(os.environ.get("OSC_DB_PORT") or os.environ.get("MAGI_REMOTE_DB_PORT") or os.environ.get("DB_PORT") or "3306"),
        "user": os.environ.get("OSC_DB_USER") or os.environ.get("MAGI_REMOTE_DB_USER") or os.environ.get("DB_USER") or "",
        "password": os.environ.get("OSC_DB_PASSWORD") or os.environ.get("MAGI_REMOTE_DB_PASSWORD") or os.environ.get("DB_PASSWORD") or "",
        "database": os.environ.get("OSC_DB_NAME") or os.environ.get("MAGI_REMOTE_DB_NAME") or "law_firm_data",
        "use_pure": True,
        "connection_timeout": 5,
        "charset": "utf8mb4",
        "collation": "utf8mb4_unicode_ci",
    }


def _safe_ident(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_]+", str(name or "")):
        raise ValueError(f"unsafe SQL identifier: {name!r}")
    return f"`{name}`"


def _safe_tenant_literal(tenant_id: str) -> str:
    if not TENANT_ID_RE.fullmatch(tenant_id):
        raise ValueError("invalid tenant id")
    return "'" + tenant_id.replace("'", "''") + "'"


def _connect(config: dict[str, Any]):
    from api.mysql_connector_guard import install_mysql_cext_blocker, patch_mysql_connector_for_stability

    install_mysql_cext_blocker()
    patch_mysql_connector_for_stability()
    import mysql.connector

    return mysql.connector.connect(**config)


def _table_exists(cursor, table: str) -> bool:
    cursor.execute(
        "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema=DATABASE() AND table_name=%s",
        (table,),
    )
    return bool(cursor.fetchone()[0])


def _column_exists(cursor, table: str, column: str) -> bool:
    cursor.execute(
        """
        SELECT COUNT(*)
        FROM information_schema.columns
        WHERE table_schema=DATABASE() AND table_name=%s AND column_name=%s
        """,
        (table, column),
    )
    return bool(cursor.fetchone()[0])


def _index_exists(cursor, table: str, index_name: str) -> bool:
    cursor.execute(
        """
        SELECT COUNT(*)
        FROM information_schema.statistics
        WHERE table_schema=DATABASE() AND table_name=%s AND index_name=%s
        """,
        (table, index_name),
    )
    return bool(cursor.fetchone()[0])


def _tenant_row_exists(cursor, tenant_id: str) -> bool:
    if not _table_exists(cursor, "tenants"):
        return False
    cursor.execute("SELECT COUNT(*) FROM tenants WHERE id=%s", (tenant_id,))
    return bool(cursor.fetchone()[0])


def _profile_report(label: str, config: dict[str, Any], expected_columns: dict[str, str], tenant_id: str) -> dict[str, Any]:
    report: dict[str, Any] = {
        "label": label,
        "database": str(config.get("database") or ""),
        "ok": False,
        "support_tables": {},
        "tenant_row": False,
        "checked_tables": [],
        "missing_columns": [],
        "skipped_tables": [],
        "error": "",
    }
    try:
        conn = _connect(config)
        cursor = conn.cursor()
        try:
            for table in SUPPORT_TABLES:
                report["support_tables"][table] = _table_exists(cursor, table)
            report["tenant_row"] = _tenant_row_exists(cursor, tenant_id)
            for table, column in expected_columns.items():
                if not _table_exists(cursor, table):
                    report["skipped_tables"].append(table)
                    continue
                report["checked_tables"].append(table)
                if not _column_exists(cursor, table, column):
                    report["missing_columns"].append(f"{table}.{column}")
        finally:
            cursor.close()
            conn.close()
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        return report

    support_ok = all(bool(v) for v in report["support_tables"].values())
    columns_ok = not report["missing_columns"]
    report["ok"] = bool(support_ok and report["tenant_row"] and columns_ok and report["checked_tables"])
    return report


def inspect_tenant_schema(*, tenant_id: str | None = None, auth_config: dict[str, Any] | None = None) -> dict[str, Any]:
    tenant = tenant_id or tenant_id_from_env()
    if not TENANT_ID_RE.fullmatch(tenant):
        return {"ok": False, "tenant_id": tenant, "profiles": [], "error": "invalid_tenant_id"}

    profiles = [
        ("auth", auth_db_config(auth_config), AUTH_TENANT_COLUMNS),
        ("osc", osc_db_config(), OSC_TENANT_COLUMNS),
    ]
    reports = []
    seen: set[tuple[str, int, str, str]] = set()
    for label, config, columns in profiles:
        key = (
            str(config.get("host") or ""),
            int(config.get("port") or 0),
            str(config.get("database") or ""),
            str(config.get("user") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        reports.append(_profile_report(label, config, columns, tenant))
    return {
        "ok": bool(reports) and all(bool(item.get("ok")) for item in reports),
        "tenant_id": tenant,
        "profiles": reports,
    }


def _ensure_support_tables(cursor) -> None:
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS tenants (
            id VARCHAR(64) NOT NULL PRIMARY KEY,
            name VARCHAR(255) NOT NULL,
            status VARCHAR(32) NOT NULL DEFAULT 'active',
            public_base_url VARCHAR(512) DEFAULT '',
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            UNIQUE KEY uq_tenants_name (name)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS tenant_memberships (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            tenant_id VARCHAR(64) NOT NULL,
            user_id VARCHAR(64) NOT NULL,
            role VARCHAR(32) NOT NULL DEFAULT 'member',
            status VARCHAR(32) NOT NULL DEFAULT 'active',
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE KEY uq_tenant_user (tenant_id, user_id),
            KEY idx_tenant_memberships_user (user_id),
            KEY idx_tenant_memberships_tenant (tenant_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS tenant_api_keys (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            tenant_id VARCHAR(64) NOT NULL,
            key_hash CHAR(64) NOT NULL,
            label VARCHAR(100) NOT NULL DEFAULT '',
            role VARCHAR(32) NOT NULL DEFAULT 'api',
            status VARCHAR(32) NOT NULL DEFAULT 'active',
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            last_used_at DATETIME NULL,
            UNIQUE KEY uq_tenant_api_key_hash (key_hash),
            KEY idx_tenant_api_keys_tenant (tenant_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS tenant_storage_roots (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            tenant_id VARCHAR(64) NOT NULL,
            root_path TEXT NOT NULL,
            label VARCHAR(100) NOT NULL DEFAULT '',
            active TINYINT(1) NOT NULL DEFAULT 1,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            KEY idx_tenant_storage_roots_tenant (tenant_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """
    )


def _ensure_tenant_row(cursor, tenant_id: str, tenant_name: str, public_base_url: str) -> None:
    cursor.execute(
        """
        INSERT INTO tenants (id, name, status, public_base_url)
        VALUES (%s, %s, 'active', %s)
        ON DUPLICATE KEY UPDATE
            name=VALUES(name),
            status='active',
            public_base_url=VALUES(public_base_url)
        """,
        (tenant_id, tenant_name, public_base_url),
    )


def _ensure_column(cursor, table: str, column: str, tenant_id: str) -> bool:
    if not _table_exists(cursor, table):
        return False
    table_ident = _safe_ident(table)
    column_ident = _safe_ident(column)
    tenant_literal = _safe_tenant_literal(tenant_id)
    if not _column_exists(cursor, table, column):
        cursor.execute(
            f"ALTER TABLE {table_ident} ADD COLUMN {column_ident} VARCHAR(64) NOT NULL DEFAULT {tenant_literal}"
        )
    else:
        cursor.execute(
            f"UPDATE {table_ident} SET {column_ident}=%s WHERE {column_ident} IS NULL OR {column_ident}='' OR {column_ident}='default'",
            (tenant_id,),
        )
        try:
            cursor.execute(
                f"ALTER TABLE {table_ident} MODIFY COLUMN {column_ident} VARCHAR(64) NOT NULL DEFAULT {tenant_literal}"
            )
        except Exception:
            pass
    index_name = f"idx_{table}_{column}"[:60]
    if not _index_exists(cursor, table, index_name):
        cursor.execute(f"CREATE INDEX {_safe_ident(index_name)} ON {table_ident} ({column_ident})")
    return True


def _ensure_memberships(cursor, tenant_id: str) -> None:
    if not _table_exists(cursor, "users"):
        return
    cursor.execute(
        """
        INSERT INTO tenant_memberships (tenant_id, user_id, role)
        SELECT %s, CAST(id AS CHAR), COALESCE(NULLIF(role, ''), 'member')
        FROM users
        ON DUPLICATE KEY UPDATE role=VALUES(role), status='active'
        """,
        (tenant_id,),
    )


def _apply_profile(
    *,
    label: str,
    config: dict[str, Any],
    expected_columns: dict[str, str],
    tenant_id: str,
    tenant_name: str,
    public_base_url: str,
) -> dict[str, Any]:
    conn = _connect(config)
    cursor = conn.cursor()
    applied: dict[str, Any] = {
        "label": label,
        "database": str(config.get("database") or ""),
        "tables_touched": [],
        "tables_skipped": [],
    }
    try:
        _ensure_support_tables(cursor)
        _ensure_tenant_row(cursor, tenant_id, tenant_name, public_base_url)
        for table, column in expected_columns.items():
            if _ensure_column(cursor, table, column, tenant_id):
                applied["tables_touched"].append(table)
            else:
                applied["tables_skipped"].append(table)
        _ensure_memberships(cursor, tenant_id)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()
    return applied


def apply_tenant_schema(
    *,
    tenant_id: str,
    tenant_name: str,
    public_base_url: str = "",
    auth_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not TENANT_ID_RE.fullmatch(tenant_id):
        raise ValueError("tenant_id must use lowercase letters, numbers, '_' or '-' and be 3-64 chars")
    if not str(tenant_name or "").strip():
        raise ValueError("tenant_name is required")

    profiles = [
        ("auth", auth_db_config(auth_config), AUTH_TENANT_COLUMNS),
        ("osc", osc_db_config(), OSC_TENANT_COLUMNS),
    ]
    results = []
    seen: set[tuple[str, int, str, str]] = set()
    for label, config, columns in profiles:
        key = (
            str(config.get("host") or ""),
            int(config.get("port") or 0),
            str(config.get("database") or ""),
            str(config.get("user") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        results.append(
            _apply_profile(
                label=label,
                config=config,
                expected_columns=columns,
                tenant_id=tenant_id,
                tenant_name=tenant_name,
                public_base_url=public_base_url,
            )
        )
    return {"ok": True, "tenant_id": tenant_id, "profiles": results}

