from __future__ import annotations

import logging
import os
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import mysql.connector

MAGI_ROOT = Path(__file__).resolve().parent
OSC_HEADLESS_DIR = MAGI_ROOT / "skills" / "osc-orchestrator"

for candidate in (MAGI_ROOT, OSC_HEADLESS_DIR):
    s = str(candidate)
    if s not in sys.path:
        sys.path.insert(0, s)

from api.case_path_mapper import translate_case_path_to_local, translate_local_path_to_canonical
from osc_headless.db import DBConfig, connect_mysql, ensure_cases_schema

logger = logging.getLogger("magi.osc.compat")


class DatabaseManager:
    """
    Lightweight compatibility shim for legacy `from osc import DatabaseManager`
    callers used by LAF and audit flows.
    """

    def __init__(self, db_config: dict[str, Any] | None = None):
        cfg = dict(db_config or {})
        self.db_config = cfg
        self.last_write_error = ""
        self._table_columns_cache: dict[str, dict[str, str]] = {}
        self._ensure_min_schema()

    def _cfg(self) -> DBConfig:
        cfg = dict(self.db_config or {})
        timeout = int(cfg.get("connection_timeout") or cfg.get("connect_timeout") or 5)
        return DBConfig(
            host=str(cfg.get("host") or "127.0.0.1"),
            port=int(cfg.get("port") or 3306),
            user=str(cfg.get("user") or ""),
            password=str(cfg.get("password") or ""),
            database=str(cfg.get("database") or "law_firm_data"),
            connection_timeout=timeout,
        )

    def _get_connection(self) -> mysql.connector.MySQLConnection:
        return connect_mysql(self._cfg())

    def _fetch_table_columns(self, table_name: str) -> dict[str, str]:
        key = str(table_name or "").strip().lower()
        if not key:
            return {}
        cached = self._table_columns_cache.get(key)
        if cached is not None:
            return cached
        conn = None
        cur = None
        try:
            conn = self._get_connection()
            cur = conn.cursor()
            cur.execute(f"SHOW COLUMNS FROM `{key}`")
            cols = {}
            for row in cur.fetchall() or []:
                if not row:
                    continue
                name = str(row[0] or "").strip()
                if not name:
                    continue
                cols[name] = str(row[1] or "")
            self._table_columns_cache[key] = cols
            return cols
        except Exception:
            return {}
        finally:
            try:
                if cur:
                    cur.close()
            except Exception:
                pass
            try:
                if conn:
                    conn.close()
            except Exception:
                pass

    def _execute_ddl(self, statements: Iterable[str]) -> None:
        conn = None
        cur = None
        try:
            conn = self._get_connection()
            cur = conn.cursor()
            for statement in statements:
                sql = str(statement or "").strip()
                if not sql:
                    continue
                try:
                    cur.execute(sql)
                except Exception:
                    continue
            conn.commit()
            self._table_columns_cache.clear()
        except Exception:
            logger.debug("DDL best-effort failed", exc_info=True)
        finally:
            try:
                if cur:
                    cur.close()
            except Exception:
                pass
            try:
                if conn:
                    conn.close()
            except Exception:
                pass

    def _ensure_min_schema(self) -> None:
        conn = None
        try:
            conn = self._get_connection()
            ensure_cases_schema(conn)
        except Exception:
            logger.debug("ensure_cases_schema failed", exc_info=True)
        finally:
            try:
                if conn:
                    conn.close()
            except Exception:
                pass

        self._execute_ddl(
            [
                "ALTER TABLE `cases` ADD COLUMN `client_name_en` VARCHAR(255) DEFAULT ''",
                "ALTER TABLE `cases` ADD COLUMN `case_subject` TEXT",
                "ALTER TABLE `cases` ADD COLUMN `case_stage` VARCHAR(100) DEFAULT ''",
                "ALTER TABLE `cases` ADD COLUMN `court_division` VARCHAR(255) DEFAULT ''",
                "ALTER TABLE `cases` ADD COLUMN `lawyer` VARCHAR(255) DEFAULT ''",
                "ALTER TABLE `cases` ADD COLUMN `legal_aid_status` VARCHAR(100) DEFAULT ''",
                "ALTER TABLE `cases` ADD COLUMN `folder_name` VARCHAR(255) DEFAULT ''",
                "ALTER TABLE `cases` ADD COLUMN `start_date` DATE NULL",
                "ALTER TABLE `cases` ADD COLUMN `court_date` DATE NULL",
                """
                CREATE TABLE IF NOT EXISTS `clients` (
                    `id` VARCHAR(64) PRIMARY KEY,
                    `name` VARCHAR(255) NOT NULL,
                    `phone` VARCHAR(64) DEFAULT '',
                    `email` VARCHAR(255) DEFAULT '',
                    `address` TEXT,
                    `tax_id` VARCHAR(64) DEFAULT '',
                    `status` VARCHAR(32) DEFAULT 'Active',
                    `created_date` TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """,
                """
                CREATE TABLE IF NOT EXISTS `laf_email_records` (
                    `id` VARCHAR(64) PRIMARY KEY,
                    `gmail_message_id` VARCHAR(255) NOT NULL,
                    `subject` TEXT,
                    `sender` TEXT,
                    `received_at` DATETIME NULL,
                    `processed_at` DATETIME NULL,
                    `status` VARCHAR(64) DEFAULT '',
                    `case_number` VARCHAR(64) DEFAULT '',
                    `created_case_id` VARCHAR(64) DEFAULT '',
                    `error_message` TEXT,
                    `created_date` TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    KEY `idx_laf_email_msgid` (`gmail_message_id`(191))
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """,
            ]
        )

    def execute(self, query: str, params: tuple | None = None, fetch: str | None = None):
        conn = None
        cur = None
        try:
            conn = self._get_connection()
            dictionary = bool(fetch in {"one", "all"})
            cur = conn.cursor(dictionary=dictionary)
            cur.execute(query, params or ())
            if fetch == "one":
                return cur.fetchone()
            if fetch == "all":
                return cur.fetchall()
            conn.commit()
            return int(getattr(cur, "lastrowid", 0) or getattr(cur, "rowcount", 0) or 0)
        finally:
            try:
                if cur:
                    cur.close()
            except Exception:
                pass
            try:
                if conn:
                    conn.close()
            except Exception:
                pass

    def fetch_one(self, query: str, params: tuple | None = None, as_dict: bool = False):
        conn = None
        cur = None
        try:
            conn = self._get_connection()
            cur = conn.cursor(dictionary=bool(as_dict))
            cur.execute(query, params or ())
            return cur.fetchone()
        except Exception:
            return None
        finally:
            try:
                if cur:
                    cur.close()
            except Exception:
                pass
            try:
                if conn:
                    conn.close()
            except Exception:
                pass

    def fetch_all(self, query: str, params: tuple | None = None, as_dict: bool = False):
        conn = None
        cur = None
        try:
            conn = self._get_connection()
            cur = conn.cursor(dictionary=bool(as_dict))
            cur.execute(query, params or ())
            return cur.fetchall() or []
        except Exception:
            return []
        finally:
            try:
                if cur:
                    cur.close()
            except Exception:
                pass
            try:
                if conn:
                    conn.close()
            except Exception:
                pass

    def execute_write(self, query: str, params: tuple | None = None):
        conn = None
        cur = None
        try:
            conn = self._get_connection()
            cur = conn.cursor()
            cur.execute(query, params or ())
            conn.commit()
            self.last_write_error = ""
            return int(getattr(cur, "rowcount", 0) or 0)
        except Exception as exc:
            self.last_write_error = str(exc)
            try:
                if conn:
                    conn.rollback()
            except Exception:
                pass
            raise
        finally:
            try:
                if cur:
                    cur.close()
            except Exception:
                pass
            try:
                if conn:
                    conn.close()
            except Exception:
                pass

    def translate_path_to_local(self, db_path_str: str) -> str:
        return translate_case_path_to_local(db_path_str)

    def translate_path_to_canonical(self, local_path_str: str) -> str:
        return translate_local_path_to_canonical(local_path_str)

    def generate_case_number(self) -> str:
        year = datetime.now().strftime("%Y")
        sql = """
            SELECT MAX(CAST(SUBSTR(`case_number`, 6) AS SIGNED))
            FROM `cases`
            WHERE `case_number` LIKE %s
        """
        result = self.fetch_one(sql, (f"{year}-%",))
        max_num = 0
        if isinstance(result, dict):
            value = next(iter(result.values()), 0)
            max_num = int(value or 0)
        elif isinstance(result, (tuple, list)) and result:
            max_num = int(result[0] or 0)
        return f"{year}-{max_num + 1:04d}"

    def check_and_add_client(self, client_data: dict[str, Any]) -> str | None:
        name = str((client_data or {}).get("name") or "").strip()
        if not name:
            return None
        phone = str((client_data or {}).get("phone") or "").strip()
        email = str((client_data or {}).get("email") or "").strip()
        address = str((client_data or {}).get("address") or "").strip()
        tax_id = str((client_data or {}).get("tax_id") or "").strip()

        conditions = ["`name` = %s"]
        params: list[Any] = [name]
        if phone:
            conditions.append("`phone` = %s")
            params.append(phone)
        if email:
            conditions.append("`email` = %s")
            params.append(email)
        row = self.fetch_one(
            f"SELECT `id` FROM `clients` WHERE {' OR '.join(conditions)} LIMIT 1",
            tuple(params),
            as_dict=True,
        )
        if isinstance(row, dict) and row.get("id"):
            client_id = str(row["id"])
            updates = []
            update_params: list[Any] = []
            if email:
                updates.append("`email` = CASE WHEN `email` IS NULL OR `email` = '' THEN %s ELSE `email` END")
                update_params.append(email)
            if phone:
                updates.append("`phone` = CASE WHEN `phone` IS NULL OR `phone` = '' THEN %s ELSE `phone` END")
                update_params.append(phone)
            if address:
                updates.append("`address` = CASE WHEN `address` IS NULL OR `address` = '' THEN %s ELSE `address` END")
                update_params.append(address)
            if tax_id:
                updates.append("`tax_id` = CASE WHEN `tax_id` IS NULL OR `tax_id` = '' THEN %s ELSE `tax_id` END")
                update_params.append(tax_id)
            if updates:
                update_params.append(client_id)
                self.execute_write(
                    f"UPDATE `clients` SET {', '.join(updates)} WHERE `id` = %s",
                    tuple(update_params),
                )
            return client_id

        cols = self._fetch_table_columns("clients")
        client_id = f"C{uuid.uuid4().hex[:8].upper()}"
        insert_cols = []
        insert_vals = []

        def _push(col: str, value: Any) -> None:
            if col in cols:
                insert_cols.append(f"`{col}`")
                insert_vals.append(value)

        id_type = str(cols.get("id") or "").lower()
        if "int" not in id_type and "serial" not in id_type:
            _push("id", client_id)
        _push("name", name)
        _push("phone", phone)
        _push("email", email)
        _push("address", address)
        _push("tax_id", tax_id)
        _push("status", "Active")
        if "created_date" in cols:
            insert_cols.append("`created_date`")
            insert_vals.append(datetime.now())

        placeholders = ", ".join(["%s"] * len(insert_vals))
        self.execute_write(
            f"INSERT INTO `clients` ({', '.join(insert_cols)}) VALUES ({placeholders})",
            tuple(insert_vals),
        )
        if "int" in id_type or "serial" in id_type:
            row = self.fetch_one(
                "SELECT `id` FROM `clients` WHERE `name` = %s ORDER BY `created_date` DESC, `id` DESC LIMIT 1",
                (name,),
                as_dict=True,
            )
            if isinstance(row, dict) and row.get("id") is not None:
                return str(row["id"])
        return client_id

    def check_laf_case_exists(
        self,
        laf_case_number: str | None = None,
        client_name: str | None = None,
        case_type: str | None = None,
        case_reason: str | None = None,
    ):
        laf_no = str(laf_case_number or "").strip()
        if laf_no:
            row = self.fetch_one(
                """
                SELECT * FROM `cases`
                WHERE `legal_aid_number` = %s
                   OR `laf_case_no` = %s
                   OR `application_no` = %s
                   OR (`notes` IS NOT NULL AND `notes` LIKE %s)
                LIMIT 1
                """,
                (laf_no, laf_no, laf_no, f"%{laf_no}%"),
                as_dict=True,
            )
            if row:
                return row

        name = str(client_name or "").strip()
        if not name:
            return None
        reason = str(case_reason or "").strip()
        ctype = str(case_type or "").strip()
        row = self.fetch_one(
            """
            SELECT * FROM `cases`
            WHERE `client_name` = %s
              AND (`case_type` = %s OR `case_reason` LIKE %s)
              AND (`legal_aid_number` IS NULL OR `legal_aid_number` = '')
              AND (`laf_case_no` IS NULL OR `laf_case_no` = '')
              AND (`application_no` IS NULL OR `application_no` = '')
            ORDER BY `created_date` DESC
            LIMIT 1
            """,
            (name, ctype, f"%{reason}%" if reason else "%"),
            as_dict=True,
        )
        if row and laf_no and row.get("id") is not None:
            self.execute_write(
                """
                UPDATE `cases`
                SET `legal_aid_number` = %s,
                    `laf_case_no` = CASE WHEN `laf_case_no` IS NULL OR `laf_case_no` = '' THEN %s ELSE `laf_case_no` END,
                    `application_no` = CASE WHEN `application_no` IS NULL OR `application_no` = '' THEN %s ELSE `application_no` END
                WHERE `id` = %s
                """,
                (laf_no, laf_no, laf_no, row["id"]),
            )
            row["legal_aid_number"] = laf_no
            row["laf_case_no"] = row.get("laf_case_no") or laf_no
            row["application_no"] = row.get("application_no") or laf_no
        return row

    def insert_case_from_csv(self, case_data: dict[str, Any]) -> bool:
        data = dict(case_data or {})
        case_number = str(data.get("case_number") or "").strip()
        if not case_number:
            return False

        cols = self._fetch_table_columns("cases")
        if not cols:
            self._ensure_min_schema()
            cols = self._fetch_table_columns("cases")

        if not data.get("status"):
            data["status"] = "進行中"
        if not data.get("legal_aid_status"):
            data["legal_aid_status"] = "未開辦"
        if not data.get("laf_case_no"):
            data["laf_case_no"] = data.get("legal_aid_number") or ""
        if not data.get("application_no"):
            data["application_no"] = data.get("legal_aid_number") or data.get("laf_case_no") or ""
        if not data.get("court_case_no"):
            data["court_case_no"] = data.get("court_case_number") or ""
        if not data.get("court_case_number"):
            data["court_case_number"] = data.get("court_case_no") or ""
        if not data.get("folder_name") and data.get("folder_path"):
            data["folder_name"] = os.path.basename(str(data.get("folder_path")).rstrip("/\\"))

        for key in ("start_date", "court_date"):
            if data.get(key) == "":
                data[key] = None

        existing = self.fetch_one(
            "SELECT `id` FROM `cases` WHERE `case_number` = %s ORDER BY `created_date` DESC, `id` DESC LIMIT 1",
            (case_number,),
            as_dict=True,
        )

        preferred_order = [
            "case_number",
            "client_name",
            "client_name_en",
            "case_type",
            "case_category",
            "case_subject",
            "case_reason",
            "status",
            "start_date",
            "court_date",
            "lawyer",
            "folder_path",
            "folder_name",
            "case_stage",
            "court_case_number",
            "court_case_no",
            "court_division",
            "court_name",
            "legal_aid_status",
            "legal_aid_number",
            "laf_case_no",
            "application_no",
            "notes",
        ]
        available = [name for name in preferred_order if name in cols]

        if existing and existing.get("id") is not None:
            assignments = []
            params: list[Any] = []
            for name in available:
                if name == "case_number":
                    continue
                assignments.append(f"`{name}` = %s")
                params.append(data.get(name))
            params.append(existing["id"])
            self.execute_write(
                f"UPDATE `cases` SET {', '.join(assignments)} WHERE `id` = %s",
                tuple(params),
            )
            return True

        insert_cols = []
        insert_vals = []
        id_type = str(cols.get("id") or "").lower()
        if "id" in cols and "int" not in id_type and "serial" not in id_type:
            insert_cols.append("`id`")
            insert_vals.append(str(uuid.uuid4()))
        for name in available:
            insert_cols.append(f"`{name}`")
            insert_vals.append(data.get(name))
        placeholders = ", ".join(["%s"] * len(insert_vals))
        self.execute_write(
            f"INSERT INTO `cases` ({', '.join(insert_cols)}) VALUES ({placeholders})",
            tuple(insert_vals),
        )
        return True

    def check_laf_email_exists(self, gmail_message_id: str) -> bool:
        mid = str(gmail_message_id or "").strip()
        if not mid:
            return False
        row = self.fetch_one(
            "SELECT `id` FROM `laf_email_records` WHERE `gmail_message_id` = %s LIMIT 1",
            (mid,),
            as_dict=True,
        )
        return bool(row)

    def add_laf_email_record(self, record_data: dict[str, Any]) -> bool:
        data = dict(record_data or {})
        mid = str(data.get("gmail_message_id") or "").strip()
        if not mid:
            return False
        if self.check_laf_email_exists(mid):
            return True
        cols = self._fetch_table_columns("laf_email_records")
        values_by_col = {
            "id": str(uuid.uuid4()),
            "gmail_message_id": mid,
            "subject": data.get("subject") or "",
            "sender": data.get("sender") or "",
            "received_at": data.get("received_at"),
            "processed_at": data.get("processed_at") or datetime.now(),
            "status": data.get("status") or "",
            "case_number": data.get("case_number") or "",
            "created_case_id": data.get("created_case_id") or data.get("case_id") or "",
            "error_message": data.get("error_message") or "",
            "created_date": data.get("created_date") or datetime.now(),
        }
        insert_cols = []
        insert_vals = []
        for name in values_by_col:
            if name in cols:
                insert_cols.append(f"`{name}`")
                insert_vals.append(values_by_col[name])
        placeholders = ", ".join(["%s"] * len(insert_vals))
        self.execute_write(
            f"INSERT INTO `laf_email_records` ({', '.join(insert_cols)}) VALUES ({placeholders})",
            tuple(insert_vals),
        )
        return True


__all__ = ["DatabaseManager"]
